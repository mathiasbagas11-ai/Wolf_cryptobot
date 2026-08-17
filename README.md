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
| `wolf/orderflow.py` | Candle order flow — volume pace, trade-count pace, taker bias |
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
| `TRAP` | both | Failed-breakout reversal: sweep + reclaim + volume climax + VWAP grab + exhaustion (anti exit-liquidity) | ≥80 (HIGH only) |

Add a detector by writing one module and appending it to `default_detectors()`
in `wolf/detectors/__init__.py` — nothing else changes.

`PREPUMP`/`PREDUMP` additionally consume an optional **market context**
(`wolf/market.py`) carrying the funding rate and open-interest momentum from
Binance futures: negative/extreme funding boosts a PREPUMP short-squeeze case,
overheated positive funding boosts a PREDUMP. The bonus is purely additive, so
detectors still work candle-only when futures data is unavailable.

## Risk gates

Detectors only decide *what* looks like a setup; **risk gates** (`wolf/regime.py`
+ the screener) decide whether it's actually emitted. They close the loop between
the bot's own results and its next trade. The default is **"Campur"** (hybrid):
drawdown is always a hard pause (it protects equity), while the two judgement
gates run in **monitor mode** — the signal is still emitted but flagged and
down-scored, so their win-rates can be measured before promoting either to a
hard block (`REGIME_HARD_BLOCK` / `AUTOPAUSE_HARD_BLOCK`). Configured under
`RiskSettings`:

| Gate | Default | What it does | Env |
|------|---------|--------------|-----|
| **Regime filter** | monitor | Reads a bellwether's trend (BTC, price vs EMA20/EMA50) and flags trend-following LONGs in a BEARISH market and SHORTs in a BULLISH one. Counter-trend setups (`SCALP`/`PREDUMP`/`TRAP`) are exempt — they're *meant* to fade the tape. | `REGIME_FILTER_ENABLED`, `REGIME_HARD_BLOCK`, `REGIME_SYMBOL`, `REGIME_INTERVAL` |
| **Drawdown throttle** | **hard** | Tracks the paper equity's high-water mark and pauses **all** new entries once the balance falls a set % below its peak — stops a correction from giving back realized gains. | `DRAWDOWN_PAUSE_PCT` |
| **Auto-pause** | monitor | Pauses a strategy only when it is *confidently* losing: with enough graded trades, the one-sided upper bound of its expectancy in R (`avg_r + z·se_r`) must still sit below the floor. Judging a bare average against a threshold flags noise — see below. | `AUTOPAUSE_MIN_TRADES`, `AUTOPAUSE_MIN_EXPECTANCY_R`, `AUTOPAUSE_CONFIDENCE_Z`, `AUTOPAUSE_HARD_BLOCK` |

Flagged signals carry `against_regime` / `weak_strategy` on the outcome record,
and the periodic stats card shows a **Risk-gate monitor** comparing their
win-rate to the overall — your evidence for whether to flip a gate to hard.

### Diagnostics

The stats card answers *how did it go*. `wolf/diagnose.py` answers *does that
mean anything* — four things an aggregate cannot say:

* **How noisy the average is.** `+0.085R` over 226 trades sounds like an edge;
  the same number with `se 0.094` is a coin flip. Every figure carries its
  standard error, t-statistic and 95% interval.
* **What "no edge" looks like here.** A win rate means nothing against 50%: a
  ladder with a 1.5R stop and a 3R target pays out ~25% of the time under a
  driftless walk. `no_edge_win_rate` is derived per strategy from the geometry
  those trades actually carried, and `win_rate_z` scores the gap.
* **What the trade cost.** Targets are ATR multiples, so 1R is often well under
  1% and a 20bps round trip (`ROUND_TRIP_COST_BPS`) can be a third of the risk
  unit. Expectancy is reported net.
* **How many independent observations there are.** `eff_n_floor` divides by the
  mean number of simultaneously open positions — the worst case under perfect
  correlation, so a floor rather than an estimate.

Verdicts are hard to earn: under 100 graded trades, or `|t| < 2`, the answer is
`INCONCLUSIVE` regardless of how good the sample looks. With samples this size
that is usually correct, and a diagnostic that cannot say "I don't know" is a
machine for manufacturing confidence.

`format=text` renders a fixed-shape digest, also posted to the stats topic after
each scheduled card — small enough to paste whole into an analysis:

```
WOLF-DIAG v1 | 2026-08-13T10:39:26+00:00 | window=all
sample   traded=226 graded=226 flat=0 invalid=0 unpriced=0
cost     20bps / 1R=0.50% => 0.400R
overall  meanR=+0.124 sdR=1.49 n=226 se=0.099 t=+1.26 ci95=[-0.070,+0.318]
         netR=-0.276  => INCONCLUSIVE
SWING     n=60 graded=60 wr=16.7 noedge_wr=25.0 wr_z=-1.49
          meanR=-0.058 sd=1.85 t=-0.24 ci95=[-0.525,+0.410] 1R=0.50% netR=-0.458 => INCONCLUSIVE
concur   mean_open=7.29 max_open=8 eff_n_floor=31
flags    STATE_NOT_PERSISTED AI_NEVER_DECIDES TP1_BANKS_WIN_OFF
```

### Why auto-pause gates on a confidence bound

An earlier version compared average PnL **percent** against a threshold at a
12-trade minimum. Both halves of that were wrong, and four days of live data
showed it:

* **Percent is the wrong unit.** Targets are ATR multiples, so the same −1R loss
  reads as −0.3% on a quiet coin and −3% on a volatile one. Over those four days
  the percent and R averages disagreed in *sign* on 6 of 16 strategy-days — one
  strategy showed −0.57% while sitting at **+0.27R**.
* **An average is not evidence.** At 12 trades the standard error is roughly
  0.4R, so a strategy at +0.2R and one at −0.2R are indistinguishable.

Together they made the gate flag ~78% of all signals, and the flagged ones then
*outperformed* the unflagged — an anti-predictive filter. Requiring
`avg_r + z·se_r < floor` means a strategy is paused when being wrong is
unlikely, not when the sample happens to look bad. The trade-off is patience: at
a −0.19R effect size it takes roughly 160 graded trades to trigger. That is the
honest cost of not acting on noise.

## Universe

The screener can scan a **dynamic universe** (`wolf/universe.py`): it ranks the
whole market by 24h quote volume in one API call and scans the most liquid pairs,
with the core majors always included. Liquidity is the gate, so meme coins and
other ecosystems rotate in as they heat up instead of only the same hardcoded
majors. Set `UNIVERSE_DYNAMIC=false` to scan the fixed majors list only.
Tuned via `UNIVERSE_TOP_N` and `UNIVERSE_MIN_QUOTE_VOLUME`.

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
provider-agnostic (`wolf/ai/base.py`) — Anthropic plus any OpenAI-compatible
provider (DeepSeek, Groq, Hermes/OpenRouter). With no usable client it degrades
to an `ABSTAIN` verdict that never blocks a signal, so the bot runs unchanged
with the AI layer off.

### Configuring the roles

All three roles default to **DeepSeek**, so a single `DEEPSEEK_API_KEY` runs the
whole debate. Each role can be pointed at a different provider:

| Env | Default | Key it needs |
|-----|---------|--------------|
| `DEBATE_BULL_PROVIDER` / `_MODEL` | `deepseek` / `deepseek-chat` | `DEEPSEEK_API_KEY` |
| `DEBATE_BEAR_PROVIDER` / `_MODEL` | `deepseek` / `deepseek-chat` | `DEEPSEEK_API_KEY` |
| `DEBATE_ARBITER_PROVIDER` / `_MODEL` | `deepseek` / `deepseek-chat` | `DEEPSEEK_API_KEY` |

Supported providers and their keys: `anthropic` → `ANTHROPIC_API_KEY`,
`deepseek` → `DEEPSEEK_API_KEY`, `groq` → `GROQ_API_KEY`,
`hermes`/`openrouter` → `HERMES_API_KEY`. **A provider with no matching key
silently becomes a null client**, so switching a role's provider means setting
that provider's key too.

**The arbiter is load-bearing.** It alone returns the structured verdict, so if
its client is unavailable the layer cannot decide anything — every signal
abstains. `GET /health` reports this as `ai.enabled` (intent) versus
`ai.available` (reality), with `ai.degraded_roles` naming any role running
without a client; startup logs an error when the arbiter is missing. A run of
100% `ABSTAIN` in the stats card means exactly this.

## Telegram topics

Messages route to forum topics with graceful fallback (own topic → a more
general one → the main channel), so nothing is dropped when only some topics are
configured:

| Telegram topic | Env var | Content | Enable |
|----------------|---------|---------|--------|
| ‼️ New Signal | `NEW_SIGNAL_THREAD_ID` | new signal alerts | always |
| 🎯 High-Conviction | `HIGH_CONVICTION_THREAD_ID` | full lifecycle of TRAP (premium) signals; blank → normal topics | always |
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
* **News** (`NEWS_ENABLED`) — an automatic, multi-source headline pipeline.
  Every `NEWS_INTERVAL_MIN` it fans out to all `NEWS_SOURCES` (free & key-less:
  `cryptocompare`, `reddit` via Atom RSS, `hackernews` via Algolia), isolates
  each source's failure, **dedups across sources** by normalised title, **ranks
  by engagement** (HN points/comments), and posts only genuinely-new items
  (seen-set in the state store, so nothing is reposted). With
  `NEWS_SYNTHESIS_ENABLED=true` an LLM (`NEWS_NARRATOR_PROVIDER`) condenses the
  fresh batch into a single grouped brief instead of a flat card — it only
  phrases the fetched headlines, never invents stories. Sources adapted from the
  `last30days` skill.
* **Flow Intelligence** (`FLOW_ENABLED`) — a Nansen-style "flow" thread posted to
  the News topic: BTC/market posture → stablecoin dry powder → chain rotation →
  ranked token picks → watchlist → skips → conclusion + strategy. Built from
  **free** data plus signals the bot already has:
  * CoinGecko — market cap, FDV/MC (unlock pressure), turnover, % from ATH.
  * DefiLlama — per-chain DEX volume, aggregate stablecoin supply (dry powder).
  * Exchange perps (existing `MarketDataClient`) — **funding rate** per pick
    (negative = shorts crowded → squeeze fuel = bullish).
  * **Fear & Greed Index** (alternative.me) + **Coinbase Premium** (Coinbase
    BTC/USD vs Binance BTC/USDT = US institutional demand). Extreme fear + a
    positive premium + dry powder → a *contrarian* RISK-ON read ("be greedy when
    others are fearful"). Both free & key-less, ported from the previous bot.
  * **Hyperliquid** perps — funding rate + open interest per pick from a single
    cached snapshot (`metaAndAssetCtxs`), wider alt coverage than Binance perps.

  A **single-token deep-dive** (`POST /flow/{symbol}`) renders an honest bull-vs-
  bear breakdown + playbook for one token (ENA-thread style): every bear point is
  a real red flag computed from the data, never softened. Works on demand even
  when the scheduled report is disabled.

  A deterministic *framework filter* (`wolf/flow/brief.py`) selects picks (low
  FDV/MC unlock pressure, healthy turnover, not already pumped, no wash-trading),
  ranks them by a **Quant score** (unlock pressure + cross-sectional **liquidity
  percentile** + funding tailwind), surfaces near-misses as a watchlist, and
  explains every skip. An LLM **narrator** (`FLOW_NARRATOR_PROVIDER` = `deepseek`
  | `groq` | `gemini` | `anthropic`) phrases the brief in the thread style;
  **without an API key it falls back to a built-in template**, so it always
  works. The narrator only ever phrases the computed numbers — it never invents
  wallet-level metrics (real netflow / whale wallets need a paid Nansen key).

Each is a small module that never touches the signal pipeline and degrades to
nothing if its data is unavailable.

## Order flow

Volume expansion is direction-blind. A capitulation sells as hard as a breakout
buys, so a size-only test — "volume is 2× its average, add points" — scores the
trap and the setup identically.

`wolf/orderflow.py` reads *who* was aggressive, from two fields Binance publishes
in every kline and the old `Candle` threw away:

| Metric | Definition | Reads as |
|--------|-----------|----------|
| volume pace | recent volume ÷ its own baseline pace | `1.0` = unchanged, `>1.2` hot |
| trade pace | the same ratio over **trade counts** | high with flat volume = many small fills (churn) |
| taker bias | aggressive-buy share of volume | `>0.5` buyers lifting the offer |

Detectors route their volume judgement through one gate, each reading it for
what its own setup needs:

* **MOMENTUM / PREDUMP** reject a setup the aggressive side opposes. Scored
  small on purpose — the gate's job is to reject, not to nudge borderline
  setups over the threshold.
* **PREPUMP** refuses a squeeze that releases on selling, and credits patient
  bid absorption during the coil. It skips the directional test deliberately: a
  pre-pump is flat by definition, so demanding a price move would reject the
  very setup it looks for.
* **SCALP** never vetoes. A sweep trades hard against its own eventual direction
  on the way through the level — that flush *is* the setup — so it checks the
  aggressor flip on the reclaim candle instead.

The gate judges the aggressor separately from price, which matters more than it
sounds. Requiring price *and* volume to agree is the stricter reading, but a
breakout is chosen precisely *because* price is rising, so that test could
almost never fire on one. Price ticking up while sellers hit every bid is the
distribution-into-strength that fails, and only the aggressor catches it. A
lopsided split on quiet volume is not a conflict — without participation behind
it, that is noise.

Only Binance publishes the taker split. On other venues the gate falls back to
price direction at partial credit and **never vetoes** — half of the test is a
hint, not a verdict.

---

## Timeframes — why a signal is short or long

Every distance in a setup is an ATR multiple of the series it was found on, so
the candle interval — not the ladder — decides whether a signal is a scalp or a
swing. Running every detector on 15m is what made every signal short-lived
regardless of its name:

| Interval | 1R (stop) | TP1 / TP2 / TP3 | Fees as R | Held for |
|---|---|---|---|---|
| 15m | ~0.33% | 0.3% / 0.7% / 1.0% | 0.61R | hours |
| 1h | ~0.68% | 0.7% / 1.4% / 2.0% | 0.30R | 1-2 days |
| 4h | ~1.42% | 1.4% / 2.8% / 4.3% | 0.14R | days |

Each detector therefore declares its own `timeframe`, and the screener fetches
one candle series per interval:

| Detector | Interval | Timeout | Character |
|---|---|---|---|
| `SCALP` / `TRAP` | 15m | 10h / 4h | intraday reversals — fast by design |
| `MOMENTUM` / `PREPUMP` / `PREDUMP` | 1h | 48h | 1-2 day moves |
| `SWING` | 4h | 7 days | a real swing, held for days |

Timeouts and dedup windows scale with the interval (~40 bars and ~1 bar of the
detector's own series). Capping a 4h swing at 24h is six bars — not enough for
the third rung to be reachable, which quietly turned it into a scalp with a
swing's name on it. The wider stop is also what makes the trade affordable:
fees fall from 0.61R on 15m to 0.14R on 4h.

Every signal card states its interval and expected hold, so a 4h entry is not
mistaken for something to close the same afternoon.

---

## Risk geometry — 1:3

One setting decides the ratio for every strategy. Detectors choose only where
the **stop** goes — often a structural level beyond a swept wick, not a fixed
ATR multiple — and the ladder is placed off that real distance, so widening a
stop to clear the wick widens the targets with it instead of quietly shrinking
the ratio.

```
RISK_RR_TARGET=3        entry ──1R──▶ TP1 ──2R──▶ TP2 ──3R──▶ TP3
RISK_TP_ALLOCATIONS     close 50%      close 30%    close 20%
```

`MIN_SIGNAL_RR` (2.5) drops anything materially under the policy at the
emission gate, which covers detector bugs and hand-posted API signals alike.

**The ratio is not the return.** Scaling out early caps a perfect trade at
**1.7R**, not 3R, because only the last 20% ever reaches the third rung:

```
full run   = .5×1R + .3×2R + .2×3R = 1.7R
break even = 100 / (1 + 1.7)       ≈ 37% win rate
```

Every performance summary prints that number next to the win rate achieved, so
a 40% result reads as profitable rather than as a failure — and a 30% one is not
mistaken for "almost there".

---

## Signal lifecycle

```
PENDING ──(price touches entry)──▶ ACTIVE ──(all rungs)──▶ TP_HIT
   │                                  │
   │                                  ├──(TP1 banked, then stop)─▶ TP_HIT (partial)
   │                                  ├──(stop, nothing banked)───▶ SL_HIT
   │                                  └──(timeout)─▶ EXPIRED_WIN / EXPIRED_LOSS / EXPIRED_FLAT
   └──(entry never touched, timeout)──────────────────────▶ INVALIDATED
```

* **TP ladder** — multiple take-profits; the stop moves to **breakeven** after TP1.
* **Once TP1 is banked the signal cannot become a loss** (`TRACKER_TP1_BANKS_WIN`,
  on by default). A later breakeven stop is booked as the scaled exit it is:
  half off at TP1, the rest at entry, ≈ +0.5R. At 1:3 this is the most common
  shape of a *winning* signal, so the all-or-nothing rule mis-graded most of the
  winners as losses.
* **Scaled-exit accounting** — each rung is weighted by the size closed there,
  not by an even split, since the near rung is the one price actually reaches.
  Ladders stored before allocations existed keep the even split, so recorded
  history is never retroactively re-graded.
* **Entry modes** — `MOMENTUM_NOW` (active immediately) or `RETEST_WAIT`
  (active only once price revisits the entry zone).

### When one bar hits both a TP and the stop

A candle reports its high and its low but not the order they traded in, and on a
bar wide enough to reach both, that order decides the outcome.
`INTRABAR_TP_FIRST` (default on) infers it from the bar's own direction: a bar
closing **down** printed its high first, one closing **up** printed its low
first.

```
LONG, entry 100, stop 95, TP1 105

bar 100 ▲106 ▼94 close 96   closes down → high first → TP1 fills, stop to
                             breakeven, sell-off closes the rest there  → +0.5R
bar 100 ▲106 ▼94 close 105  closes up   → low first  → stopped out       → −1.0R
```

Inferring beats fixing the order in either direction. Always assuming the stop
went first writes off a TP1 that plainly filled before the reversal; always
assuming the profit went first is worse still, because the bar that *fills* TP1
routinely dips to entry beforehand and would be closed out by the breakeven stop
it had just created. Set `INTRABAR_TP_FIRST=false` for the strictly pessimistic
reading.

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
| `GET`  | `/health` | Liveness + redacted config, resolved `state_dir`, `outcomes_stored` |
| `GET`  | `/signals/active` | Currently pending/active signals |
| `GET`  | `/signals/outcomes?limit=50` | Resolved outcomes (newest first) |
| `GET`  | `/stats` | Win-rate / PnL aggregates (incl. per-strategy) |
| `POST` | `/scan` | Run one screening cycle now |
| `POST` | `/track` | Advance pending signals now |
| `POST` | `/signals` | Record a signal manually (external strategies) |
| `GET`  | `/diagnostics?window_hours=24&format=text` | Statistics behind a verdict (see below) |
| `POST` | `/signals/outcomes/import` | Merge an exported outcome log back into state |
| `POST` | `/flow` | Build the flow-intelligence brief now → News topic |
| `POST` | `/flow/{symbol}` | Single-token contrarian deep-dive (bull vs bear) → News topic |

Example — on-demand single-token deep-dive (works even when scheduled flow is off):

```bash
curl -X POST localhost:8000/flow/ENA      # → posts an ENA deep-dive to Telegram
```

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
| `RISK_RR_TARGET` | `3` | Reward:risk of the final TP — `3` is 1:3 |
| `RISK_TP_ALLOCATIONS` | `0.5,0.3,0.2` | Position fraction closed at each rung |
| `MIN_SIGNAL_RR` | `2.5` | Signals paying less than this are never emitted |
| `TRACKER_TP1_BANKS_WIN` | `true` | A banked TP1 can no longer end as a loss |
| `INTRABAR_TP_FIRST` | `true` | Infer TP/stop order on a bar that hits both |
| `FLOW_VETO` | `true` | Reject setups the aggressive side opposes |
| `STATE_DIR` | `state_data` | Where JSON state is persisted |
| `API_PORT` | `8000` | REST API port |
| `API_KEY` | _(empty)_ | If set, `POST` endpoints require it in `X-API-Key` |
| `AI_DEBATE_ENABLED` | `false` | Enable the Bull/Bear/arbiter AI layer |
| `DEBATE_ARBITER_PROVIDER` | `deepseek` | Provider for the verdict — needs its own key |
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

### Persisting signal history (do this before it matters)

`STATE_DIR` defaults to `state_data`, a **relative** path. On Railway that
resolves inside the container filesystem, which is replaced on every deploy — so
each redeploy silently discards the accumulated outcome history. Win-rate and
expectancy then restart from zero, and a wiped log is indistinguishable from a
quiet trading week.

Both are surfaced so this is checkable rather than discovered later: startup logs
warn when `STATE_DIR` is relative, and `GET /health` reports the resolved
absolute `state_dir` alongside `outcomes_stored`.

To make history survive deploys on Railway:

1. Add a Volume to the service and mount it at `/data`.
2. Set `STATE_DIR=/data/state`.
3. Redeploy, then confirm `GET /health` shows `"state_dir": "/data/state"`.

Note that step 3 is itself a deploy, so **export first** and restore afterwards:

```bash
curl -s "$HOST/signals/outcomes?limit=5000" > outcomes-backup.json
# ...mount the volume, set STATE_DIR, redeploy...
curl -X POST "$HOST/signals/outcomes/import" \
     -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
     --data @outcomes-backup.json
```

The import merges by signal `id` and never overwrites an existing record, so
running it twice is a no-op and a stale export cannot clobber fresher outcomes.

Alternatively, wire the `StateStore` to a database — it is the single swap point.

---

## License

MIT
