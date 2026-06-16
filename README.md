# 🐺 Wolf Crypto Tracker

A modular crypto **signal-tracking bot** with a REST API. It screens liquid
USDT pairs on Binance, records every signal it emits, and tracks each one
through its full lifecycle — reporting TP/SL outcomes and win-rate statistics.

This is a ground-up rewrite of an earlier bot
([`crypto_bot`](https://github.com/mathiasbagas11-ai/crypto_bot)), built to fix
the five architectural problems that made the original hard to maintain. See
[Why this rewrite](#why-this-rewrite) below.

---

## Architecture

```
                       ┌──────────────────────┐
                       │   wolf.app.build_     │  composition root
                       │   application()       │  (all wiring lives here)
                       └──────────┬───────────┘
              ┌───────────────────┼────────────────────┐
              ▼                   ▼                     ▼
        ┌───────────┐      ┌────────────┐        ┌─────────────┐
        │ Screener  │─────▶│  Tracker   │◀──────▶│  StateStore │  atomic + locked
        │ (detect)  │      │ (lifecycle)│        │  JSON store │
        └─────┬─────┘      └─────┬──────┘        └─────────────┘
              │                  │
              ▼                  ▼
        ┌───────────┐      ┌────────────┐
        │ Detectors │      │ Telegram   │  notifications
        │ (momentum)│      │ Notifier   │
        └───────────┘      └────────────┘
              ▲                  ▲
        ┌─────┴──────────────────┴─────┐
        │       BinanceClient          │  market data (narrow error handling)
        └──────────────────────────────┘

   Two entrypoints share the same Application object:
   • wolf.main      — worker: APScheduler jobs + uvicorn API
   • wolf.api       — FastAPI app (importable for tests / ASGI servers)
```

### Package layout

| Module | Responsibility |
|--------|----------------|
| `wolf/config.py` | Immutable `Settings` loaded from env — **no globals** |
| `wolf/models.py` | Typed `Signal`/`Candle`/`Status` dataclasses & enums |
| `wolf/state/store.py` | Atomic, thread-safe JSON store (the **only** persistence layer) |
| `wolf/exchange/` | Multi-exchange data layer — Binance/OKX/Bybit sources + fallback client |
| `wolf/indicators.py` | Pure indicator functions (RSI, ATR, EMA, MACD, Bollinger…) |
| `wolf/structure.py` | Price-action helpers (swing points, liquidity sweep, RSI divergence) |
| `wolf/detectors/` | One detector per module (`momentum`, `prepump`, `predump`, `scalp`, `swing`) |
| `wolf/market.py` | Futures market context (funding rate, open interest) + provider |
| `wolf/ai/` | AI debate layer + LLM clients (Anthropic / DeepSeek / Groq) |
| `wolf/flow/` | Flow-intelligence data (CoinGecko + DefiLlama) + framework-filter brief |
| `wolf/reports/flow.py` | Nansen-style flow-intelligence thread → News topic |
| `wolf/tracker.py` | Signal lifecycle engine + stats — the core |
| `wolf/notify/telegram.py` | Telegram notifier + message builders |
| `wolf/screener.py` | Thin orchestration (replaces the old 11k-line hub) |
| `wolf/scheduler.py` | APScheduler jobs (track + scan) |
| `wolf/api/app.py` | FastAPI REST API |
| `wolf/main.py` | Worker entrypoint |

---

## Detectors

Each detector implements the `Detector` contract (`evaluate(symbol, candles) ->
SignalCandidate | None`) in its own module. The screener runs them all and keeps
the highest-scoring candidate per symbol. Scoring/thresholds follow the original
bot's design, re-expressed on a single candle series so each detector is pure
and unit-tested.

| Detector | Bias | Trigger | Threshold |
|----------|------|---------|-----------|
| `MOMENTUM` | both | Range breakout + RSI/MACD/volume confirmation | ≥65 |
| `PREPUMP` | LONG | Bollinger squeeze + volume coil + momentum (pre-breakout accumulation) | ≥65 |
| `PREDUMP` | SHORT | Bearish RSI divergence + over-extension + rejection (distribution) | ≥65 |
| `SCALP` | both | Liquidity sweep (stop-hunt) + volume spike + RSI extreme | ≥60 |
| `SWING` | both | Trend (EMA align) + pullback to EMA20 + rejection candle | ≥65 |

Add a detector by writing one module and appending it to `default_detectors()`
in `wolf/detectors/__init__.py` — nothing else changes.

`PREPUMP`/`PREDUMP` additionally consume an optional **market context**
(`wolf/market.py`) carrying the funding rate and open-interest momentum from
Binance futures: negative/extreme funding boosts a PREPUMP short-squeeze case,
overheated positive funding boosts a PREDUMP. The bonus is purely additive, so
detectors still work candle-only when futures data is unavailable.

## Data sources (multi-exchange fallback)

Market data is fetched through a `MarketDataClient` that tries an ordered list of
exchange sources and serves from the first that responds — resilient to a venue
being geo-blocked or down (the same role the old bot's `exchange_resolver`
played). The winning source per symbol is cached so later cycles skip dead
venues. Order is configurable via `EXCHANGES` (default `binance,okx,bybit,gate`).

```
get_klines(BTCUSDT) ─► Binance ─(403/empty)─► OKX ─(ok)─► candles   [cache: OKX]
```

Each venue lives in its own module (`wolf/exchange/sources.py`) and normalises
its symbol format (`BTCUSDT` ↔ `BTC-USDT` ↔ `BTC_USDT`), interval codes
(`15m` ↔ `1H`/`15`) and JSON payload into the common `Candle` type.

**Funding rate** is itself multi-venue (`wolf/exchange/derivatives.py`): the
client falls back across Binance → OKX → Bybit so the PREPUMP/PREDUMP funding
signal survives one venue being blocked. Open-interest change stays Binance-
specific. When no funding/OI is available, those detectors degrade to
candle-only.

## AI debate layer

Optional and **off by default** (`AI_DEBATE_ENABLED=true` to enable). When on,
the screener runs the single best candidate per symbol through a three-step
debate before recording it:

1. **Bull** argues for the trade.
2. **Bear** argues against it.
3. **Arbiter** returns a structured verdict — `CONFIRM` / `NEUTRAL` / `REJECT`
   with a confidence (0-100) and one-line rationale.

A `REJECT` at or above `AI_VETO_MIN_CONFIDENCE` (default 70) vetoes the signal;
otherwise the rationale is attached to the signal's reasons. The layer is
provider-agnostic (`wolf/ai/base.py`) with an Anthropic implementation
(`claude-opus-4-8`, adaptive thinking, structured-output verdicts via the
official `anthropic` SDK). With no API key it degrades to an `ABSTAIN` verdict
that never blocks a signal, so the bot runs unchanged with the AI layer off.

## Telegram topics

Messages route to forum topics with graceful fallback (own topic → a more
general one → the main channel), so nothing is dropped when only some topics are
configured:

| Telegram topic | Env var | Content | Enable |
|----------------|---------|---------|--------|
| ‼️ New Signal | `NEW_SIGNAL_THREAD_ID` | new signal alerts | always |
| ⭐ Signal Entry | `SIGNAL_THREAD_ID` | entry touched + TP hits | always |
| 📝 Trade Reports | `TRADE_REPORT_THREAD_ID` | win/loss resolutions | always |
| 📚 Market Update | `MARKET_UPDATE_THREAD_ID` | BTC/ETH bias pulse | `MARKET_PULSE_ENABLED` |
| 🔥 Hot Ecosystem | `RADAR_THREAD_ID` | market radar (gainers/losers/volume) | `RADAR_ENABLED` |
| 👁 Whale Report | `WHALE_THREAD_ID` | large trades | `WHALE_ENABLED` |
| 🐝 BTC/ETH/SOL | `MAJORS_THREAD_ID` | majors session report | `MAJORS_ENABLED` |
| 🗞 News Update | `NEWS_THREAD_ID` | crypto headlines | `NEWS_ENABLED` |
| System / Stats | `SYSTEM_THREAD_ID` / `STATS_THREAD_ID` | startup + performance | always |

Timestamps render in `TIMEZONE` (default `Asia/Jakarta` → WIB). The bot sends a
startup "ONLINE" message on boot, and Telegram API errors are logged with their
description (e.g. "message thread not found") so a misconfigured chat/topic is
obvious in the logs.

## Market reports & news

Periodic reports each post to their own topic and are **opt-in**:

* **Majors** (`MAJORS_ENABLED`) — BTC/ETH/SOL price + 24h snapshot, one API call.
* **Radar** (`RADAR_ENABLED`) — top gainers/losers/volume from one all-symbols
  24h call (no per-symbol fan-out, so it's rate-limit friendly).
* **Market pulse** (`MARKET_PULSE_ENABLED`) — BTC/ETH trend + RSI bias.
* **Whale** (`WHALE_ENABLED`) — large public trades above `WHALE_MIN_USD`,
  de-duplicated via the state store (REST only, no key, no WebSocket).
* **News** (`NEWS_ENABLED`) — CryptoCompare headlines (free, key-less),
  de-duplicated so the same story isn't reposted.
* **Flow Intelligence** (`FLOW_ENABLED`) — a Nansen-style "flow" thread posted to
  the News topic: BTC/market posture → stablecoin dry powder → chain rotation →
  token picks/skips → conclusion + strategy. Built from **free** data
  (CoinGecko for FDV/MC, market cap & turnover; DefiLlama for per-chain DEX
  volume & stablecoin supply). A deterministic *framework filter*
  (`wolf/flow/brief.py`) selects picks (low FDV/MC unlock pressure, healthy
  turnover, not already pumped, no wash-trading) and explains every skip. An LLM
  **narrator** (`FLOW_NARRATOR_PROVIDER` = `deepseek` | `groq` | `gemini` |
  `anthropic`) phrases the brief in the thread style; **without an API key it
  falls back to a built-in template**, so it always works. The narrator only
  ever phrases the computed numbers — it never invents wallet-level metrics.

Each is a small module that never touches the signal pipeline and degrades to
nothing if its data is unavailable.

## Signal lifecycle

```
PENDING ──(price touches entry)──▶ ACTIVE ──(TP rungs)──▶ TP_HIT
   │                                  │
   │                                  └──(stop)─────────▶ SL_HIT
   │                                  └──(timeout, +PnL)─▶ EXPIRED_WIN
   │                                  └──(timeout, -PnL)─▶ EXPIRED_LOSS
   └──(entry never touched, timeout)──────────────────────▶ INVALIDATED
```

* **TP ladder** — multiple take-profits; the stop moves to **breakeven** after TP1.
* **Entry modes** — `MOMENTUM_NOW` (active immediately) or `RETEST_WAIT`
  (active only once price revisits the entry zone).
* **Conservative evaluation** — within a candle, the stop is checked before TPs.

---

## Quickstart

```bash
# 1. Install
pip install -r requirements-dev.txt

# 2. Configure
cp .env.example .env      # fill in TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

# 3. Run tests
pytest

# 4. Run the worker (scheduler + API)
python -m wolf.main
```

The API is then available at `http://localhost:8000` (interactive docs at
`/docs`).

---

## REST API

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/health` | Liveness + redacted config |
| `GET`  | `/signals/active` | Currently pending/active signals |
| `GET`  | `/signals/outcomes?limit=50` | Resolved outcomes (newest first) |
| `GET`  | `/stats` | Win-rate / PnL aggregates (incl. per-strategy) |
| `POST` | `/scan` | Run one screening cycle now |
| `POST` | `/track` | Advance pending signals now |
| `POST` | `/signals` | Record a signal manually (external strategies) |

Example — record a signal from an external strategy:

```bash
curl -X POST localhost:8000/signals -H 'Content-Type: application/json' -d '{
  "symbol": "BTCUSDT", "direction": "LONG",
  "entry_price": 65000, "tp": 68000, "sl": 63500,
  "strategy": "MANUAL", "score": 80,
  "tps": [{"level": 1, "price": 66500}, {"level": 2, "price": 68000}]
}'
```

---

## Configuration

All configuration is via environment variables (see `.env.example`). Variable
names match the previous deployment, so an existing Railway / `.env` setup works
unchanged. Key knobs:

| Variable | Default | Meaning |
|----------|---------|---------|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | – | Telegram credentials |
| `SCREENER_INTERVAL_MIN` | `10` | Minutes between screening cycles |
| `TRACKER_INTERVAL_MIN` | `5` | Minutes between tracking passes |
| `TRACKER_DEDUP_MINUTES` | `30` | Suppress duplicate symbol+direction |
| `STATE_DIR` | `state_data` | Where JSON state is persisted |
| `API_PORT` | `8000` | REST API port |
| `API_KEY` | _(empty)_ | If set, `POST` endpoints require it in `X-API-Key` |
| `AI_DEBATE_ENABLED` | `false` | Enable the Bull/Bear/arbiter AI layer |
| `CLAUDE_MODEL` | `claude-opus-4-8` | Model for the AI arbiter |
| `AI_VETO_MIN_CONFIDENCE` | `70` | Min `REJECT` confidence to veto a signal |

---

## Why this rewrite

The previous bot was a mature project but had five issues that hurt
maintainability. Each is addressed structurally here:

| # | Old problem | Fix in Wolf |
|---|-------------|-------------|
| 1 | 11k-line monolithic `crypto_screening_bot_v13.py` | Small, single-responsibility modules; detectors split one-per-file |
| 2 | 350+ broad `except:` swallowing real bugs | Narrow exceptions (`requests.RequestException`, `KeyError`…) + `log.exception` everywhere |
| 3 | ~30 JSON files written ad-hoc from many call sites | One `StateStore` with **atomic writes + per-key locks** |
| 4 | 10+ module-level `global` statements | Immutable `Settings` + dependency injection; zero globals |
| 5 | Debug junk files committed (`r.json`, `response.json`…) | Clean tree + comprehensive `.gitignore` |

---

## Deployment

Runs as a single long-lived worker process:

* **Railway** — `railway.toml` (nixpacks, Python 3.11, `python -m wolf.main`)
* **Heroku-style** — `Procfile` (`worker: python -m wolf.main`)

State is persisted to `STATE_DIR`. On platforms with ephemeral filesystems,
mount a volume there (or wire the `StateStore` to a database — it is the single
swap point).

---

## License

MIT
