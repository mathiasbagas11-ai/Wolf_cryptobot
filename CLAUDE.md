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

**A sign that holds for four windows is still close to one observation when
the windows overlap.** Three patterns carried on the watch list for weeks all
reversed on the same card (2026-09-18). MOMENTUM had been negative on four
consecutive cards (-0.433, -1.000, -0.786, -0.625) and printed +0.900.
`ai:CONFIRM` had sat at or near the bottom for eight cards and became the best
bucket on it. `learn:BOOST` had been the worst of the learning labels and
became the best, with `PENALTY` taking its place at the bottom. Every one was
`padj = 1.000` every time, and every one was written down as "pattern, not
finding, do not act" — acting on any of them would have been wrong in three
places at once. The windows are 24h while the timeouts run to 48h and beyond,
so consecutive cards share trades: a streak of four is not four observations.
**Count how much of a streak is the same trades before treating its length as
evidence.**

**Buckets on one card are not independent of each other.** The 09-18 card
showed `PREDUMP` n=9, `learn:PENALTY` n=8 and `ai:REJECT` n=4 all at exactly
-1.000 with `sd=0.00`, which reads as three separate confirmations of a loss.
The card had 12 `SL_HIT` in total, so inclusion-exclusion puts at least 5 of
PENALTY's 8 and at least 1 of REJECT's 4 inside PREDUMP's 9: one cluster of
losses in one strategy, seen through every label that correlates with it.
**Before reading several red rows as several findings, bound their overlap
against the card's own status counts.**

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

**An error that runs in both directions is invisible on every card.** Three
exit paths close a scaled position and until 2026-09-18 only two priced it as
scaled: the timeout path booked the whole position at the price on the clock,
as though the slice sold at TP1 were still open. The size of the mistake is
exactly `a * (x - R1)` — the first rung's allocation times how far the timeout
price sits from it — so it is *symmetric* about the rung: it under-books a
trade that drifted back and over-books one parked above. Nothing in meanR
leaned, so no diagnostic could point at it; it only added noise, and moved
trades across the dead band where `EXPIRED_FLAT` drops them out of the win
rate and out of the paper account entirely. It was found by the owner asking
why the balance did not move when TP1 hit, not by any card in six months.
**Ask which quantity is computed in more than one place; the copy added last
is the one that forgot.**

The repair carried its own version of the same shape, and the idempotence test
is the only thing that caught it: the backfill overwrote `exit_price` with a
synthetic figure derived from the blended PnL — which is the convention the
other two paths use, and which destroys the one input the blend reads. Run
twice, it re-blended its own output and booked the rung a second time. There
is no arithmetic that can tell a corrected row from an uncorrected one, because
both satisfy `pnl = (exit/entry - 1)`. **A correction that overwrites its own
input can only be run once; either keep the input or the correction is not a
correction.**

**Predict the number before you write it, or the report will talk you into
its own mistake.** The backfill's report was right on every line but one:
all 36 corrections matched the blend formula exactly when checked from
outside, and the balance line — 2,562.58 → 1,162.36 — read as their plausible
consequence. It was not. The outcome log is capped (`MAX_OUTCOMES`), so
replaying it from the starting balance does not correct the balance, it
**re-anchors it to whenever the surviving rows begin**; a −2.303R correction
over 36 of 500 rows came back as −55%. The only reason it was caught is that
the expected figure had been written down before the write — "−2.3R at 1%
risk is about −2.3%, and if it lands far from that it is not the rebank". The
reasoning that caused it is worth naming too: *the balance compounds, so it
cannot be patched by a delta, so it must be replayed* — true, then a
non-sequitur. `apply` is multiplicative, so the balance is a product and one
changed row patches it by a **ratio**, exactly and order-independently, with
no history at all. **Before a destructive write, state what the number should
be; a report cannot audit its own blind spot.**

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

`wolf/hypotheses.json`, readable in Telegram via `/tested`. Twenty-five entries.
`OPEN` (not measured) is deliberately distinct from `INCONCLUSIVE` (measured,
separated nothing). **Add an entry whenever something is settled, and never
duplicate its content into this file** — one of them would go stale.

Six entries added 2026-09-08: `prepump-unsatisfiable-threshold`,
`universe-volume-ranking-is-lagging`, `chase-gate-self-blinding`,
`predump-thin-evidence`, `trap-detector-dead-19-days` and
`gated-awards-are-constants`. One added 2026-09-09:
`momentum-score-cannot-reject`. One added 2026-09-17:
`contest-max-score-self-blinding`. One added 2026-09-18:
`timeout-forgot-the-banked-rung`. One added 2026-09-21:
`replay-over-a-truncated-log`. **`chase-gate-self-blinding` moved OPEN →
INCONCLUSIVE on 2026-09-17 — the limit is not a lever; do not re-open it.**

Large rejections worth knowing without opening it: exit-geometry re-cut (was
believed the biggest lever; measured across 6 variants, does not move),
tighter entries for win rate, cost-model refinement, LLM in the signal path,
and the 350-trade sample target.

## Status — 2026-09-24, HEAD `balance-repaired`

1042 tests green. Working tree clean.

**The ledger was wrong, and every card built on it inherited that.** A position
that banked TP1 and then timed out was booked as though nothing had been sold —
see the lesson above for why no card could show it. Fixed forward in
`wolf/tracker.py`; the historical rows are re-booked by `wolf/rebank.py`.

**The backfill has been run** (2026-09-21 11:48 UTC, against
`/data/state_data`). 500 outcomes scanned, **36 re-booked, 1 changed status,
`r_delta` −2.303R**. The ledger had been *over*-booking, not under-booking:
the correction is zero at 1.000R for a row that banked TP1 and at 1.375R for
one that banked TP1+TP2, and above those points it marks the row down. So the
rows hit hardest were the runners that went furthest and then timed out —
`HYPEUSDT` was booked +2.387R and is now +1.577R. All 36 were verified against
`Σ(alloc·R) + (1−Σalloc)·x` from outside the tool before the write.

Aggregate impact is small: −0.064R per re-booked row, −0.0046R on an overall
`meanR` across 500. **This corrects an earlier reading in the other
direction** — the 09-18 → 09-21 jump in `avgWin` (+0.76R → +1.04R) was
guessed to be partly a booking artifact; the fix pushes `avgWin` *down*, so
that jump was real and if anything understated. Historical cards had inflated
`avgWin` and therefore a `needs WR` bar that was too low.

**The balance step of that run was wrong, and is fixed** — see the lesson
above. It replayed the outcome log from `PAPER_START_BALANCE` and reported
2,562.58 → 1,162.36, which is not a −55% correction but a re-anchor: the log
is capped at `MAX_OUTCOMES` (the live one held exactly 500 rows of a longer
history). `rebank_outcomes` now patches the balance by a ratio instead, which
needs no history, and `replay_balance` refuses a truncated log by name.
**The balance is repaired** (2026-09-24): `/rebank repair 2562.58 -2.303
confirm` wrote **2,504.24**, which is the figure predicted before the write to
the cent. The exact path (`/rebank repair 2562.58 36`) had produced nothing:
three days at ~25 outcomes/day pushed the oldest rows out of the capped log,
and the rows it needs were among them — the same shrinking-reach shape as the
chase audit, and its window was about a day. The estimate path takes the
report's `r_delta` and needs nothing from the log; `exp(k·r_delta)` matches
the exact product to 0.0258% on the 15 rows the report printed.

**Two account fields were destroyed by the replay and cannot be recovered.**
`peak` was reset to the replayed curve and now reads 2,504.24, so `/paper`
shows **Max DD 0.00%** — flattering: the balance stood at 2,562.58 before the
correction, so the true drawdown is at least 2.3% and the true peak is higher
still. `trades` reads 554, an undercount of the all-time figure. Neither feeds
any card or decision. `peak` corrects itself once the balance clears 2,562.58;
`trades` does not. Read both as starting 2026-09-21.

**09-24 card (24h):** n=25 `eff=7`, meanR +0.389, netR +0.299, ci95
[−0.408, +1.185], all 16 buckets `padj = 1.000`. Nothing separated. Two lines
worth tracking because they are counts or mechanics, not means: rung fill has
risen card over card (09-18 41/4/4 → 09-21 52/29/15 → 09-24 **60/36/28**),
and `avgLoss` printed **−0.83R**, the first card below −1.00R — timeouts that
banked a rung and then drifted negative now book the rung (`EXPIRED_LOSS=2`),
which is the forward fix showing up, not the market. Required WR 40.9% against
62.5% achieved is the widest margin yet, but it straddles the booking change
(09-18 and earlier over-stated `avgWin`), so the 57.0 → 40.9 slide is not one
quantity measured four times. `evidence:THIN` was empty this window and
PREPUMP did not trade. On-chain named its fault: 5 of 15, rate limited.

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
/diag 24h 2026-09-18: n=22 eff=4 mean_open=4.44
meanR -0.202  se 0.420  t -0.48 (nom -1.01)  netR -0.281  => INCONCLUSIVE
cost 0.079R assumed / 0.060R measured, spread 22/22
ladder avgWin +0.76R avgLoss -1.00R => needs WR>57.0%, fill 41/4/4
chase 5 dropped (TRAP=2 SCALP=2 PREDUMP=1) median 0.76R max 1.11R
all 20 buckets padj = 1.000
```

Five cards of one era, which is the only thing here worth reading. Every
strategy has changed sign at least once:

| | 09-09 | 09-13 | 09-15 | 09-17 | 09-18 |
|---|---|---|---|---|---|
| MOMENTUM | -0.433 | -1.000 | -0.786 | -0.625 | **+0.900** |
| SCALP | -0.850 | +0.723 | +0.424 | +0.700 | +0.171 |
| PREDUMP | -0.161 | -0.240 | -0.099 | +1.700 | **-1.000** (n=9) |
| TRAP | +0.051 | -1.000 | +0.241 | -0.250 | +0.825 |
| SWING | -0.250 | -1.000 | +0.800 | +0.125 | -1.000 |

**Nothing has separated, and three long-running patterns collapsed at once on
09-18** — see the lesson above. Treat the watch list as vindicated rather than
as a queue of near-findings.

`eff` has been 3-9 throughout while `n` ran 12-40: `mean_open` between 1.9 and
4.9 is doing most of the work, and it is why no single card has ever been able
to speak. Reading any one row of one card alone has been wrong every time it
has been tried.

**Sample target: stop computing one from a single card's effect.** It has been
re-derived three times off successive point estimates (-0.335, -0.092, -0.202)
and each figure was obsolete within days. The effect has no stable estimate to
size against yet.

**PREDUMP's first real sample, and its caveat.** n=9, all `-1.000`, `sd=0.00` —
nine clean stops, no rung filled. It is the largest single-strategy block any
card has shown, and it lands on the strategy Broken #7 flags as the only
directional detector with no market-context gating. But it printed +1.700 the
day before, and at least five of those nine are the same trades as
`learn:PENALTY`. One window, `padj = 1.000`. Do not act; watch whether it
repeats.

**SCALP is approaching its own cost gate.** `1R = 1.48%` gives `cost = 0.14R`
against `max_cost_r = 0.15`. If its stops keep tightening it will start being
rejected by the cost gate rather than by anything about the setup.

**AI recovered on its own.** The DeepSeek read timeouts that reached 21% on
09-17 were gone by 09-18 — no `ABSTAIN`, no `AI_DEGRADED` flag. Nothing was
done to it, which is worth remembering before treating the next spike as a
defect needing a fix.

**The `evidence` bucket is working now.** `THIN` was 0 for three cards, 1, then
3 on 09-18, after `sweep_reclaim` (a gated constant) and `rsi_extreme` came out
of the primary sets on 09-17. Still far too small to report anything, but the
instrument can finally produce a contrast.

**PREPUMP still shows n=0 on every card**, and Broken #5 now explains why: it
fires and loses the `max(score)` contest, surviving only as a
`Confluence [MOMENTUM+PREPUMP]` tag on a MOMENTUM row. The contest audit is the
instrument for that; it is not evidence of a detector fault.

**TRAP is emitting again**, for the first time since 2026-08-20; its absence
was a crashed signature, not strictness (see Lessons). Its frequency was badly
mis-estimated when it came back: a synthetic sweep said ~0.23% of bars, about
1-3 signals a day, and it produced 12 traded plus 4 chase drops in the first
24h. It has since run 2-3 a day. **A sweep over random series is a poor
estimator of how often a gate opens in a real market** — it was out by 5-16x
here, in the direction of understating.

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
python -m pytest            # 1042 tests, ~12s
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
| `wolf/rebank.py` | one-shot re-booking of timeout rows that forgot a banked rung (`/rebank`, `python -m wolf.rebank`) |
| `wolf/screener.py` | the gate order — cheap disqualifiers before expensive ones |
| `wolf/hypotheses.json` | what has been settled, and what settled it |

Commit messages here explain **why**, in prose, including what was rejected and
what the change costs. Match that. Tests are named as the sentence they prove.
