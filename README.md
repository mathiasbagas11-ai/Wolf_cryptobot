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
| `wolf/exchange/binance.py` | Binance REST client with narrow error handling |
| `wolf/indicators.py` | Pure indicator functions (RSI, ATR, EMA, MACD, Bollinger…) |
| `wolf/structure.py` | Price-action helpers (swing points, liquidity sweep, RSI divergence) |
| `wolf/detectors/` | One detector per module (`momentum`, `prepump`, `predump`, `scalp`, `swing`) |
| `wolf/market.py` | Futures market context (funding rate, open interest) + provider |
| `wolf/ai/` | AI debate layer — Bull/Bear + arbiter verdict (Anthropic SDK) |
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
