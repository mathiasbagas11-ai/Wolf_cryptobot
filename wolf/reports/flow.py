"""Flow Intelligence digest → its own Telegram topic.

Six sections, in the order capital actually moves: what the whole market did,
how much dry powder is sitting on the sidelines, which chain it is rotating
into, whether US institutions are bidding, where whales are positioned, and only
then which coins are worth a look.

**This reporter never fetches.** It reads snapshots the :mod:`wolf.onchain`
collectors persisted and renders them. The previous version fetched inside
``build()`` and threw the numbers away with the returned string, which meant a
second consumer of the same source had to fetch again and could reach a
different conclusion. One fetch, two consumers, no disagreement.

Four rules this report is held to, each of them a bug the previous one shipped:

* **No entry calls.** This answers "which coin is worth looking at", never "at
  what price do I get in". Entries, stops and targets come from the detectors
  and ``build_targets()``, which read price structure. The old report printed
  "entry zone: sekarang (pullback sehat)" derived from nothing but the 24h
  change — a price recommendation with no price analysis behind it.
* **Pegged and tokenized assets are filtered out.** Stablecoins and tokenized
  stocks screen beautifully on FDV/MC ≈ 1.0x because full circulation is
  trivially true for anything pegged. That is how $CRVUSD and $SNDKB became
  "token picks".
* **Watchlist entries must be tradeable here.** A screener hit whose
  ``get_klines()`` returns an empty list is not a finding, so the watchlist is
  intersected with the exchange universe.
* **Labels must match their numbers, and the verdict must match the body.** No
  "numpuk 🔥" on a 0.0% change, and no execution advice above a NEUTRAL read.

Stale snapshots are still shown — a 45-minute-old whale read is information —
but always carrying their age, so nothing is mistaken for live.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from wolf.market import age_minutes
from wolf.onchain.coinbase_premium import STATE_KEY as PREMIUM_KEY
from wolf.onchain.macro import STATE_KEY as MACRO_KEY
from wolf.onchain.whale_hyperliquid import STATE_KEY as WHALE_KEY
from wolf.textfmt import DIVIDER, esc, fmt_usd, now

log = logging.getLogger("wolf.reports")

#: Age past which a section is annotated with how old it is.
STALE_AFTER_MIN = 15.0

#: Bases that are pegged, wrapped or tokenized claims on something else. They
#: are excluded from the watchlist because the screen's headline metric —
#: FDV/MC near 1.0 — is trivially true for all of them and says nothing.
PEGGED_BASES: frozenset[str] = frozenset({
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD", "BUSD",
    "USDD", "USDP", "GUSD", "LUSD", "CRVUSD", "FRAX", "SUSD", "USD0", "USDY",
    "USDX", "EURC", "EURS", "EURI", "AEUR", "EUR", "GBP", "XAUT", "PAXG",
    "WBTC", "WETH", "WBETH", "WEETH", "STETH", "WSTETH", "CBBTC", "CBETH",
    "TBTC", "RETH", "SFRXETH", "METH", "SOLVBTC", "BSOL", "JITOSOL", "MSOL",
})

#: Substrings that mark a *name* as a pegged or tokenized instrument even when
#: its ticker is not in the list above — new stablecoins and tokenized equities
#: appear constantly and a static ticker list cannot keep up.
PEGGED_NAME_HINTS: tuple[str, ...] = (
    "usd", "stablecoin", "tokenized", "wrapped", "staked", "tether",
    "euro", "gold", "xstock", "backed ",
)

#: Majors have their own reporting; they are not "finds".
EXCLUDED_FROM_WATCHLIST: frozenset[str] = frozenset({"BTC", "ETH"})

# Verdict labels. Deliberately descriptive of capital direction only — none of
# them implies an action.
RISK_ON = "RISK-ON"
RISK_OFF = "RISK-OFF"
ROTATION = "ROTATION"
NEUTRAL = "NEUTRAL"

_VERDICT_TEXT = {
    RISK_ON: "Modal masuk ke risk asset — kondisi mendukung setup LONG dari layer teknikal.",
    RISK_OFF: "Modal keluar dari risk asset — setup LONG jalan lebih berat.",
    ROTATION: "Modal berputar antar sektor, bukan masuk-keluar — arah pasar belum satu suara.",
    NEUTRAL: "Belum ada arah modal yang jelas. Tidak ada yang perlu dikejar dari report ini.",
}


def is_pegged(symbol: str, name: str = "") -> bool:
    """Whether a token is a pegged, wrapped or tokenized claim on something else.

    Checked by ticker first, then by name, because the ticker list cannot keep
    up with how fast new stablecoins and tokenized equities are minted.
    """
    if symbol.upper() in PEGGED_BASES:
        return True
    lowered = name.lower()
    return any(hint in lowered for hint in PEGGED_NAME_HINTS)


def stale_note(ts, *, threshold_min: float = STALE_AFTER_MIN) -> str:
    """``" (data 45m lalu)"`` for an aged snapshot, empty while it is current."""
    age = age_minutes(ts)
    if age is None:
        return " (umur data tidak diketahui)"
    if age < threshold_min:
        return ""
    if age < 90:
        return f" (data {age:.0f}m lalu)"
    return f" (data {age / 60:.1f}j lalu)"


def flow_change_label(pct: float, *, deadband: float = 0.05) -> str:
    """Describe a percentage change without overstating it.

    The deadband exists because the previous report labelled a 0.0% change
    "numpuk 🔥": the direction was read off ``>= 0``, so *no movement at all*
    rendered as an inflow.
    """
    if abs(pct) < deadband:
        return "flat — belum ada perubahan berarti"
    return "numpuk 🔥" if pct > 0 else "nyusut 📉"


def build_watchlist(
    markets: Sequence[dict],
    tradeable_bases: Optional[set[str]] = None,
    *,
    limit: int = 5,
    min_market_cap: float = 20_000_000.0,
) -> list[dict]:
    """Rank screen candidates, after filtering out what cannot be acted on.

    Two filters, both of them fixes: pegged/tokenized assets never qualify, and
    (when a universe is supplied) a candidate must exist on an exchange Wolf can
    actually pull candles from. Deliberately returns names and numbers only —
    no entry, no target, no stop.
    """
    ranked: list[tuple[float, dict]] = []
    for row in markets:
        symbol = str(row.get("symbol", "")).upper()
        name = str(row.get("name", ""))
        if not symbol or symbol in EXCLUDED_FROM_WATCHLIST:
            continue
        if is_pegged(symbol, name):
            continue
        if tradeable_bases is not None and symbol not in tradeable_bases:
            continue
        mcap = _f(row.get("market_cap"))
        if mcap < min_market_cap:
            continue
        volume = _f(row.get("volume_24h"))
        turnover = volume / mcap if mcap else 0.0
        fdv = _f(row.get("fdv"))
        ranked.append((turnover, {
            "symbol": symbol,
            "name": name,
            "change_24h": _f(row.get("change_24h")),
            "market_cap": mcap,
            "turnover": turnover,
            "fdv_mc": (fdv / mcap) if (fdv and mcap) else None,
            "ath_change_pct": _f(row.get("ath_change_pct")),
        }))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in ranked[:limit]]


def decide_verdict(
    global_doc: Optional[dict],
    stablecoin: Optional[dict],
    premium: Optional[dict],
    whale_coins: dict,
) -> str:
    """Read the four macro inputs into one direction-of-capital verdict.

    Scores risk-on and risk-off evidence and requires a clear margin. A tie with
    evidence on both sides is ROTATION (capital moving sideways between
    sectors); no evidence at all is NEUTRAL. Nothing here recommends an action —
    the verdict describes the backdrop the technical layer operates in.
    """
    on = 0
    off = 0

    if global_doc:
        change = _f(global_doc.get("market_cap_change_24h"))
        if change >= 1.0:
            on += 1
        elif change <= -1.0:
            off += 1

    if stablecoin:
        # Growing stablecoin supply is sidelined cash building up (fuel);
        # shrinking supply is redemptions, i.e. cash leaving crypto entirely.
        change_7d = _f(stablecoin.get("change_7d_pct"))
        if change_7d >= 0.5:
            on += 1
        elif change_7d <= -0.5:
            off += 1

    if premium and premium.get("signal") == "ACCUMULATION":
        on += 1
    elif premium and premium.get("signal") == "DISTRIBUTION":
        off += 1

    longs = sum(1 for c in whale_coins.values() if str(c.get("direction")).upper() == "LONG")
    shorts = sum(1 for c in whale_coins.values() if str(c.get("direction")).upper() == "SHORT")
    if longs > shorts:
        on += 1
    elif shorts > longs:
        off += 1

    if on == 0 and off == 0:
        return NEUTRAL
    if on - off >= 2:
        return RISK_ON
    if off - on >= 2:
        return RISK_OFF
    if on == off:
        return ROTATION
    return RISK_ON if on > off else RISK_OFF


class FlowIntelReporter:
    """Renders the Flow Intelligence digest from persisted collector snapshots."""

    def __init__(
        self,
        store,
        universe_provider=None,
        anomaly=None,
        *,
        max_watchlist: int = 5,
        max_chains: int = 3,
        max_whale_coins: int = 5,
        tz: str = "UTC",
    ) -> None:
        self._store = store
        self._universe_provider = universe_provider
        self._anomaly = anomaly        # AnomalyScanner → appends its section (optional)
        self._max_watchlist = max_watchlist
        self._max_chains = max_chains
        self._max_whale_coins = max_whale_coins
        self._tz = tz

    # ── universe ──────────────────────────────────────────────────────
    def tradeable_bases(self) -> Optional[set[str]]:
        """Base symbols Wolf can actually fetch candles for.

        ``None`` when no universe is wired, which disables the filter rather
        than silently emptying the watchlist.
        """
        if self._universe_provider is None:
            return None
        try:
            symbols = self._universe_provider.symbols()
        except (AttributeError, ValueError, TypeError, KeyError):
            log.warning("Universe lookup failed — watchlist CEX filter disabled", exc_info=True)
            return None
        from wolf.exchange.sources import split_quote

        bases = {split_quote(str(s).upper())[0] for s in symbols or []}
        return bases or None

    # ── rendering ─────────────────────────────────────────────────────
    def build(self) -> Optional[str]:
        """Render the digest, or ``None`` when no collector has produced data."""
        macro = self._store.read(MACRO_KEY, default=None) or {}
        whale = self._store.read(WHALE_KEY, default=None) or {}
        premium = self._store.read(PREMIUM_KEY, default=None) or {}

        global_doc = macro.get("global")
        stablecoin = macro.get("stablecoin")
        chains = macro.get("chains") or []
        markets = macro.get("markets") or []
        whale_coins = whale.get("coins") or {}

        if not any((global_doc, stablecoin, chains, markets, whale_coins, premium.get("available"))):
            log.debug("Flow Intelligence: no collector data yet, skipping")
            return None

        macro_age = stale_note(macro.get("ts"))
        lines = [f"🧠 <b>FLOW INTELLIGENCE</b>\n{DIVIDER}"]
        lines += self._macro_section(global_doc, macro_age)
        lines += self._dry_powder_section(stablecoin, macro_age)
        lines += self._rotation_section(chains, macro_age)
        lines += self._institutional_section(premium)
        lines += self._whale_section(whale_coins, whale.get("ts"))
        lines += self._watchlist_section(markets, macro_age)

        verdict = decide_verdict(global_doc, stablecoin, premium if premium.get("available") else None,
                                 whale_coins)
        lines.append(f"\n<b>📌 KESIMPULAN: {esc(verdict)}</b>")
        lines.append(esc(_VERDICT_TEXT[verdict]))
        # Says plainly what this report is not, so its absence of entries reads
        # as a boundary rather than an omission.
        # Worded to avoid the vocabulary of execution entirely — the report is
        # tested for the absence of those words, and the disclaimer must not be
        # the one line that reintroduces them.
        lines.append("\n<i>Report ini soal ke mana modal bergerak, bukan harga eksekusi. "
                     "Level trading tetap datang dari sinyal detector.</i>")

        anomaly = self._anomaly_section(verdict)
        if anomaly:
            lines.append(f"{DIVIDER}\n{anomaly}")
        lines.append(f"{DIVIDER}\n🕐 {now(self._tz)}")
        return "\n".join(lines)

    def _anomaly_section(self, verdict: str) -> str:
        """Render the anomaly scanner's section; never let it break the report.

        A scan failure degrades to a one-line notice so the rest of the digest
        still goes out.
        """
        if self._anomaly is None:
            return ""
        from anomaly.formatter import simplify_verdict
        try:
            return self._anomaly.build_section(simplify_verdict(verdict))
        except Exception:  # an anomaly scan must never cost the whole digest
            log.exception("Anomaly scan failed")
            return "⚠️ Anomaly scan gagal — bagian lain report tetap dikirim."

    def _macro_section(self, global_doc: Optional[dict], age: str) -> list[str]:
        lines = [f"<b>1/ MARKET MACRO</b>{esc(age)}"]
        if not global_doc:
            return lines + ["• Data macro belum tersedia."]
        change = _f(global_doc.get("market_cap_change_24h"))
        arrow = "🟢" if change > 0 else ("🔴" if change < 0 else "⚪")
        lines.append(f"{arrow} Total mcap {fmt_big_usd(global_doc.get('total_market_cap'))} "
                     f"({change:+.1f}% 24h)")
        lines.append(f"₿ BTC dominance {_f(global_doc.get('btc_dominance')):.1f}% · "
                     f"USDT.D {_f(global_doc.get('usdt_dominance')):.1f}%")
        return lines

    def _dry_powder_section(self, stablecoin: Optional[dict], age: str) -> list[str]:
        lines = [f"\n<b>2/ DRY POWDER</b>{esc(age)}"]
        if not stablecoin:
            return lines + ["• Data stablecoin supply belum tersedia."]
        change_7d = _f(stablecoin.get("change_7d_pct"))
        lines.append(f"💵 Stablecoin supply {fmt_big_usd(stablecoin.get('total_usd'))} "
                     f"({change_7d:+.1f}% / 7h) — {esc(flow_change_label(change_7d))}")
        return lines

    def _rotation_section(self, chains: list, age: str) -> list[str]:
        lines = [f"\n<b>3/ CHAIN ROTATION</b>{esc(age)}"]
        if not chains:
            return lines + ["• Data DEX volume per chain belum tersedia."]
        ordered = sorted(chains, key=lambda c: _f(c.get("dex_volume_24h")), reverse=True)
        for chain in ordered[: self._max_chains]:
            change = _f(chain.get("change_1d"))
            mark = "🔥" if change > 0.05 else ("📉" if change < -0.05 else "⚪")
            lines.append(f"{mark} {esc(chain.get('label') or chain.get('chain'))}: "
                         f"DEX vol {fmt_usd(chain.get('dex_volume_24h'))} ({change:+.1f}%)")
        return lines

    def _institutional_section(self, premium: dict) -> list[str]:
        lines = ["\n<b>4/ INSTITUTIONAL FLOW</b>" + esc(stale_note(premium.get("ts")))
                 if premium else "\n<b>4/ INSTITUTIONAL FLOW</b>"]
        if not premium or not premium.get("available"):
            return lines + ["• Coinbase premium belum tersedia."]
        pct = _f(premium.get("premium_pct"))
        signal = str(premium.get("signal", "NEUTRAL"))
        label = {
            "ACCUMULATION": "institusi US lagi ngangkat bid",
            "DISTRIBUTION": "institusi US lagi distribusi",
        }.get(signal, "belum ada bias institusi yang jelas")
        lines.append(f"🏦 Coinbase premium (BTC) {pct:+.3f}% — {esc(label)}")
        return lines

    def _whale_section(self, coins: dict, ts) -> list[str]:
        lines = [f"\n<b>5/ WHALE POSITIONING</b>{esc(stale_note(ts))}"]
        if not coins:
            return lines + ["• Belum ada koordinasi whale terdeteksi window ini."]
        ordered = sorted(coins.items(), key=lambda kv: _f(kv[1].get("notional_usd")), reverse=True)
        for coin, row in ordered[: self._max_whale_coins]:
            direction = str(row.get("direction", "")).upper()
            emoji = "🟢" if direction == "LONG" else "🔴"
            lines.append(f"{emoji} <b>${esc(coin)}</b> {esc(direction)} — "
                         f"{int(_f(row.get('wallet_count')))} wallet · "
                         f"{fmt_usd(row.get('notional_usd'))}")
        return lines

    def _watchlist_section(self, markets: list, age: str) -> list[str]:
        lines = [f"\n<b>6/ WATCHLIST</b>{esc(age)}"]
        watchlist = build_watchlist(markets, self.tradeable_bases(), limit=self._max_watchlist)
        if not watchlist:
            return lines + ["• Belum ada kandidat yang lolos filter."]
        for row in watchlist:
            facts = [f"turnover {row['turnover'] * 100:.0f}% mcap",
                     f"mcap {fmt_usd(row['market_cap'])}"]
            if row["fdv_mc"] is not None:
                facts.append(f"FDV/MC {row['fdv_mc']:.1f}x")
            if row["ath_change_pct"] <= -1:
                facts.append(f"{row['ath_change_pct']:.0f}% dari ATH")
            lines.append(f"👀 <b>${esc(row['symbol'])}</b> {row['change_24h']:+.1f}% 24h · "
                         f"{esc(' · '.join(facts))}")
        return lines


def fmt_big_usd(v) -> str:
    """Like :func:`~wolf.textfmt.fmt_usd` but with a trillions tier.

    Total market cap is the digest's very first number and lives above $1T, where
    the shared helper's largest unit renders it as "$3100.00B". Kept local rather
    than widened in ``textfmt`` so the other reports' output is untouched.
    """
    value = _f(v)
    if abs(value) >= 1e12:
        return f"${value / 1e12:.2f}T"
    return fmt_usd(value)


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
