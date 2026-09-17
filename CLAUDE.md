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
outcome, so the veto's own correctness is unmeasurable. Found five times — the
AI veto (rejected), the whale veto (identified, still hard-blocking), the
learning blacklist (fixed 2026-09-07, and the worst of them because a bench is
an absorbing state), the chase gate (made visible 2026-09-08, settled
2026-09-17), and `_best_candidate`'s `max(score)` contest (made visible
2026-09-17). **Whenever a component decides what does not happen, ask what it
makes unmeasurable.**

The fifth is the one worth copying, because recording it made an *unaffordable*
question affordable. The chase audit compares drops against different setups, so
it is a level question and duly said nothing (+0.015R, p=0.976). The contest is
the same symbol on the same bar, so the market move cancels and it is paired.
**When a gate discards an alternative, check whether what it discarded is a
matched pair — those are the cheap ones.**

The chase gate also shows the wrong remedy. The first fix widened the limit,
which does not cure self-blinding — it relocates the blind spot, and pays for
the move by changing the geometry of every trade admitted past the old line.
**Recording what a gate drops costs nothing and answers the question; moving a
gate costs a sample and answers a different one.**

**A measurement whose reach shrinks with time has to be taken on a schedule,
not on demand.** The chase audit replayed drops when asked, and its first live
run lost 21 of 48 to "no history reaching back". The replay needs 15m candles
from now back to the drop, so the bars required grow every hour and a venue
serving fewer returns a window that opens too late — which means the skips
correlate with age and with which venue serves the symbol, and the surviving
sample leans toward recent drops on liquid pairs. A 44% loss on a non-random
criterion can manufacture the whole effect it was built to detect. Grading now
runs hourly, once per drop, close to the event, and the verdict is stored.
**Ask when a measurement stops being possible, not only whether it is correct.**

**Level questions are unaffordable; paired questions are not.** "Does the bot
make money" needs thousands of trades. "Is rule A better than B on these same
trades" got a decisive answer at n=66, because the market move cancels. Reach
for `/whatif` before reaching for patience.

**An award whose condition the gate has already defined away is free to add
and never pays.** MOMENTUM paid 15 points for an FvG-launch test that probed
the 50-candle extreme it had just cleared against gaps drawn from the last 40
candles. The windows overlap, so a bull gap's lower edge is some in-window
candle's high and the window's minimum low is at or below it by construction —
zero hits over 3738 breakout bars. It read as confluence on the card and as a
`primary_component` in the evidence bucket. **Before trusting an award, ask
which window each side of its comparison comes from; if the gate already fixed
one of them, the test is vacuous.**

**A test that waives the case it exists to catch is worse than no test.** The
guard written to stop a gated constant being listed as a `primary_component`
skipped any detector producing under twenty signals — which was four of six,
including TRAP, the detector it was written for. It passed with the bug
restored. The minimum is now asserted rather than waived, and the one detector
random series genuinely cannot drive is named with its reason. **A skip
condition in a test is a silent exemption; make it fail instead.**

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
`gated-awards-are-constants`. One added 2026-09-09:
`momentum-score-cannot-reject`. One added 2026-09-17:
`contest-max-score-self-blinding`. **`chase-gate-self-blinding` moved OPEN →
INCONCLUSIVE on 2026-09-17 — the limit is not a lever; do not re-open it.**

Large rejections worth knowing without opening it: exit-geometry re-cut (was
believed the biggest lever; measured across 6 variants, does not move),
tighter entries for win rate, cost-model refinement, LLM in the signal path,
and the 350-trade sample target.

## Status — 2026-09-17, HEAD `contest-recorded`

1015 tests green. Working tree clean.

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
/diag 24h 2026-09-09: n=40 eff=9 mean_open=4.04
meanR -0.335  se 0.299  t -1.12 (nom -2.26)  netR -0.403  => INCONCLUSIVE
cost 0.068R assumed / 0.061R measured, spread 40/40 (first full coverage)
by strategy: TRAP +0.051 (n=12) | PREDUMP -0.161 | SWING -0.250 | MOMENTUM -0.433 | SCALP -0.850 (n=10, wr=10)
ladder avgWin +0.83R avgLoss -0.96R => needs WR>53.7%, fill 30/12/0
chase 13 dropped (SCALP=9 TRAP=4) median 0.68R max 0.83R
all 18 buckets padj = 1.000
```

Volume 9 → 40 per day. **`n` rose 4.4x and `eff` only 3x** — the mover lane and
TRAP's revival bought less than the count suggests, exactly as the standing
lesson says. The `t -1.12` against `nom -2.26` is the clearest illustration the
card has produced of why rule 3 exists: read nominally it looks like evidence
of losing, discounted it is nothing.

**Sample target moved with the effect.** At `meanR -0.335` and `sd 0.94`,
resolving at `|t|=2` needs `eff ≈ 31`; at ~9/day that is roughly 2–3 days, not
weeks. The earlier 3–6 week figure was computed against a smaller effect.

**Era hygiene on that card is only partial, and this matters.** Windowing is on
resolution time (Broken #3), so with a 24h window: TRAP (4h timeout) and SCALP
(10h) are provably one era — 22 of the 40 — while MOMENTUM and PREDUMP (48h)
reach back to 09-07 and SWING (168h) to 09-02. Do not compare the last three
against earlier cards.

Read `/diag 72` for now. From ~2026-09-14 a `/diag 168` window is one clean era.

**Watch, do not act.** `learn:BOOST` (n=24, -0.607) below `learn:PENALTY`
(-0.208) below `learn:NONE` (+0.431) — monotonically inverted, and clean
because learning is monitor-only. `ai:CONFIRM` (n=32, -0.410) below
`ai:NEUTRAL` (+0.078), sixth card running. `padj = 1.000` on both.

**Two things the 09-09 card said about instruments rather than markets.**
PREPUMP still emitted **nothing** in 24h despite being fixed — 24h is short for
a 1h detector needing a coil and a release, but if 72h is still zero there is
another gate nobody has found. And the `evidence` bucket split 33 PRIMARY / 7
UNRECORDED / **0 THIN**: with the primaries as chosen, everything qualifies, so
the instrument cannot yet answer the PREDUMP question it was built for. If THIN
is still empty after a few days the primary sets are too permissive and need
tightening.

TRAP's frequency was also badly mis-estimated here: the synthetic sweep said
~0.23% of bars, about 1–3 signals a day. It produced 12 traded plus 4 chase
drops in 24h — 5–16x the estimate, making it the second most productive
detector rather than the rarest.

**Watch list — patterns, not findings, do not act:** `ai:CONFIRM` at or near
the bottom five cards running (windows overlap heavily, so not five
observations); `whale:WITH` has flipped sign four times; `learn:BENCH` won and
`learn:BOOST` lost on n=1 and n=4.

**TRAP is emitting again**, for the first time since 2026-08-20. Expect a small
handful of signals a day — the sweep audit puts it at ~0.23% of bars. Its
absence was a crashed signature, not strictness; see Lessons.

**MOMENTUM's threshold decides nothing, and this is not fixed.** Its gates
guarantee 65 and `vwap_aligned` fires on 100% of bars, so the floor is 85
against a threshold of 80 — lowest score seen 85, median 100, `confluence_level`
always HIGH. Two consequences worth holding: the score carries almost no
information about a MOMENTUM signal, and `screener._best_candidate` picks one
candidate per symbol with `max(score)` across floors of 0 (PREPUMP, PREDUMP),
20 (SCALP), 22 (TRAP), 55 (SWING) and 85 (MOMENTUM) — so MOMENTUM takes a
contested symbol on its floor rather than on the read. Both remedies are
strategy changes; they wait.

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
5. **`_best_candidate` discards the alternative with no record — now being
   measured.** One candidate per symbol survives `max(score)`, and the scores
   compared are not on a common scale: gates guarantee floors of 0 (PREPUMP,
   PREDUMP), 20 (SCALP), 22 (TRAP), 55 (SWING) and 85 (MOMENTUM). So MOMENTUM
   takes a contested symbol partly on how much it pays itself for conditions it
   already required — and it has been negative on four consecutive cards while
   its own chase drops are negative too, so it is not a selection artifact.
   This also explains PREPUMP's n=0 since being fixed: it fires and is then
   relabelled as a `Confluence [MOMENTUM+PREPUMP]` tag on a MOMENTUM row.
   `wolf/contest_audit.py` records the losers and grades them hourly;
   `/whatif contest` pairs each against what the winner actually returned.
   **This is the paired question — read it before any level one.**

   *Settled and closed:* the chase limit. On a near-complete sample
   (2026-09-17) drops returned +0.070 against taken +0.055 — gap +0.015R at
   p=0.976 — and the distance split inverted between readings across a range
   now reaching 1.66R. The gate neither protects nor costs; **do not widen it,
   do not tighten it, do not re-argue it**. The 09-13 reading of +0.350R was
   the age-bias artifact it was flagged as.

6. **How much of each score is decided before any confluence is read.**
   Counting only bars past each detector's own gate: SCALP's `sweep` 100% and
   `vwap` 95.0% against `order_block` 0.3%; TRAP's `sweep` 100% and
   `rejection_wick` 86.4% — the gate already demands the reclaim that makes the
   wick dominant — against `divergence` 3.8%; SWING awards 55 of its 80 for
   things it already gated and MOMENTUM 85 of its 80, which is the one case
   where the threshold has stopped deciding anything at all (see Status).
   MOMENTUM's `not_overextended` (5) is dead in practice too — the calmest
   breakout swept printed RSI 77 against a ceiling of 75, median 98. Unlike
   PREDUMP's `atr/price`, none of these is a clean constant, so none can be
   removed with provably identical decisions; the one that could — an FvG probe
   the gate forbade outright — is gone. Do not reweight the rest from the
   sweep; that is the chase-gate mistake. SWING is the healthiest of the six:
   floor 55 against 80, no unreachable component, and its score genuinely
   rejects. The `evidence` bucket covers all six strategies.
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
python -m pytest            # 1015 tests, ~12s
```

Entry point `python -m wolf.main` (Procfile worker). Wiring lives only in
`wolf/app.py`; configuration only in `wolf/config.py`, read from env.

| Path | What it holds |
|---|---|
| `wolf/diagnose.py` | the diagnostic card — the project's centre of gravity |
| `wolf/stats.py` | BH-FDR, Student-t, Welch gap with the overlap discount |
| `wolf/whatif.py` | paired re-scoring: stop rules, ladder geometry, whale policies |
| `wolf/chase_audit.py` | grades what the chase gate dropped (settled: not a lever) |
| `wolf/contest_audit.py` | pairs each displaced candidate against the winner that beat it |
| `wolf/screener.py` | the gate order — cheap disqualifiers before expensive ones |
| `wolf/hypotheses.json` | what has been settled, and what settled it |

Commit messages here explain **why**, in prose, including what was rejected and
what the change costs. Match that. Tests are named as the sentence they prove.
