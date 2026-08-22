"""Hyperliquid whale positioning — a global scanner, not a per-symbol lookup.

One run reads the leaderboard's top wallets, pulls every position each of them
holds, and derives a per-coin bias for the whole market at once. That shape is
deliberate and it is the reason this is a collector rather than something the
context provider calls: the leaderboard answer is identical for every symbol, so
asking for it inside ``ContextProvider.build(symbol)`` would fetch the same data
fifteen times per cycle and cost ~450 wallet requests instead of ~30.

**What counts as a signal.** A single whale opening a position is noise — they
run dozens, hedge across venues, and are wrong often enough to be untradeable.
The alert condition is *coordination*: at least
:data:`DEFAULT_MIN_WALLETS` distinct wallets opening or materially adding to a
position in the same coin, in the same direction, inside one scan window. A
per-coin cooldown then keeps a sustained build-up from re-alerting every scan.

Two structural fixes over the original ``whale_tracker.py``: the module-level
``_state`` dict becomes instance state (it made the scanner a singleton that
tests could not isolate), and the scanner no longer sends Telegram messages
itself — it writes a snapshot, and the reporter decides how to say it.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

log = logging.getLogger("wolf.onchain.whale")

#: StateStore document this collector owns.
STATE_KEY = "whale_hyperliquid"

DEFAULT_TOP_WALLETS = 30        # leaderboard depth tracked per scan
DEFAULT_MIN_POSITION_USD = 30_000.0   # ignore dust positions
DEFAULT_MIN_WALLETS = 3         # distinct wallets needed to call it coordination
DEFAULT_COOLDOWN_MIN = 60.0     # per-coin quiet period after an alert
ADD_THRESHOLD = 1.10            # a position must grow >10% to count as "added to"

LONG = "LONG"
SHORT = "SHORT"


# ── pure logic (unit-tested without network) ──────────────────────────────
def parse_leaderboard(payload: Any, limit: int = DEFAULT_TOP_WALLETS) -> list[str]:
    """Extract the top ``limit`` wallet addresses, ranked by all-time PnL."""
    rows = payload.get("leaderboardRows") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    wallets: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        addr = row.get("ethAddress") or row.get("address") or ""
        if not addr:
            continue
        pnl = row.get("pnl")
        if isinstance(pnl, dict):
            pnl_val = _f(pnl.get("allTime"))
        elif isinstance(pnl, list):
            # Some responses shape PnL as [[window, value], ...].
            pnl_val = max((_f(p[1]) for p in pnl if isinstance(p, list) and len(p) > 1),
                          default=0.0)
        else:
            pnl_val = _f(pnl)
        wallets.append((str(addr), pnl_val))
    wallets.sort(key=lambda w: w[1], reverse=True)
    return [addr for addr, _ in wallets[:limit]]


def parse_positions(payload: Any, min_notional: float = DEFAULT_MIN_POSITION_USD) -> dict[str, dict]:
    """Parse one wallet's ``clearinghouseState`` into ``coin → position``.

    Positions below ``min_notional`` are dropped: a whale's $2k tail position
    says nothing about conviction and would let three rounding errors look like
    coordination.
    """
    rows = payload.get("assetPositions") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict] = {}
    for entry in rows:
        pos = entry.get("position") if isinstance(entry, dict) else None
        if not isinstance(pos, dict):
            continue
        coin = str(pos.get("coin", "")).upper()
        szi = _f(pos.get("szi"))
        entry_px = _f(pos.get("entryPx"))
        notional = abs(szi) * entry_px
        if not coin or szi == 0 or notional < min_notional:
            continue
        out[coin] = {
            "side": LONG if szi > 0 else SHORT,
            "size": abs(szi),
            "entry_px": entry_px,
            "notional": notional,
        }
    return out


def detect_whale_coordination(
    old_positions: dict[str, dict],
    new_positions: dict[str, dict],
    *,
    min_wallets: int = DEFAULT_MIN_WALLETS,
) -> list[dict]:
    """Find coins several wallets opened or added to, same direction, this window.

    ``old_positions`` / ``new_positions`` map ``address → {coin → position}``.
    Returns one event per (coin, direction) that cleared ``min_wallets``, sorted
    by total notional. Fewer than ``min_wallets`` is not a weak signal — it is
    *no* signal, and is dropped rather than reported quietly.
    """
    by_coin: dict[str, dict[str, list[dict]]] = {}

    for addr, wallet in new_positions.items():
        previous = old_positions.get(addr) or {}
        for coin, pos in wallet.items():
            before = previous.get(coin)
            is_new = before is None
            # "Added to" needs a real increase; a mark-price wiggle is not news.
            is_add = before is not None and pos["notional"] > before["notional"] * ADD_THRESHOLD
            if not (is_new or is_add):
                continue
            sides = by_coin.setdefault(coin, {LONG: [], SHORT: []})
            sides[pos["side"]].append({
                "addr": addr,
                "notional": pos["notional"],
                "entry": pos["entry_px"],
                "is_new": is_new,
            })

    events: list[dict] = []
    for coin, sides in by_coin.items():
        for side, wallets in sides.items():
            if len(wallets) < min_wallets:
                continue
            events.append({
                "coin": coin,
                "direction": side,
                "wallet_count": len(wallets),
                "notional_usd": sum(w["notional"] for w in wallets),
                "wallets": wallets,
            })
    return sorted(events, key=lambda e: e["notional_usd"], reverse=True)


def summarise_coin_bias(positions: dict[str, dict]) -> dict[str, dict]:
    """Aggregate every tracked wallet's book into a per-coin long/short split."""
    bias: dict[str, dict] = {}
    for wallet in positions.values():
        for coin, pos in wallet.items():
            row = bias.setdefault(coin, {
                "long_notional": 0.0, "short_notional": 0.0,
                "long_count": 0, "short_count": 0,
            })
            if pos["side"] == LONG:
                row["long_notional"] += pos["notional"]
                row["long_count"] += 1
            else:
                row["short_notional"] += pos["notional"]
                row["short_count"] += 1
    return bias


# ── collector (network + cooldown state) ──────────────────────────────────
class WhaleHyperliquidCollector:
    """Scans Hyperliquid's top wallets and persists per-coin whale positioning."""

    name = "whale_hyperliquid"

    def __init__(
        self,
        store,
        *,
        info_url: str = "https://api.hyperliquid.xyz/info",
        leaderboard_url: str = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",
        timeout: float = 15.0,
        top_wallets: int = DEFAULT_TOP_WALLETS,
        min_position_usd: float = DEFAULT_MIN_POSITION_USD,
        min_wallets: int = DEFAULT_MIN_WALLETS,
        cooldown_min: float = DEFAULT_COOLDOWN_MIN,
        request_pause: float = 0.15,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._store = store
        self._info_url = info_url
        self._leaderboard_url = leaderboard_url
        self._timeout = timeout
        self._top_wallets = top_wallets
        self._min_position_usd = min_position_usd
        self._min_wallets = min_wallets
        self._cooldown_min = cooldown_min
        self._request_pause = request_pause
        self._session = session or requests.Session()
        # Instance state. The original held these in a module-level ``_state``
        # dict, which made the tracker a process-wide singleton: two instances
        # shared one baseline and tests could not start from a clean slate.
        self._positions: dict[str, dict] = {}
        self._cooldowns: dict[str, float] = {}   # coin → monotonic-ish epoch
        self._scanned_once = False

    # ── HTTP ──────────────────────────────────────────────────────────
    def _get_leaderboard(self) -> list[str]:
        try:
            resp = self._session.get(self._leaderboard_url, timeout=self._timeout,
                                     headers={"User-Agent": "wolf/1.0"})
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            log.debug("hyperliquid leaderboard error: %s", exc)
            return []
        except ValueError as exc:
            log.debug("hyperliquid leaderboard invalid JSON: %s", exc)
            return []
        return parse_leaderboard(payload, self._top_wallets)

    def _get_wallet_positions(self, address: str) -> dict[str, dict]:
        try:
            resp = self._session.post(
                self._info_url,
                json={"type": "clearinghouseState", "user": address},
                timeout=self._timeout,
                headers={"User-Agent": "wolf/1.0"},
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            log.debug("hyperliquid wallet %s error: %s", address[:8], exc)
            return {}
        except ValueError as exc:
            log.debug("hyperliquid wallet %s invalid JSON: %s", address[:8], exc)
            return {}
        return parse_positions(payload, self._min_position_usd)

    # ── cooldown ──────────────────────────────────────────────────────
    def in_cooldown(self, coin: str, *, now: Optional[float] = None) -> bool:
        last = self._cooldowns.get(coin)
        if last is None:
            return False
        elapsed_min = ((now if now is not None else time.time()) - last) / 60.0
        return elapsed_min < self._cooldown_min

    def _set_cooldown(self, coin: str, *, now: Optional[float] = None) -> None:
        self._cooldowns[coin] = now if now is not None else time.time()

    def _apply_cooldown(self, events: list[dict], *, now: Optional[float] = None) -> list[dict]:
        """Drop events for coins still inside their quiet period, arm the rest.

        A whale build-up unfolds across several scans; without this every scan
        would re-report the same accumulation as if it were new.
        """
        fresh: list[dict] = []
        for event in events:
            coin = event["coin"]
            if self.in_cooldown(coin, now=now):
                log.debug("Whale %s still in cooldown — not re-alerting", coin)
                continue
            self._set_cooldown(coin, now=now)
            fresh.append(event)
        return fresh

    # ── orchestration ─────────────────────────────────────────────────
    def scan(self) -> dict:
        """One full scan: leaderboard → positions → coordination → StateStore."""
        wallets = self._get_leaderboard()
        if not wallets:
            log.warning("Whale scan: leaderboard empty — keeping the previous snapshot")
            return self._store.read(STATE_KEY, default={}) or {}

        previous = self._positions
        current: dict[str, dict] = {}
        for address in wallets:
            positions = self._get_wallet_positions(address)
            if positions:
                current[address] = positions
            if self._request_pause:
                time.sleep(self._request_pause)
        self._positions = current

        bias = summarise_coin_bias(current)

        if not self._scanned_once:
            # No baseline yet, so every position looks newly opened. Record the
            # snapshot and start detecting on the next scan.
            self._scanned_once = True
            log.info("Whale scan: baseline captured (%d wallets, %d coins)",
                     len(current), len(bias))
            return self._persist([], bias, len(current))

        events = detect_whale_coordination(previous, current, min_wallets=self._min_wallets)
        fresh = self._apply_cooldown(events)
        if fresh:
            log.info("Whale coordination: %s",
                     ", ".join(f"{e['coin']} {e['direction']} x{e['wallet_count']}" for e in fresh))
        return self._persist(fresh, bias, len(current))

    def _persist(self, events: list[dict], bias: dict[str, dict], wallet_count: int) -> dict:
        """Write the snapshot readers consume.

        ``coins`` carries only the coordinated moves — that is what a gate or an
        alert should act on. ``bias`` keeps the full positioning table for
        context that wants the whole book rather than the events.
        """
        coins = {
            e["coin"]: {
                "direction": e["direction"],
                "wallet_count": e["wallet_count"],
                "notional_usd": round(e["notional_usd"]),
                # Kept so the whale-room alert can name who moved. Capped: the
                # alert shows a handful, and an unbounded address list would
                # grow the persisted document for no reader.
                "wallets": [
                    {"addr": w["addr"], "notional": round(w["notional"]),
                     "is_new": w["is_new"]}
                    for w in sorted(e["wallets"], key=lambda w: w["notional"], reverse=True)[:5]
                ],
            }
            for e in events
        }
        doc = {
            "coins": coins,
            "bias": bias,
            "wallets_scanned": wallet_count,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._store.write(STATE_KEY, doc)
        return doc


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
