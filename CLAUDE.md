# Wolf Cryptobot — working notes for Claude

A crypto signal bot on Railway. It keeps a **paper ledger**: it records
signals and grades outcomes, and it never places an order. Its real product is
not the signals — it is the diagnostic that says whether the signals are worth
anything, and which is usually allowed to answer "not yet".

**Before you finish a commit, update the Status and Broken sections below.**
They are the reason this file exists: a new session should be able to start
here instead of asking for a summary. Leave the rules and lessons alone unless
something genuinely settled them.

## The prime directive

Measure; do not act. Almost every mistake this project has made was acting on
a number that had not earned it. When a change and a measurement compete for
the same hour, the measurement wins.

## Rules that must not be broken

1. **Do not change strategy** until the sample lands (see Status). Bugs and
   measurement faults may be fixed at any time — and a mechanism that corrupts
   the measurement counts as a bug.
2. **Read `padj`, never `t`.** Every bucket goes through BH-FDR.
3. **Check `eff=` before `n=`.** Overlapping positions are not independent
   observations, and the gap is a factor of 2–6.
4. **Never put an LLM on the signal or execution path.** See Lessons.
5. **Do not pool eras.** See Era boundaries.

## How to read a WOLF-DIAG card

In this order, and stop at the first one that fails:

1. `window=` and the era boundaries — is this one bot or several averaged?
2. `eff=` — the real sample. `n=` is the flattering one.
3. `padj` — not `t`, and not `nom`.
4. Shape across rows, not the winning row. A maximum is positive under a null.

`nom` is printed so the size of the overlap discount is visible, never so it
can be preferred. Everything on the card is charged `mean_open`.

The card is allowed to say nothing. At `eff` of 3–6 that is the correct
answer, and a card that could not say it would be a machine for manufacturing
confidence.

## Lessons that cost real time

**Self-blinding.** A live veto cannot be judged: what it drops never becomes an
outcome, so the veto's own correctness is unmeasurable. Found four times — the
AI veto (rejected), the whale veto (identified, still hard-blocking), the
learning blacklist (fixed 2026-09-07, and the worst of them because a bench is
an absorbing state), and the chase gate (made visible 2026-09-08). **Whenever a
component decides what does not happen, ask what it makes unmeasurable.**

The chase gate also shows the wrong remedy. The first fix widened the limit,
which does not cure self-blinding — it relocates the blind spot, and pays for
the move by changing the geometry of every trade admitted past the old line.
**Recording what a gate drops costs nothing and answers the question; moving a
gate costs a sample and answers a different one.**

**Level questions are unaffordable; paired questions are not.** "Does the bot
make money" needs thousands of trades. "Is rule A better than B on these same
trades" got a decisive answer at n=66, because the market move cancels. Reach
for `/whatif` before reaching for patience.

**A score hides its own composition, and a constant among the awards hides
twice.** PREDUMP's `atr/price < 0.1` paid 5 points on 100% of 1320 bars
measured: not a component but a threshold shift wearing evidence's clothes. It
is a gate now, and the same sweep says `vwap_premium` lands on 50.3% of bars
and `bear_fvg_above` on 49.5% against 2.5% for the divergence the docstring
calls the strongest tell — so a quarter of its signals cleared the bar carrying
neither primary. **Ask what fraction of bars each award fires on before reading
any total; anything near 100% is a constant and anything near 50% is a coin.**

**Two numbers under one name is how a component hides.** The cost card quoted
an assumed round trip (fees + slippage) beside a measured one (fees + spread)
and called both a round trip. The diag reported `eff_n_floor` on one line and
undiscounted `t` on the next. In both cases the reader was left to reconcile
figures that were never the same quantity.

**A threshold nobody can reach reads exactly like a market with no setups.**
PREPUMP scored zero signals for months and every reading of that was wrong,
because its hard gate forbade three of the things it awarded points for —
a ceiling of 73 against a threshold of 78. Silence from an unsatisfiable rule
carries no information at all, yet it is indistinguishable on every card from
silence that means "conditions are absent". The tests missed it for the same
reason they usually do: they asserted a signal appeared on a fixture built to
produce one, never that the score it achieved cleared the bar it was graded
against. **When a component gates and scores the same bar, check the two sets
can intersect.**

**A detector that cannot be called looks exactly like a detector being
strict.** TRAP's `evaluate` never grew the `features` parameter the screener
started passing on 2026-08-20, so every call raised TypeError — caught by
`_best_candidate`'s own handler, logged beside real detector faults, and
stepped over. Nineteen days, sixty-five commits, zero signals through every
era the bot has measured. Its docstring pre-explains the silence
("deliberately strict — threshold 80, HIGH conviction only"), so nothing on
any card contradicted it. The tests were green from both sides: every TRAP
test called `evaluate` with two arguments, and every fake detector in the
screener tests carried the correct four-argument one. **Test the pairing, not
the halves — invoke the real collaborators the way production invokes them.**

**Name the fault, not the symptom.** "The arbiter abstained", "bear is quiet",
"the collector has 2 symbols" are each shared by several unrelated faults with
unrelated remedies. The provider almost always already said which; the bug was
discarding it.

**More data ≠ more evidence.** `eff_n = n / mean_open`. More symbols raises
both, so the effective sample barely moves. Faster scanning finds nothing new
either — detectors read closed bars, and a 10-minute cycle already oversamples
a 1h detector six times.

## Era boundaries — never pool across these

| When | What changed |
|---|---|
| `cf2adac` | buggy era dropped whole |
| ~2026-09-03 | `MAX_COST_R=0.15` gate landed (median 1R 1.14% → 2.2%, cost −46%) |
| 2026-09-04 | AI `thinking` disabled — verdicts changed character, not just availability |
| 2026-09-07 | learning moved to monitor mode |
| 2026-08-20 | TRAP silently uncallable (signature drift) — **zero TRAP signals from here until 2026-09-08** |
| **2026-09-08** | PREPUMP made satisfiable, universe mover lane, TRAP revived — **current sample starts here** |

Eras should be segmented by signal **creation** date. `/diag` and `/whatif`
currently window on **resolution** time; that is a known, unfixed gap.

## Already tested — check before proposing anything

`wolf/hypotheses.json`, readable in Telegram via `/tested`. Fifteen entries.
`OPEN` (not measured) is deliberately distinct from `INCONCLUSIVE` (measured,
separated nothing). **Add an entry whenever something is settled, and never
duplicate its content into this file** — one of them would go stale.

Six entries added 2026-09-08: `prepump-unsatisfiable-threshold`,
`universe-volume-ranking-is-lagging`, `chase-gate-self-blinding`,
`predump-thin-evidence`, `trap-detector-dead-19-days` and
`gated-awards-are-constants`.

Large rejections worth knowing without opening it: exit-geometry re-cut (was
believed the biggest lever; measured across 6 variants, does not move),
tighter entries for win rate, cost-model refinement, LLM in the signal path,
and the 350-trade sample target.

## Status — 2026-09-08, HEAD `trap-revived`

981 tests green. Working tree clean.

**The sample was reset, deliberately.** Two things changed signal composition:
PREPUMP can now emit at all (it could not — see below), and the universe gained
a mover lane. The first is a bug fix, allowed under rule 1 at any time; the
second is a strategy change, made because a detector made reachable is worth
little while the symbols it reads go unscanned.

A third change was made and then withdrawn the same day, which is the part
worth remembering. Both breakout detectors briefly carried a 1.5R chase limit
in place of the global 0.5R. Widening it does not merely admit more trades: the
stop does not move with the re-quote, so the risk unit stretches and the ladder,
rebuilt at the same R multiples, demands a far larger price move for the same
nominal 3R — at 1.5R of chase, 1R goes 5.0% → 11.6% of entry and the last rung
moves from +15% to +35%. Both ratio gates get *weaker* exactly there: nominal
R:R is unchanged, and a bigger 1R passes the cost gate more easily. MOMENTUM is
most of the current sample and would have carried it, so it is back on the
default and the sample stays one population.

The gate itself is still a self-blinding veto, and now a visible one: every
drop is recorded (`chase_drops`) and the diag card reports the count, the split
by strategy, and how far past the quote price had run. **Do not re-argue the
limit from replayed candle shapes — argue it from those drops.** PREPUMP keeps
1.5R only because it has never emitted a signal, so there is nothing there to
contaminate.

```
/diag 24h: n=9 eff=3 mean_open=2.36
meanR +0.338  se 0.546  t +0.62 (nom +0.95)  netR +0.244  => INCONCLUSIVE
cost 0.094R assumed / 0.060R measured, spread 9/9
ladder avgWin +1.01R avgLoss -1.00R => needs WR>49.8%, fill 67/44/11
ai CONFIRM=3 NEUTRAL=5 REJECT=1 — abstain 0%, two days running
all 13 buckets padj = 1.000
```

Volume 14 → 15 → 9 per day, trending down.

**Sample target is not a fixed count.** ~77 independent observations are needed
to resolve netR +0.244 at t=2. In nominal trades that is ~182 at `mean_open`
2.36 and ~411 at 5.34 — so 3–6 weeks, and the number moves with overlap.

Read `/diag 72` for now; the daily card is too thin. From ~2026-09-14 a
`/diag 168` window is one clean era.

**Watch list — patterns, not findings, do not act:** `ai:CONFIRM` at or near
the bottom five cards running (windows overlap heavily, so not five
observations); `whale:WITH` has flipped sign four times; `learn:BENCH` won and
`learn:BOOST` lost on n=1 and n=4.

**TRAP is emitting again**, for the first time since 2026-08-20. Expect a small
handful of signals a day — the sweep audit puts it at ~0.23% of bars. Its
absence was a crashed signature, not strictness; see Lessons.

**New on the card: the `evidence` bucket.** Splits every strategy into
`PRIMARY` (the signal carried its detector's own `primary_components`) and
`THIN` (it cleared the threshold on context alone), with `UNRECORDED` for rows
written before the composition was persisted — an absence of data, never folded
into THIN. Read it the same way as any other bucket: `padj`, and shape across
rows. It exists because a pooled strategy row can show a 50% win rate made of
one population that always won and one that never did. This is the cheap way to
ask about PREDUMP, because the comparison is paired *inside* a strategy.

## Broken / unfinished

1. **On-chain valuation collector** asks for 15 symbols and returns 2. The card
   now names the cause (rate limit vs universe drift vs empty) — read it before
   guessing.
2. **Whale veto still hard-blocks**, dropping candidates before
   `record_signal`. Every day it runs, the evidence needed to judge it is
   discarded. The only queued item whose cost grows while it waits.
3. **Windowing uses resolution time** where era hygiene wants creation time.
4. `wolf/reports/conviction.py` (AI conviction ranking) arrived via PR #32 from
   another session and has never been measured.
5. **The chase limit is still unargued.** 0.5R was never derived from a trade
   and neither was the 1.5R that briefly replaced it. Drops are now recorded, so
   from ~2026-09-15 the card can say how often the gate fires and how far price
   had run. Grading them needs one more piece that does not exist yet: a job
   that re-prices a recorded drop against later candles to say what it would
   have returned. Until that exists the record accumulates and settles nothing.
6. **How much of each score is decided before any confluence is read.**
   Counting only bars past each detector's own gate: SCALP's `sweep` 100% and
   `vwap` 95.0% against `order_block` 0.3%; TRAP's `sweep` 100% and
   `rejection_wick` 86.4% — the gate already demands the reclaim that makes the
   wick dominant — against `divergence` 3.8%; SWING awards 55 of its 80 for
   things it already gated, MOMENTUM 55 of its 65. Unlike PREDUMP's
   `atr/price`, none of these is a clean constant, so none can be removed with
   provably identical decisions. Do not reweight them from the sweep; that is
   the chase-gate mistake. The `evidence` bucket now covers all six strategies.
7. **PREDUMP shorts into strength and nothing stops it.** It is the only
   directional detector with no market-context gating in effect: exempt from
   the regime filter (`COUNTER_TREND_TYPES`), the bounce guard that covers it
   is monitor-only, and its lone live veto reads four hours of tape rather than
   a trend. Whether this actually costs anything is unmeasured — the claim that
   PREDUMP performs badly has never been checked against a card, and at eff=3
   could not be. The `evidence` bucket is the instrument; do not reweight its
   awards or lift the exemption before it reports, which would repeat the
   chase-gate mistake of moving a gate with no trade behind it.
8. **PREPUMP's band edges are calibrated on synthetic series, not on trades.**
   The RSI ceiling (80), the VWAP premium (3%) and the expansion multiple
   (2.5×) were chosen against replayed candle shapes because the detector had
   never emitted a signal to fit them to. Replay is enough to prove a threshold
   is *reachable*; it says nothing about whether it is *right*. Treat the first
   PREPUMP trades as a check on these three numbers, and note that synthetic
   bases inflate RSI — a very tight coil makes any breakout print RSI in the
   nineties, which real bases do not.

Not yet built, offered and not taken: Deflated Sharpe Ratio (the registry is
its trial counter); splitting `sentiment` from `materiality` in
`wolf/news/signal.py`.

## Practical

```bash
pip install -r requirements-dev.txt
python -m pytest            # 981 tests, ~5s
```

Entry point `python -m wolf.main` (Procfile worker). Wiring lives only in
`wolf/app.py`; configuration only in `wolf/config.py`, read from env.

| Path | What it holds |
|---|---|
| `wolf/diagnose.py` | the diagnostic card — the project's centre of gravity |
| `wolf/stats.py` | BH-FDR, Student-t, Welch gap with the overlap discount |
| `wolf/whatif.py` | paired re-scoring: stop rules, ladder geometry, whale policies |
| `wolf/screener.py` | the gate order — cheap disqualifiers before expensive ones |
| `wolf/hypotheses.json` | what has been settled, and what settled it |

Commit messages here explain **why**, in prose, including what was rejected and
what the change costs. Match that. Tests are named as the sentence they prove.
