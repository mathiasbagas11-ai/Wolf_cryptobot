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
| `wolf/market.py` | Per-symbol context (funding, OI, on-chain, whale, premium) + provider |
| `wolf/ai/` | AI debate layer + LLM clients (Anthropic / DeepSeek / Groq) |
| `wolf/flow/` | Shared market-data clients (CoinGecko, DefiLlama, Hyperliquid, sentiment) |
| `wolf/onchain/` | Collectors: valuation, whale coordination, Coinbase premium, macro |
| `wolf/reports/flow.py` | Flow Intelligence digest (reads collector snapshots) → own topic |
| `wolf/reports/deepdive.py` | On-demand single-token bull-vs-bear card |
| `wolf/reports/whale_alert.py` | Whale-coordination event alert → Whale Report topic |
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
| `MOMENTUM` | both | Range breakout + RSI/MACD/volume confirmation | ≥80 |
| `PREPUMP` | LONG | Coil release (breakout bar ≥2.5× the base's range) + volume + momentum, near VWAP | ≥65 |
| `PREDUMP` | SHORT | Bearish RSI divergence + over-extension + rejection (distribution) | ≥65 |
| `SCALP` | both | Liquidity sweep (stop-hunt) + volume spike + RSI extreme | ≥65 |
| `SWING` | both | Trend (EMA align) + pullback to EMA20 + rejection candle | ≥80 |
| `TRAP` | both | Failed-breakout reversal: sweep + reclaim + volume climax + VWAP grab + exhaustion (anti exit-liquidity) | ≥80 (HIGH only) |

Add a detector by writing one module and appending it to `default_detectors()`
in `wolf/detectors/__init__.py` — nothing else changes.

`PREPUMP`/`PREDUMP` additionally consume an optional **market context**
(`wolf/market.py`) carrying the funding rate and open-interest momentum from
Binance futures: negative/extreme funding boosts a PREPUMP short-squeeze case,
overheated positive funding boosts a PREDUMP. The bonus is purely additive, so
detectors still work candle-only when futures data is unavailable.

### When a detector's own gates contradict its scoring

`PREPUMP` emitted nothing for months, and not because the market lacked the
setup. Its mandatory breakout gate — close above the prior 10-bar high, on a
bullish bar — could not be true at the same time as several of the components it
was graded on:

| Award | Required | Why the gate forbade it |
|---|---|---|
| VWAP discount, 15 | `price <= vwap` | The gate demands a close above the base *and* above EMA50; VWAP is back in the base |
| Quiet accumulation, 12 | volume **not** expanding | The volume-coil award on the same bar requires that it is |
| RSI 50-68, 20 | a calm bar | A close above a 10-bar high on 1.8× volume prints RSI in the seventies |
| Bullish divergence, 15 | a lower low | A breakout is a higher high |

Replaying a real base shape through it put the honest ceiling on a breakout bar
at **73** against a threshold of **78**. The detector was not mistuned; it was
unsatisfiable, and the difference matters because no amount of market activity
would have produced a signal.

The unit test stayed green throughout because its fixture used an 18-bar base —
short enough that the Bollinger width was still falling monotonically into the
breakout and an FvG from the preceding ramp was still inside the 50-bar lookback.
Stretching that same fixture to a realistic 60+ bars drops it to 73 and it goes
silent. `test_prepump_fires_on_a_realistically_long_base` is that case, and
`test_prepump_scoring_is_reachable_on_the_bar_its_gate_requires` asserts the
arithmetic directly, so an unsatisfiable threshold fails loudly instead of
quietly emitting nothing.

The fix rebalanced the budget onto things a breakout bar can actually be — value
*proximity* to VWAP rather than a discount, RSI 50-80, no reward for absent
volume — and moved the hard decision off Bollinger compression. Inside a base
occupying most of the 150 candles on hand there is no longer history to call the
base "compressed" against, so any percentile cut passes that fraction of its bars
by construction. What separates a coil releasing from an ordinary breakout is
measurable without that contrast: **the breakout bar's range against the range of
the bars it broke out of**. A base is quiet by definition, so a genuine release
prints a bar several times anything inside it. Compression is still scored, where
a coin flip costs points instead of the whole signal.

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

### The chase gate, and why it is recorded rather than widened

`_reprice_at_market` drops a market entry that has already run `MAX_CHASE_R`
past the price the detector quoted. That drop happens before `record_signal`, so
what it rejects appears in no count, no bucket and no strategy row on the diag
card — a gate rejecting most of a strategy's setups and a gate that never fires
looked identical. Every drop is now persisted to `chase_drops` and the card
reports the count, the split by strategy, and how far past the quote price had
gone. Behaviour is unchanged; only the gate's output became visible.

Widening the limit is the tempting fix and the wrong one. The stop does not move
with the re-quote, so a chased entry stretches the risk unit, and the ladder —
rebuilt at the same R multiples — then demands a far larger absolute move for the
same nominal 3R:

| Chase | Entry | 1R | Move to the last rung | Loss at stop |
|---|---|---|---|---|
| 0.2R | 101 | 5.9% | +17.8% | −5.9% |
| 0.6R | 103 | 7.8% | +23.3% | −7.8% |
| 1.5R | 107.5 | 11.6% | **+34.9%** | −11.6% |

Neither ratio gate objects: nominal R:R is unchanged, and `MAX_COST_R` compares a
fixed cost against the risk unit, so a *bigger* 1R passes it more easily. Both
gates are weakest exactly where the trade is worst, which is why the limit needs
to be argued from recorded drops rather than from a number picked off a chart.

### A detector that cannot be called looks like a detector being strict

`TRAP.evaluate` was written taking `(symbol, candles, context)` and never grew
the `features` parameter the screener started passing on 2026-08-20. Every
invocation raised `TypeError`, which `Screener._best_candidate` catches
alongside genuine detector faults and steps over. TRAP emitted nothing for
nineteen days and sixty-five commits — through every era boundary the project
records, so it contributed zero signals to every measured era.

Two things kept it hidden. Its own docstring pre-explains the silence
("deliberately strict — threshold 80, HIGH conviction only"), so zero signals
read as working as designed. And the tests were green from both sides: every
TRAP test called `evaluate` with two arguments, while every fake detector in
the screener tests carried the correct four-argument signature. Both halves
passed; the pairing was broken.

`test_every_detector_accepts_the_call_the_screener_actually_makes` and
`test_no_detector_crashes_through_the_screener` close that gap by invoking each
*real* detector the way production does — the second through the screener
itself, so a future drift cannot hide behind its exception handler.

### When the gates decide and the threshold does not

Two detectors were audited for the reverse of PREPUMP's problem — not a
threshold nothing can reach, but one nothing can miss.

`MOMENTUM`'s gates guarantee `breakout` (35), `macd_confirms` (20) and at least
the lower volume tier (10), and `vwap_aligned` (20) fired on **100%** of 7020
bars swept: a close beyond a 50-candle extreme is above the 40-candle VWAP in
all but contrived cases. That is a floor of **85** against a threshold of
**80** — lowest score observed exactly 85, median 100. Every bar clearing the
gates becomes a signal, and `confluence_level` (HIGH at ≥85) is therefore always
HIGH.

It also paid 15 points for an FvG-launch test that could never be satisfied.
The test asked whether the 50-candle extreme the breakout had just cleared sat
inside a Fair Value Gap drawn from the last 40 candles — but the windows
overlap, so a bull gap's lower edge is `c1.high` for a candle inside the
breakout window, and that window's minimum low is at or below it by
construction. Zero hits across 3738 breakout bars, never within 0.43 of an
edge. Removed: 15 points that never arrived, so no decision changes, and it had
also been claimed as a `primary_component`, which rested the `PRIMARY` label on
a test that could not fire.

`not_overextended` (5) is dead in practice for the same family of reasons — it
wants RSI under 75 on a long, and the calmest breakout swept printed 77, median
98 — but it is not *forbidden*, so it stays. Neither it nor `vwap_aligned` is
provable the way PREDUMP's `atr/price` was: VWAP(40) includes the breakout
candle, whose typical price can exceed its close on a long upper wick with
dominant volume. Only a provable constant can become a gate with provably
identical decisions.

Two things follow and neither is acted on, because both are strategy changes
with no trade behind them. Raising the threshold above the floor would make the
score mean something again. And `Screener._best_candidate` picks one candidate
per symbol with `max(score)` across floors of 0 (`PREPUMP`, `PREDUMP`), 20
(`SCALP`), 22 (`TRAP`), 55 (`SWING`) and 85 (`MOMENTUM`) — so MOMENTUM takes a
contested symbol on its scoring floor rather than on the strength of the read.

`SWING` came out the healthiest of the six: a floor of 55 against a threshold
of 80, every one of its nine components observed firing, and a score that
genuinely rejects.

### Grading what the gate threw away

Recording the chase drops made the gate visible; it did not make it judged. A
count says how often the gate fires and says nothing about whether what it
rejected would have paid — and without that, the limit can only be argued from
replayed candle shapes, which is how 1.5R was picked and then withdrawn.

`wolf/chase_audit.py` closes that loop. Each recorded drop is reconstituted as
the signal the screener *would* have written had the gate not fired, and
replayed through the same evaluator that grades real trades, over the candles
that actually followed. A re-quote only ever moves one of three things, which
is why the record is enough:

| | |
|---|---|
| **entry** | becomes the live price — the drop is decided *after* it is read |
| **stop** | stays put; it marks a level, which is why chasing stretches the risk unit |
| **ladder** | rebuilt on that stretched unit at the R multiples the policy sets |

`entry_quoted_live` is set on the rebuild, which is not cosmetic: it tells the
replay the entry was priced at the drop instant rather than at an earlier bar
close, so the gap between them is neither credited to the entry nor hidden from
the stop — on exactly the fast moves the gate fires on.

Read the **distance split** first. It compares drops that ran far past the
quote against drops that ran less far, within one population, and can argue the
limit without a second sample. The **`vs taken`** line compares drops against
the trades the bot took, which are different setups — so it is a level question,
priced as one (Welch, charged the live book's `mean_open`), and it will say
nothing for a long time. Unresolved drops are reported separately and never
folded in silently: one whose history ran out is marked to the last close,
which is a guess about a position that was never closed.

**Graded on a schedule, not on demand.** This was got wrong first and the
mistake is worth keeping: the audit originally replayed every drop when asked,
and its first live run lost 21 of 48 to "no history reaching back". That is
structural. The replay needs 15m candles from now back to the drop, so the bars
required grow with every hour that passes, and a venue serving fewer than that
returns a window opening after the drop. Skips therefore correlate with age and
with which venue serves the symbol — the survivors lean toward recent drops on
liquid pairs, and a 44% loss on a non-random criterion can manufacture the very
effect the audit exists to detect.

So an hourly job grades each drop exactly once, between its strategy's timeout
(long enough to have settled) and a 60h ceiling — 240 bars, comfortably inside
what every venue in the fallback chain serves. The verdict is written onto the
record and never recomputed, so the sample accumulates instead of decaying,
which is how the tracker has always graded real signals. The audit reads stored
verdicts first and replays only what is still ungraded.

Skips name their own fault, because one number covering three of them is what
let the original loss read as incidental:

| | |
|---|---|
| `too_old` | aged out before the grader reached it — history can no longer be trusted |
| `no_data` | young enough, but no venue served candles for the symbol |
| `unbuildable` | the record cannot be turned into a signal (inverted geometry, missing stop) |

```
/whatif chase          # Telegram
GET /whatif/chase      # reads stored verdicts; replays only what is ungraded
CHASE_GRADE_MAX_AGE_H=60      CHASE_GRADE_INTERVAL_MIN=60
```

### The evidence bucket — what a score was made of

A total says nothing about its composition, and the components behind one are
not interchangeable. `PREDUMP` reaches its threshold from a bearish RSI
divergence — the tell its own module calls the strongest — and equally from a
stack of awards that are close to coin-flips in any uptrend. Measured across
1320 bars of randomly-shaped markets:

| Component | Points | Fires on |
|---|---|---|
| `atr/price < 0.1` (was an award) | 5 | **100.0%** — a constant, now a hard gate |
| `vwap_premium` | 15 | 50.3% |
| `bear_fvg_above` | 10 | 49.5% |
| `overbought_at_high` | 25 | 35.6% |
| `volume_fading` | 15 | 32.7% |
| `bearish_rejection` | 20 | 7.3% |
| **`bear_divergence`** | 35 | **2.5%** |

So roughly 23 of the 65 points needed can arrive from two near-coin-flips that
say nothing about distribution, and 27% of the signals in that sweep carried
neither a divergence nor a rejection candle — a composition that reads as a
*healthy uptrend* rather than a top.

PREDUMP is not alone in this. Counting only bars that pass each detector's own
hard gate, SCALP's `sweep` lands on 100% and its `vwap` award on 95.0% (a
bullish sweep wicks *below* recent lows, which is below fair value almost by
construction) against `order_block` on 0.3%; TRAP's `rejection_wick` lands on
86.4% because the gate already demands the reclaim that makes the wick
dominant. SWING awards 55 of its 80 for conditions it has already gated, and
MOMENTUM 55 of its 65.

Whether any of that costs anything is not yet known, and reweighting awards on
the strength of a sweep would be acting on a number no trade has earned. Unlike
PREDUMP's `atr/price`, none of these is a clean constant either, so none can be
converted to a gate with provably identical decisions. Instead all six
detectors record `score_parts` on the signal, and the diagnostic carries an
`evidence` dimension splitting each strategy in two:

```
evidence:PRIMARY       n=6 graded=6 wr=100.0 meanR=+1.400 t=+1.87 padj=0.723
evidence:THIN          n=6 graded=6 wr=0.0   meanR=-1.000 t=-1.33 padj=0.723
```

`PRIMARY` carried the detector's own `primary_components`; `THIN` cleared the
threshold on context alone. `UNRECORDED` is a third label for signals written
before the composition was persisted — missing data, never folded into `THIN`.
The comparison is paired *inside* a strategy, so the market move largely
cancels, which is why it can resolve on a fraction of the sample a level
question needs. It is deliberately two labels and not one per component:
every bucket enters the same BH-FDR family, so cardinality is paid for by
every other dimension on the card.

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

### When the AI abstains, which fault is it?

Every failure in the debate layer degrades to `ABSTAIN` so it can never break
screening — which also means it can never be *seen*, because an abstention looks
identical to an AI with no opinion. The digest therefore prints the provider's
own sentence, and the two budget faults are worded apart on purpose:

| Abstain reason | What happened | Remedy |
|---|---|---|
| `NO_ARBITER` | no usable client for the arbiter role | set the provider's API key |
| `ERROR` | a role raised — network, auth, SDK | provider-side |
| `NO_JSON: spent all N tokens on reasoning` | the model reasoned until the budget ran out and never wrote an answer | a different model, or a bigger budget |
| `NO_JSON: answer cut off at max_tokens=N` | it started answering and was truncated | a bigger budget |

**The commonest cause here was not a broken key.** Since the V4 rename every
DeepSeek model name reasons by default at high effort — `deepseek-v4-flash` is
the fast *name*, not a non-thinking model, and there is no non-thinking name to
switch to. Left on, the model spent the whole budget reasoning and returned
empty content on 54% of one day's signals. `AI_THINKING=disabled` (the default)
sends the provider's switch to turn it off; the field is sent only to the
provider that owns it, because an unknown top-level field is ignored by some
vendors and rejected by others. Set `AI_THINKING=enabled` to send nothing and
fall back to the provider's own default — the way out if a vendor renames it,
since a rejected field fails every call rather than half of them.

A role can also fail *without* abstaining. An empty bull or bear argument is not
an error on any path: it becomes `(none)` in the arbiter's prompt, the verdict
still comes back, and the card reports a healthy debate that was in fact the
arbiter talking to itself. Nothing downstream can tell the two apart, so the
self-test probes all three roles and any that answers with nothing joins
`degraded_roles` — the list `/ai` and the boot log already print — carrying the
provider's own explanation beside it. Naming the role without the fault is only
half a diagnosis: a rate limit, a spent balance, a budget eaten by reasoning and
a model that replied with whitespace all read as "bear is quiet", and none of
the four share a remedy.

The knobs, all env vars:

| Variable | Default | |
|---|---|---|
| `DEBATE_ARBITER_PROVIDER` | `deepseek` | `deepseek`, `groq`, `hermes`/`openrouter`, `anthropic` |
| `DEBATE_ARBITER_MODEL` | `deepseek-v4-flash` | OpenRouter needs `vendor/model`; every other provider needs a bare name, and a mismatch is named at boot |
| `DEBATE_ARBITER_MAX_TOKENS` | `2048` | sized for whatever the model does *before* the verdict, not for the verdict |
| `AI_THINKING` | `disabled` | applies to all three roles; `enabled` sends nothing and restores the provider default |

`/ai` fires one live call through the same path and reports what came back, so a
change is confirmed in seconds rather than in tomorrow's digest — a broken layer
abstains rather than failing, so without the probe a fix cannot be verified until
the abstentions it caused have aged out of the window.

### Whale-veto policies, and the trades that are not there

`/whatif whale` scores each veto policy over the recorded book: what it would
have kept, what that book's mean and net R would have been, and — for each
policy — Welch's two-sample t between the trades it kept and the trades it
would have dropped.

**It is not the ladder card with different rows.** A geometry re-cut re-scores
*the same trade*: every variant holds every signal, the market move is common
to both columns and cancels, and a paired t is right. A veto does not re-score
anything — it decides which trades exist, so two policies hold different
subsets, nothing cancels, and a paired statistic would claim a pairing that
isn't there. Policies selecting identical trades collapse into one row, because
two names for one test would report a finding twice and enter it into the
correction twice.

**The gap is charged for overlap.** Positions held side by side in a
correlated market are not independent samples, so `mean_gap` scales the
standard error by `sqrt(mean_open)` and divides the degrees of freedom by it —
the same worst case `eff_n_floor` reports. It belongs in the statistic rather
than beside it: a card printing the discount on one line and an undiscounted t
on the next invites the reader to believe the wrong one, and the wrong one is
always the more exciting. On the live book that moved a `drop WITH` contrast
from t=3.4 to t=1.9. The undiscounted figure travels as `nom_t`, so the size of
the discount is visible rather than taken on trust.

**And the gap is recomputed inside each strategy**, because a one-dimensional
split cannot tell a whale effect from a bookkeeping artefact: if the strategies
that lose are also the ones whales agree with, "trading with whales loses" is
those strategies losing, re-labelled. A real effect survives inside the
strategies; a mix artefact appears across them and vanishes within. The card
says how many strategies reproduced the pooled gap.

**And the decisive evidence is missing by construction.**
`Screener._whale_vetoed` runs before `record_signal`, so a candidate the live
veto rejected never became a signal, never got an outcome, and cannot appear on
this card. Every policy is scored only on trades the current veto already
allowed. That is precisely the self-blinding the AI layer was kept out of the
signal path to avoid — arriving through a gate that was never held to the same
rule. The card prints the caveat every time, because the reading the numbers
most invite ("the veto is filtering the wrong side") is exactly the one they
cannot support.

### The backtest runs the bot that is actually deployed

`wolf/backtest/engine.py` replays the live detectors over history — under the
live `MAX_COST_R` gate. That gate is not decoration: on the live ledger it
removed roughly half the daily volume and nearly all of SCALP, by refusing
setups whose stop is too tight to survive their own round trip.

An ungated backtest therefore reports on a strategy nobody runs, and **depth
makes that worse rather than better**: the deeper the history, the more of the
rejected population it accumulates. So the gate is applied inside the engine
with the same arithmetic as `Screener._too_expensive`, a test pins the two
against each other so they cannot drift, and the run reports how many
candidates were refused — a run returning half as many trades has either found
less or been allowed less, and those are opposite conclusions from one number.

Depth is nearly free here, because one klines request already returns up to
1000 bars: a deeper walk costs CPU, not API budget. `candle_limit` is clamped
to 1000 because no venue in this codebase paginates, and asking for more would
silently return 1000 while the extra `lookback` walked off the front of the
history it was given.

`/whatif` history is deliberately **not** deepened. Its reach is 1000 × 15m ≈
10.4 days, which is why it scores only the recent part of the ledger — and that
window rolls the pre-gate era out of the sample on its own, which is the
behaviour you want when the gate changed which trades exist.

### What the cost figures do and do not contain

Two costs are reported side by side, and they are **not the same sum**:

| Figure | Contains | Source |
|---|---|---|
| `cost` / `assumed` | taker both sides **+ slippage** | `ROUND_TRIP_COST_BPS`, a constant |
| `spread` / `measured` | taker both sides **+ recorded spread** | `TAKER_FEE_BPS` + `/ticker/bookTicker` per signal |

The measured figure has no slippage term at all, because the ledger is paper:
no order is ever filled, so there is no realised price to compare a quote
against. At ordinary spreads it therefore lands *below* the assumption every
time, and a reader comparing the two straight would conclude costs are cheaper
than modelled when one component is simply absent.

So the digest names each figure for what it holds — `fee+spread` rather than
`round trip` — and prints the difference as `slippage_residual_bps`:

```
spread   4.00bps median [4.00..4.00] over 4/4 signals
         + 2x5bps taker => 14.00bps fee+spread, measured cost=0.140R
         assumed 20bps => 0.200R, incl. 6.00bps slippage the book cannot price (paper ledger, no fill)
```

A **negative** residual is not a smaller allowance, it is a deficit: fees and
spread alone have overrun the constant, so `netR` is understating the cost
outright. That case is worded as such rather than as slippage.

### One card, one standard

Every `se`, `t` and `ci95` on the digest is charged for overlap. Nine positions
running through the same BTC move are not nine observations — they are one move
recorded nine times, and a standard error computed on the nominal count says
otherwise. The error is scaled by `sqrt(mean_open)` and the degrees of freedom
divided by it, which is the same worst case `eff_n_floor` reports.

This closes a real inconsistency rather than adding conservatism for its own
sake: `eff_n_floor` sat two lines below the verdict saying the sample was a
fraction of its nominal size, while every `t` above it was quoted as though the
nominal count were the evidence. On a live card that was `t=+1.46` where the
honest figure was `+0.63`. The undiscounted value prints as `nom` so the size of
the discount is visible, and `win_rate_z` is charged too — leaving one statistic
on the old standard would rebuild the whole problem.

The degrees of freedom follow the effective count, so the p-values the FDR family
is built from carry the same discount as the `t` beside them. They are not
floored at 1: a bucket whose effective sample is a single observation has
measured nothing, and `t_to_p` returning 1.0 is how that gets said.

### When a collector goes quiet

The on-chain blocks print only when a real label exists — but when none does,
the card now names the reason instead of rendering nothing. Silence reads as
"measured, nothing to report", which is the one thing it does not mean, and it
is exactly how the `onchain:` rows disappeared off a live card with no way to
tell a dead collector from one that simply found nothing to label.

Three states, three different remedies, none of them a code change: absent means
the collector never wrote, stale means it stopped, and a fresh snapshot covering
symbols the bot does not trade means the two universes have drifted apart.

A partial snapshot is reported **with its denominator and its cause**. The
valuation collector asks for the top `ONCHAIN_VALUATION_MAX_SYMBOLS` of the scan
universe and records both that count and whether a 429 cut the run short — so
"2 of 15 symbols (rate limited)" and "2 of 15 symbols" are told apart, and
neither reads as the coverage note it used to. Two symbols means nothing without
knowing how many were asked for, and a run stopped by a rate limit is a
producer-side fault, not an observation about which coins happened to trade.

### Learning records its judgment instead of enforcing it

`LearningEngine` adjusts a candidate's score after 5 trades and benches a
symbol after 8 with a win rate under 25%. The diagnostic next door reports that
28 trades at `mean_open` 5.34 cannot separate anything, and that resolving the
edge currently showing needs roughly 540. Those two facts cannot both be acted
on, so `LEARNING_HARD_BLOCK` defaults to false and the engine only records.

The arithmetic: a symbol that is a pure coin flip returns two wins or fewer
from eight trades **14.5%** of the time, so across a 30-symbol universe about
one symbol is benched every rotation on luck alone.

And a bench is an **absorbing state**. A benched symbol emits no signals, so
its record never grows, so the bench is permanent and can never be checked
against what those trades would have done — the same self-blinding the AI layer
was kept out of the signal path to avoid, arriving through a gate whose name
made it sound like the opposite. Held back and recorded, the judgment becomes
`by_learning_action`, a bucket scored like any other: *was the bench going to
be right?* now has an answer.

Monitor mode withholds the **score delta** too, which the sibling risk gates do
not. That is deliberate: score decides which detector's candidate wins a
symbol, and bounce-risk shorts must clear `bounce_min_score`, so a swing of up
to 15 points changes which signals exist. Left on, a strategy the freeze is
meant to hold still keeps drifting on samples of five — and the count toward a
stable-strategy sample would be counting something that never stopped moving.

`LEARNING_HARD_BLOCK=1` restores the old behaviour.

### Is the AI's verdict worth anything?

The debate layer runs in monitor mode: it annotates a signal and never blocks
one. That is not timidity, it is what makes the layer measurable at all. A live
veto blinds itself — the signals it drops never become outcomes, so a `REJECT`
can never be checked against what the trade would have done.

`by_ai_verdict` is the payoff. It splits the traded sample on the verdict the
layer returned and runs the same statistics as every other bucket, and
`ai:CONFIRM-REJECT` reports the gap between the two opinionated buckets with
Welch's t. The gap is the number that matters: on a system whose overall mean is
negative every bucket reads negative, because every bucket pays the same costs.

`ABSTAIN` and `NO_AI` stay their own buckets. An abstention is a failure of the
layer, never an opinion it held, and a switched-off layer never tried — folding
either into `NEUTRAL` would report a missing verdict as a considered one.

All of it joins the same Benjamini-Hochberg family as the strategy and on-chain
rows, which raises the bar every other row has to clear. That is the point: a
reader scanning the card is now scanning these rows too.

### The hypothesis registry

`wolf/hypotheses.json` records every change that has been proposed, measured and
closed out, with the evidence that settled it. `/tested` renders it in Telegram.

That work is worth almost nothing while it lives only in a conversation: a
rejected idea nobody can point at gets proposed again a week later, re-measured
on a sample that has barely moved, and rejected again — at the cost of the only
scarce resource here, which is trades.

Three properties, each ruling out an obvious alternative:

* **A file in the repository, not state.** State is wiped when the container
  restarts, which is exactly the failure this guards against.
* **Edited by commit, never at runtime.** Every status change carries an author,
  a date and a diff.
* **Every entry names its evidence.** A status with no measurement behind it is
  a rumour, and rumours get re-litigated.

`OPEN` is part of the vocabulary and is distinct from `INCONCLUSIVE`: the second
was measured and did not separate anything, the first has not been measured yet.
That difference is what decides whether spending the sample again is worth it.

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

### The mover lane

Ranking by 24h volume is a **lagging** filter, and it lags exactly where it costs
most. A coin enters the top-N by volume only *because* it already pumped — which
puts that lane in direct contradiction with the detector meant to catch a move
early. `PREPUMP` looks for quiet accumulation, low volume by definition, on a
symbol the volume lane cannot see until the accumulation is over. A token can run
+127% in a day without the bot ever having evaluated one of its candles.

A second lane runs alongside it with a *lower* liquidity floor, ranked by the size
of the 24h move rather than by volume, so a mid-cap already moving gets scanned
while the move is young instead of after it has grown into a market-wide volume
leader. Both directions qualify — a token down 20% is a candidate for the short
detectors the same way one up 20% is for the long side.

It is still reactive, and worth being clear about why: the 24h snapshot carries no
volume *baseline*, so genuine pre-move accumulation is not detectable from that one
call. What the lane buys is the difference between "after the pump" and "early in
the pump", which is where the detectors can still do something. Catching the base
itself needs a per-symbol volume history the overview does not carry.

| Env | Default | Meaning |
|-----|---------|---------|
| `UNIVERSE_MOVER_LANE` | `true` | Enable the second lane |
| `UNIVERSE_MOVER_TOP_N` | `15` | Extra symbols it may add per cycle |
| `UNIVERSE_MOVER_MIN_QUOTE_VOLUME` | `3000000` | Its own (lower) liquidity floor |
| `UNIVERSE_MOVER_MIN_CHANGE_PCT` | `8.0` | \|24h change\| needed to qualify |

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
| 🎯 High-Conviction | `HIGH_CONVICTION_THREAD_ID` | full lifecycle of TRAP (premium) signals + the AI conviction ranking; blank → normal topics | always |
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
* **Flow Intelligence** (`FLOW_ENABLED`) — a six-section digest posted to its
  **own topic** (`FLOW_THREAD_ID`): market macro → dry powder → chain rotation →
  institutional flow → whale positioning → watchlist. Give it a topic of its own
  rather than folding it into the whale room: the whale room is event-driven
  (coordination detected → alert), this is a periodic digest. Different rhythm,
  different reason to open it.

  The report **never fetches**. It renders snapshots the collectors below wrote
  to the state store, so the interval costs nothing but a message, and the digest
  and the signal gates cannot reach different conclusions from the same source.

  Four things it will not do, each of them a bug the previous version shipped:
  * **No entry calls.** It answers "which coin is worth a look", never "at what
    price do I get in" — entries, stops and targets come from the detectors and
    `build_targets()`, which read price structure. A test lowercases the whole
    output and fails on "entry", "target", "stop loss", " sl " or " tp ".
  * **No pegged or tokenized assets in the watchlist.** Stablecoins and tokenized
    stocks screen beautifully on FDV/MC ≈ 1.0x, because full circulation is
    trivially true for anything pegged. Filtered by ticker *and* by name, since
    no static list keeps up with new ones.
  * **Nothing you cannot trade.** Candidates are intersected with Wolf's exchange
    universe — a screener hit whose `get_klines()` returns `[]` is not a finding.
  * **Labels that match their numbers.** A 0.0% change reads "flat", not
    "numpuk 🔥", and a `NEUTRAL` verdict carries no execution advice.

  Stale snapshots are still shown, carrying their age (`🐋 Whale (data 45m lalu)`),
  so nothing is mistaken for live.

  A **single-token deep-dive** (`POST /flow/{symbol}`) renders an honest bull-vs-
  bear breakdown + playbook for one token: every bear point is a real red flag
  computed from the data, never softened. Fetching on demand is correct there —
  the request is the trigger. An LLM narrator (`FLOW_NARRATOR_PROVIDER`) phrases
  it, falling back to a template without a key. The digest itself is fully
  deterministic: no model ever phrases its numbers.

## AI conviction ranking (🏆 High-Conviction)

Every other room answers *"did something fire?"*. This one answers the question
that comes next: **of everything live right now, which one deserves the risk?**

Enable with `CONVICTION_RANKING_ENABLED=true`. Every `CONVICTION_INTERVAL_MIN`
(default 60) the whole live signal book — pending + active, newer than
`CONVICTION_LOOKBACK_HOURS` — goes into **one** LLM call and comes back ordered,
with a conviction score, a one-line thesis and the thing that would invalidate
each pick. The card posts to `HIGH_CONVICTION_THREAD_ID` (falling back to the
New Signal topic, never to the main channel).

This is deliberately not what the debate layer does. The debate judges each
candidate *in isolation* — "is this setup sound?" — which structurally cannot
say that setup A is a better use of the same risk than setup B. Ranking is
comparative, so it needs the whole book in one prompt.

Rules it is held to, each one a way this could otherwise lie to you:

* **It never fetches.** Candidates come from the tracker's own pending book and
  every fact comes off the recorded `Signal` — the same numbers that were true
  when the card was sent. A ranker that re-read the market would silently
  disagree with the signals it is ranking.
* **It never invents a pick.** Every id the model returns is matched back to a
  real live signal; hallucinated or repeated ids are dropped.
* **Setups it would not take are omitted, not ranked last.** A pick below
  `CONVICTION_MIN_CONVICTION` (default 60) is dropped, and a book with nothing
  worth taking produces **no message** rather than a weak leaderboard.
* **It says when the AI did not rank it.** With no usable client the picks are
  ordered by a documented heuristic (detector score adjusted for the debate
  verdict, R:R, regime/strategy flags and whale stance) and the header says
  `⚠️ AI unavailable` — the score is shown as a score, never dressed up as a
  conviction. Silent AI degradation has cost this bot real information before.
* **It shows what it passed over.** The setups considered and not picked are
  named at the bottom, so the ranking can be checked against how those trades
  actually resolved.

The same ranking is never posted twice: the ordered pick ids are remembered in
the state store, and an unchanged leaderboard is skipped. `/rank` in Telegram
(or `POST /rank`) forces a fresh one and answers in the room you asked from.

| Variable | Default | What it does |
|----------|---------|--------------|
| `CONVICTION_RANKING_ENABLED` | `false` | turn the ranking on |
| `CONVICTION_INTERVAL_MIN` | `60` | how often the book is ranked |
| `CONVICTION_MAX_PICKS` | `3` | how many picks the card shows |
| `CONVICTION_MIN_CANDIDATES` | `2` | below this there is nothing to compare — stays silent |
| `CONVICTION_MIN_CONVICTION` | `60` | picks the model believes in less than this are dropped |
| `CONVICTION_LOOKBACK_HOURS` | `12` | ignore setups the market has already accepted or refused |
| `CONVICTION_MAX_TOKENS` | `1500` | budget for the ranking call |

It reuses the arbiter's client (`DEBATE_ARBITER_PROVIDER`/`_MODEL`), so no extra
key is needed when `AI_DEBATE_ENABLED=true`. With the debate off it still runs,
heuristically, and says so.

### On-chain, whale and institutional collectors

Each fetches on its own schedule, writes a timestamped snapshot to the state
store, and knows nothing about who reads it. Two consumers read those snapshots:

```
COLLECTOR (scheduled job) → StateStore ─┬→ Flow Intelligence digest
                                        └→ per-symbol signal context → gates + AI debate
```

| Collector | Env | Interval | Writes |
|---|---|---|---|
| Valuation | `ONCHAIN_VALUATION_ENABLED` | 60m | `onchain_valuation` |
| Whale (Hyperliquid) | `WHALE_HL_ENABLED` | 10m | `whale_hyperliquid` |
| Coinbase premium | `COINBASE_PREMIUM_ENABLED` | 10m | `coinbase_premium` |
| Macro / dry powder / rotation | `FLOW_MACRO_ENABLED` | 60m | `flow_macro` |

* **Valuation** — CoinGecko markets + DefiLlama TVL → FDV ratio (unlock
  overhang), volume/market-cap turnover, distance from ATH, MCap/TVL and 30-day
  TVL trend. Hourly because a 15-symbol universe on the 10-minute scan cycle
  would be ~90 CoinGecko calls an hour uncached, and the key-less API will not
  carry it; the cache cuts that to ~15, and an HTTP 429 opens a backoff window
  rather than retrying into a longer ban.
* **Whale (Hyperliquid)** — one **global** scan per run reads the leaderboard's
  top ~30 wallets and every position they hold. It is not a per-symbol lookup:
  doing it inside the context would re-fetch an identical leaderboard once per
  scanned symbol.

  The snapshot holds **two different facts**, and keeping them apart matters:
  * `bias` — where every tracked wallet is *sitting*. Persists for as long as
    they hold. This is what the veto gate and the digest's positioning section
    read.
  * `coins` — who *opened or added* during the last scan window. An event:
    it needs `WHALE_HL_MIN_WALLETS` (default 3) distinct wallets moving the same
    way on the same coin, and it empties on the next scan. This is what fires
    the whale-room alert, with a 60-minute per-coin cooldown so a build-up
    unfolding across scans is announced once rather than every ten minutes.

  Reading `coins` where `bias` belongs is a live trap: ten minutes after a
  coordinated entry the event list is empty while the same whales are still
  holding, so anything keyed to it goes quietly blind.
* **Coinbase premium** — Coinbase BTC/USD vs Binance BTC/USDT, the
  US-institutional demand gauge. **BTC only**: for every other symbol the field
  is `None` and changes nothing. A deliberate first cut — the premium is often
  read market-wide and may earn that role here, but wiring it into every altcoin
  on day one would make its effect impossible to attribute.
* **Macro** — CoinGecko global + token screen, DefiLlama stablecoin supply and
  per-chain DEX volume. Feeds sections 1–3 and 6 of the digest.

The valuation read also appears under each watchlist entry, showing the bias
plus only what the macro screen does not already print (MCap/TVL, 30-day TVL
trend), so the two lines complement rather than repeat:

```
👀 $AAVE -1.4% 24h · turnover 15% mcap · mcap $4.00B · FDV/MC 1.1x · -45% dari ATH
   🐻 fundamental mendukung SHORT · MCap/TVL 9.40 · TVL 30h -31%
```

**Staleness is the safety property.** A snapshot older than
`ONCHAIN_STALENESS_MIN` (30m) reads as absent on the signal path, and the bot
degrades to candle-only behaviour it already handles. Gating a live signal on a
stale whale read is strictly worse than gating it on nothing. An undated or
corrupt snapshot counts as stale too.

### Measuring before gating

Only the whale veto acts on any of this. Valuation and the Coinbase premium
reach the AI debate, which is itself in monitor mode — it records a verdict and
sends the signal anyway. So today those two change *what the reasoning says*,
not *which signals fire*.

That is deliberate, and it is the same measure-then-enable path the regime and
AI flags already follow. Every signal records the on-chain context **as it stood
when it fired** (`onchain_bias`, `whale_stance`, `whale_net_wallets`,
`coinbase_premium_pct`), and the diagnostics digest buckets resolved outcomes by
each:

```
whale:WITH             n=20 graded=20 wr=70.0 meanR=+0.960 t=+3.26 => ...
whale:AGAINST          n=15 graded=15 wr=20.0 meanR=-0.440 t=-1.47 => ...
onchain:SUPPORTS_LONG  n=14 graded=14 wr=100.0 meanR=+1.800 t=+0.00 => ...
```

`whale_stance` is stored **relative to the signal's own direction** (WITH /
AGAINST), because "whales were LONG" means opposite things for a LONG and a
SHORT and bucketing on the raw side averages the effect away. `NO_DATA` stays
its own bucket: a collector that was off is not the same finding as one that
looked and saw nothing. The lines appear only once a real bucket exists, so a
deployment with the collectors off does not carry three rows of `NO_DATA`.

Promote a dimension to a gate when its buckets say so — not before.

**Whale veto gate.** A signal is dropped when whale *positioning* leans
`WHALE_VETO_MIN_WALLETS` (default 5) **net** against its direction — six longs
against one short is a net of five, while six against five is a net of one,
which is a market having a disagreement rather than whales agreeing with each
other. Because it reads standing positions, the veto holds as long as the whales
hold, not just during the scan that spotted them.

The bar is higher than the alert threshold on purpose: three wallets is worth
reporting, overriding a technical setup takes five. It runs in the screener — not
in a detector, which stays a pure function of candles plus context — and
immediately **before** the AI debate, because the gate is a free dict lookup and
the debate is three LLM calls, so a vetoed signal never costs a token.

### Two whale outputs, two topics

| Topic | What lands there | Rhythm |
|---|---|---|
| 👁 Whale Report | Large trades (existing) **+ coordination alerts** (`WHALE_HL_ALERT_ENABLED`) | Event — fires when wallets pile in |
| 🧠 Flow Intelligence | Section 5: standing positioning per coin | Periodic — every `FLOW_INTERVAL_MIN` |

Turning the alert off does not stop the scan: the snapshot still feeds the veto
gate and the digest. Only the message is optional.

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
* **PREPUMP** refuses a squeeze that releases on selling. It skips the
  directional test deliberately: a pre-pump is flat by definition, so demanding
  a price move would reject the very setup it looks for. It no longer credits
  patient bid absorption — that award required the volume *not* to be expanding
  on the same bar the breakout gate requires it to expand, so it was unreachable
  points padding a threshold nothing could clear (see below).
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
| `POST` | `/flow` | Render the Flow Intelligence digest now → its topic (503 if no collector has run) |
| `POST` | `/flow/{symbol}` | Single-token deep-dive (bull vs bear), fetched on demand |
| `POST` | `/rank` | Rank the live signal book now → High-Conviction topic (503 if nothing to rank) |

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
| `CONVICTION_RANKING_ENABLED` | `false` | AI ranking of the live signal book → High-Conviction |

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

1. **Service → Settings → Volumes → Add Volume**, mount path `/data`. (Volumes
   are not under the Variables tab; the command palette `Cmd/Ctrl+K` → *Add
   Volume* works too.)
2. **Delete the `STATE_DIR` variable.** Railway exports
   `RAILWAY_VOLUME_MOUNT_PATH` once a volume is attached, and an unset
   `STATE_DIR` adopts it automatically. This step is the one that is easy to
   miss: an explicit `STATE_DIR` always wins, so a leftover `state_data` keeps
   the bot writing into the container even with the volume mounted. Setting
   `STATE_DIR=/data/state_data` by hand works too.
3. Redeploy. The startup card reports where state landed and whether it is
   durable — `(volume)` versus a loud `EPHEMERAL` warning — and `GET /health`
   shows the resolved `state_dir` alongside `outcomes_stored`.

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
