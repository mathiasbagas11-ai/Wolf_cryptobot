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

**Unequal exposure manufactures skill, and a bias you write down is not a
bias you removed.** The High-Conviction bucket shipped labelling a signal a
pick if it was *ever* picked. Rankings run hourly; losers stop out within a
look or two, winners stay live for dozens — so a picker with no skill at all
has "picked" nearly every winner. Simulated, a random picker prints a +1.15R
gap that way. The first live reading was +1.22R, with an 86% win rate on
picks. The bias had been written down the day the bucket shipped and called
"worth holding", which was wrong: it was the size of the entire effect, and
the reading would have been taken as proof the room works. **Give every unit
exactly one chance to be chosen — or measure what the extra chances are worth
before reading any gap.**

**A pair is only paired if both sides go through the same steps.** The
contest audit was built to cancel the market move by comparing two
candidates on the same symbol and bar — and then recorded the loser one step
before the winner was re-quoted at the live price. The winner always entered
late on a stretched risk unit; the loser was graded on a fill that no longer
existed. Three weeks of pairs were biased against the winner, and the first
read (−0.081R) was taken, written down and moved the registry before the
asymmetry was found. What found it was a count nobody had printed: ties.
TRAP and SCALP use the same entry, stop and ladder rule, so an honest
instrument has to score them level nearly every time; it showed them tied
only 37 times in 87. **When two things that should be identical come out
different, the instrument is measuring itself — and print the ties, because
they are where that shows.** The tie argument itself was only half right,
which is the coda worth keeping: TRAP and SCALP share entry, stop and ladder
but not the timeout (4h against 10h), so any trade still open at 4h *should*
split. The bias was real by mechanism; how much of the 37/87 it explained was
never separated from the timeout. **Before calling two things identical,
list every parameter, not the ones the argument needs.**

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

`wolf/hypotheses.json`, readable in Telegram via `/tested`. Twenty-seven entries.
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
`replay-over-a-truncated-log`. One added 2026-09-24:
`conviction-room-picks-unrecorded`. **`chase-gate-self-blinding` moved OPEN →
INCONCLUSIVE on 2026-09-17 — the limit is not a lever; do not re-open it.**
**`contest-max-score-self-blinding` moved OPEN → INCONCLUSIVE on 2026-10-20 —
max(score) is not a lever either; the floor-bias worry is closed.**

Large rejections worth knowing without opening it: exit-geometry re-cut (was
believed the biggest lever; measured across 6 variants, does not move),
tighter entries for win rate, cost-model refinement, LLM in the signal path,
and the 350-trade sample target.

## Status — 2026-10-24, long replies split for Telegram

1085 tests green. Working tree clean.

### Start here (handoff written 2026-10-20 for the next session)

**Talking to the owner.** Reports go in plain, casual Bahasa Indonesia —
the owner asked for "bahasa yang paling mudah dipahami" after a technical
reply lost them. Lead with the conclusion, use simple tables and everyday
analogies, keep jargon (eff, padj, R) out of the headline or explain it in
one clause, and end with exactly what they should type or do next. Anything
the owner has to run should be one Telegram command, not a shell step: they
operate from a phone, and `railway ssh` was a dead end.

**How cards arrive.** The owner pastes `/diag` cards (24h, usually stamped
~05:27 UTC), sometimes several days at once and sometimes the same card
twice. Pool only disjoint windows, count a duplicate once, and say which
days are missing. Running totals live in the dated blocks below; the latest
is the twenty-card block.

**Where the evidence stands (450 trades, 09-21 → 10-19).**
- Bot overall: **−0.027R before costs, −0.111R after**, t −1.95 on 19
  day-level df. Flat before costs, slowly losing after them. Not yet
  settled, but the shape is no longer in doubt.
- **No strategy has a demonstrated edge.** MOMENTUM is the only one positive
  in both halves (+0.089R pooled, +0.029R after cost) and is smaller than
  what the best of six zero-edge strategies prints by chance (median
  +0.32R in simulation). TRAP and PREDUMP swapped sign between halves;
  SWING's 0/14 streak broke. TRAP and SCALP are largely one detector.
- **High-Conviction room: no edge** over 17 first-look days (gap +0.045R).
- **`/paper`'s headline balance is gross of costs** — it now says so on the
  card. Since 2026-10-23 a net balance runs beside it (`cost_bps` on each
  trade's own stop; flat timeouts pay fees too), anchored on the gross
  balance at its first trade, plus a ledger line compounding the capped log
  gross and net. Quote the net figures to the owner.
- PREPUMP stops reached **29%** (10-14); its 1.5R chase exemption has no
  remaining justification.

**What the owner has been told to do with real money** (keep consistent):
**as of 10-23, pause real-money following** — no strategy is positive after
costs, MOMENTUM included. If they insist, MOMENTUM only at 0.25–0.5% risk
(drawdown scales roughly with risk: p95 ~33% at 1% → ~8–17%), at most 2–3
positions open; never follow every signal (that is the −44% lived path);
skip PREPUMP; ignore the 🥇 badge. Earlier "skip SWING" advice was
withdrawn on 10-19; the 10-19 "MOMENTUM only, small size" was superseded
on 10-23 by the reading above.

**Open offers the owner has not yet accepted — ask before building:**
1. ~~Liquidity split on `/diag`~~ — **built 2026-10-23** ("gas A"). `liq:`
   rows bucket every traded row by recorded `spread_bps` at fixed edges
   (`TIGHT` <2bps, `MID` 2–8, `WIDE` ≥8, `NO_SPREAD` kept separate), chosen
   before any reading so cards pool. Each tier also prints its own measured
   fee+spread in R and the net after it. In the BH-FDR family. A level
   question, not paired — read gross for edge, net for survival, shape
   across tiers, and expect `padj` 1.000 on a 24h card. **First read due:
   `/diag 600`** (the capped log reaches ~25 days back). Context: on 15m–1h
   stops majors can pay *more* cost per R (fixed % fee over a tighter stop),
   so `TIGHT` is not automatically cheaper in R.
2. **Record chase distance on admitted signals** (offered 09-21, 10-04,
   "gas catat chase"): keep `quoted_entry`/`chase_r` before the re-quote
   overwrites `entry_price`. Needed to explain PREPUMP's huge stops.
3. ~~Net-of-cost line on `/paper`~~ — built 2026-10-23 ("gas bikin net paper").
4. ~~Cost lever~~ — read 10-23, **not a lever** (registry
   `tighter-cost-gate`). `/whatif cost` at 0.10 keeps 287 of 485, cost
   0.086 → 0.059R, but gross falls −0.024 → −0.053R and net is unchanged
   (−0.110 → −0.112). The dropped tight-stop trades were the gross-better
   ones (+0.018R gross, 0.125R cost). The loss is spread across stop widths,
   so no gate setting removes it. The owner had also asked how to lift a
   ~50% signal to 60–90% "with learning"; they were told win rate is the
   wrong target (expectancy is), that the old learning layer was moved to
   monitor mode for overfitting noise, and that +0.1–0.2R net is the
   realistic goal — reached, if at all, by finding a gross-positive subset
   in existing data (liquidity split, `evidence` bucket) rather than tuning.

**`/whatif contest` has been read (2026-10-20): the contest decides
nothing.** 56 clean pairs, winner minus displaced candidate **+0.000R**,
se 0.103, eff 9, p 0.998; ahead 11 / tied 30 / behind 15. TRAP>SCALP n=40
−0.012R, MOMENTUM>PREPUMP n=9 −0.000R, SCALP>TRAP n=6 +0.082R. The ~95%
range is ±0.24R. Registry → INCONCLUSIVE, read like the chase gate: not a
lever. Two side results: TRAP-vs-SCALP is in effect a paired 4h-vs-10h
timeout test (same entry/stop/ladder, different timeout) and reads zero;
and the 10-07 tie argument overlooked that timeout difference (see the
lesson). 84 winners were still unresolved — one re-read when they settle,
no more. SWING still has no paired look (one pair).

**`/montecarlo` (2026-10-21) — a sizing tool, not evidence.** Built after
the owner shared a "build trading systems with AI" carousel and asked to
take what was good from it. It deals the ledger's resolution *days* (kept
whole) into 2000 thirty-day paths at `paper_risk_pct`, each trade charged
`round_trip_cost_bps` on its own stop, and prints return and max-drawdown
percentiles, the p95 losing streak, the one lived path, and every ISO week
in the log. Two deliberate departures from the textbook version: days not
trades are the unit, because same-day stops are one move (09-28: ~−15.7%
in a day) and a trade shuffle prints a shallower hole — the shuffle figure
is shown beside it for the gap, like `nom`; and costs are charged, because
`/paper` is gross. The weekly table also removes the owner's choice of
which days to send. It resamples the measured mean, so it says nothing
about whether there is an edge; with a negative mean it shows how fast the
hole grows.

**First reading (2026-10-23, pool 09-29 → 10-22).** All strategies: 498
trades over 24 days, meanR −0.024 gross / **−0.110 net** — the same net
figure the twenty-card pool gave (−0.111), which is the cross-check that
the tool prices cost the way the card does. Lived path at 1% risk: **net
−43.7%, max DD 45.1%**; 30-day median −51.7%, 99% of paths lose. Day vs
trade shuffle differ by only 1.4 points because 09-28, the clustered day,
has already fallen out of the capped log. MOMENTUM: 110 trades over 22
days, **−0.025 gross / −0.098 net** — its earlier +0.089 (cards 09-21 →
10-19) is gone, carried down by W42 −0.400 and W43 −0.296 gross after W41
+0.421. 30-day median −14.4%, max DD p95 33.1% at 1% risk, 90% of paths
lose. Two weeks is a streak and is read as one; it does not show MOMENTUM
loses, but it removes the one positive reading the real-money advice
rested on. The plain arithmetic for the owner: ~21 trades a day at 1%
risk turns −0.11R into roughly −2% a day.
From the same carousel, considered and not built: a post-AI risk gate,
daily-loss limit and kill switch (no orders to stop; the drawdown pause
already exists in config and is off for the reason written there), LLM
agent roles (rule 4), profit factor (the card's avgWin/avgLoss/needs-WR
already carries it) and stale-feed checks (closed bars only; collectors
are staleness-checked in `wolf/market.py`).

**Long replies were silently lost (fixed 2026-10-24).** The owner ran
`/diag 600` and got nothing back: the card passed Telegram's 4096-char
limit, the API answered 400, and `send_raw` only logged it. Every reply and
digest now goes through `telegram.split_message`, cut on line boundaries
at 4000 chars with each piece re-wrapped in `<pre>`. Worth remembering that
the 24h card had been growing one bucket family at a time (`evidence:`,
`conv:`, `liq:`) toward the same wall; a daily digest that quietly stops
arriving would have read as the bot being down.

**Next read:** `/diag 600` for the first `liq:` reading (arrives in
several messages now); then
`/montecarlo MOMENTUM` again in a week — not to chase the
streak, but because it is the only number the real-money advice hangs on.
Nothing paired is left queued. The level question keeps
landing card by card; the two open offers below are what would sharpen it.

**Do not**: change any strategy, gate or threshold (rule 1 — the sample is
still landing); re-argue the chase limit; act on a single card or a streak;
pool across the era table.


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

**Twenty disjoint 24h cards since the booking fix (09-21 → 10-19).** Ten
more arrived in one batch on 10-19 (10-08 → 10-14 contiguous at 05:27,
then 10-17 → 10-19; 10-08 and 10-09 were pasted twice and counted once;
10-15, 10-16 and most of 10-07 were not sent). 450 trades, **−0.027R before
costs, −0.111R after**; 8 positive days of 20. Taking each day as one
observation, the after-cost mean is −0.110R at **t −1.95 on 19 df** —
the closest the level question has come to an answer. Two discounts keep it
short of one: that figure charges the assumed 20bps (measured runs ~13bps,
which gives roughly −0.09R), and the owner chooses which days to send.
Before costs the bot is flat; after them it bleeds slowly.

**The paper balance charges no costs.** `PaperAccount.apply` books
`pnl_pct` and the tracker never subtracts fees or spread, so `/paper` shows
the gross curve. At ~0.08R a trade and ~20 trades a day that is the whole
gap between a flat bot and a losing one; it has to be said whenever the
balance is quoted to the owner.

| | n | first 10 cards | last 10 cards | all 20 | after cost |
|---|---|---|---|---|---|
| MOMENTUM | 94 | +0.076 | +0.101 | +0.089 | +0.029 |
| SCALP | 142 | −0.005 | +0.044 | +0.020 | −0.080 |
| TRAP | 87 | +0.080 | −0.154 | −0.017 | −0.127 |
| PREDUMP | 90 | −0.168 | +0.031 | −0.095 | −0.155 |
| PREPUMP | 16 | −0.244 | −0.563 | −0.384 | −0.414 |
| SWING | 21 | −0.597 | +0.250 | −0.355 | −0.435 |

Every range still spans zero. TRAP and PREDUMP swapped places between the
halves; MOMENTUM is the only strategy positive in both, by a margin no card
can resolve. **The SWING streak ended at 0/14**: it won 3 of its next 6, so
the "skip SWING" advice given to the owner on 10-06/10-07 rested on a run
that the lesson on overlapping streaks already warned about.

**Seventeen first-look `conv:` days:** AI_PICK n=86 +0.068R, PASSED n=252
+0.023R — gap +0.045R; daily gaps positive on 9 of 17, t +0.60. The three
positive days that ended the 10-06 note were followed by gaps of −0.02,
−0.17, +1.21, +0.17, −0.79, +0.95, −0.55, +0.09, −0.12, −1.08. No edge.

**PREPUMP's stops keep widening:** 1R = 16.48% (10-12), 9.42% (10-13) and
**29.44%** (10-14) — a last rung near +88%. Its 1.5R chase exemption stands
on no argument (see below) and the chase distance of admitted signals is
still unrecorded, so whether these are base stops or chase stretch cannot
be told. The only number that matters for real money: at 1% risk a 29% stop
is a position of 3.4% of the account.

**10-17 was the second-worst day of the era** (meanR −0.560, SCALP 1/8,
`padj` 0.677 lowest yet, nothing survives). One `onchain:NEUTRAL` label
appeared on 10-18 — the first on-chain label in weeks; the collector is
still 1–4 of 15, rate limited.

**Contest audit:** read 10-20, +0.000R over 56 clean pairs — see Start here.

**Per-strategy pool, ten disjoint 24h cards since the booking fix**
(09-21 → 10-06, n-weighted; ranges use sd(R)≈1 and ~3.5 overlapping
positions per independent bet, the same discount the cards charge):

| | n | meanR | after cost | ~95% range | days +/− |
|---|---|---|---|---|---|
| MOMENTUM | 44 | +0.076 | +0.016 | −0.48 .. +0.63 | 6/3 |
| TRAP | 51 | +0.080 | −0.030 | −0.43 .. +0.59 | 8/2 |
| SCALP | 68 | −0.005 | −0.105 | −0.45 .. +0.44 | 4/6 |
| PREDUMP | 57 | −0.168 | −0.228 | −0.65 .. +0.32 | 2/8 |
| PREPUMP | 9 | −0.244 | −0.274 | −1.47 .. +0.98 | 2/3 |
| SWING | 15 | −0.597 | −0.677 | −1.54 .. +0.35 | 2/7 |

Every range spans zero. No strategy has shown an edge and none has been
shown to lose. If the means held, SWING would separate in about 40 trades
(~2–3 weeks); PREDUMP in ~460 (~2.5 months); MOMENTUM and TRAP in ~2,000
each, which is the "level questions are unaffordable" lesson in numbers.
TRAP's 8/2 day split is the kind of streak that has collapsed before, and
TRAP and SCALP are largely one detector — pooled together they are +0.031R.

**Contest audit, first read (2026-10-07) — withdrawn the same day.** It
read −0.081R (winner minus displaced candidate) over 122 pairs, p 0.588,
and the registry briefly moved to INCONCLUSIVE. Then the new tie count
showed TRAP>SCALP at 16 ahead / 37 tied / 34 behind — impossible for an
honest grader, since the two detectors share entry, stop and ladder. The
loser had been snapshotted at the bar close while the winner was re-quoted
at the live price, so every pair was biased against the winner (see the
lesson). **Fixed:** losers are re-quoted at the winner's live price by the
same rule (`screener._requote_spec`); a loser that rule would have dropped
is marked unfillable and never graded; the 122 pre-fix pairs are excluded
by name as `legacy` — the replay's ~10-day reach means they cannot be
re-graded. Registry entry back to **OPEN**. The clean sample starts at zero.
*(Clean read 10-20: +0.000R, p 0.998 — INCONCLUSIVE, not a lever.)*

What survives because it never depended on outcomes: **TRAP and SCALP are
largely one detector scored twice** (~100 collisions in three weeks), and
**SWING contested once**, so this audit is not SWING's paired look — a claim
made to the owner on 10-06 that was wrong. Because the bias ran only against
the winner, the withdrawn −0.081R is a floor on the true gap in expectation,
and MOMENTUM's +0.234R over displaced PREPUMP is if anything understated.

**10-05 and 10-06 cards (24h each):** windows 10-04 16:50 → 10-05 16:50 and
→ 10-06 16:50, contiguous and disjoint from 10-04. Both bad: meanR −0.205
(n=15, `eff=5`) and −0.266 (n=20, `eff=6`); wins 5/14 and 7/19 against bars
of ~50–55%; fill 33/20/7 and 35/20/5. All buckets `padj = 1.000`. PREPUMP
printed 1R = **10.37%** — the second card running above 10%.

**Ten disjoint 24h cards since the booking fix:** 244 trades, **−0.056R**
before costs, **−0.135R** after. Worse than the eight-card figure.

**Seven first-look `conv:` days pooled:** AI_PICK n=41 **+0.087R**, PASSED
n=113 **−0.042R** — gap **+0.128R**. Daily gaps −0.61, +0.78, +0.22, −1.03,
+1.03, +0.97, +0.84: the last three positive. Treating each day as one
observation, mean gap +0.315, t +1.01 on 6 df — about a one-in-three
chance of a gap this size from a room with no edge. Not a finding.
*Correction:* earlier pools weighted by graded n; bucket means are over
every traded row (flats included), so pools now weight by traded n. The
five-day gap was −0.041R, not −0.026R; no conclusion moves.

**SWING is 0 of 13, on seven separate days.** The same-day clustering that
discounted it is now mostly gone. Counting only days (no day with a SWING
win), the chance is 2.8% at a 40% true win rate and 4.9% at 35% — before
the post-hoc factor of six strategies scanned, which takes it to roughly
15–30%. Still not a finding at the most cautious count, but the
paired look the 10-04 note asked for is now due.

**The contest audit has never been read.** `wolf/contest_audit.py` has
recorded and graded every displaced `max(score)` candidate since
2026-09-17 — three weeks — and Broken #5 calls it the paired question to
read before any level one. `/whatif contest` was due "once pairs
accumulate (~09-19)" and was never run. It is also SWING's paired look:
SWING's floor of 55 beats every detector but MOMENTUM, so SWING signals
that displaced a candidate are paired against it on the same symbol and
bar. **Next step: run `/whatif contest`.** *(Done 10-07 — see above; it
turned out not to cover SWING.)*

**10-04 card (24h):** n=19 `eff=4`, meanR −0.071, netR −0.130, all 20
buckets `padj = 1.000`. Window 10-03 16:47 → 10-04 16:47, disjoint from
10-03. (The owner re-sent the 10-03 card once in between; it was not
counted twice.) PREDUMP went 0/4 the day after 5/6; `learn:` flipped back
(BOOST +0.321, PENALTY −0.480).

**Five first-look `conv:` days pooled:** AI_PICK n=32 **+0.014R**, PASSED
n=88 **+0.040R** — gap **−0.026R**. Daily gaps −0.61, +0.78, +0.22, −1.03,
+1.03: alternating, centred on zero. This is what a room with no edge looks
like. UNRANKED is 19 of 19 losses since first look began.

**SWING 0 of 11** since 09-27. Still discounted as below, but the
independent count is now plausibly seven or eight; one more week of this
and it earns the paired look.

**PREPUMP printed 1R = 12.57%** (one trade, flat) — the widest stop any
card has shown; the ladder's last rung sits at +37.7%. PREPUMP still
carries `max_chase_r = 1.5`, and the sentence below justifying that ("it
has never emitted a signal, so there is nothing to contaminate") **is no
longer true**: it has traded on at least four cards. How far an admitted
signal was chased is not recorded (the re-quote overwrites `entry_price`
and keeps only `entry_quoted_live`), so whether this 1R is the structural
base stop or the chase stretching it cannot be told. Recording it was
offered to the owner on 09-21 and not taken up.

**Eight disjoint 24h cards since the booking fix:** 209 trades, **−0.025R**
before costs, **−0.102R** after.

**10-03 card (24h):** n=22 `eff=5`, meanR +0.138, netR +0.077, all 19
buckets `padj = 1.000`. Window 10-02 15:05 → 10-03 15:05 (10-01 was not
sent). PREDUMP led (+0.850, wr 83%, n=6); `learn:` flipped once more
(PENALTY +0.473 over BOOST −0.092). No UNRANKED rows this window.

**Four first-look `conv:` days pooled:** AI_PICK n=29 **−0.089R**, PASSED
n=77 **+0.050R** — gap **−0.14R**, from daily gaps of −0.61, +0.78, +0.22,
−1.03. Two each way. If anything the room's picks trail what it passes
over, but nothing here separates from zero.

**SWING is now 0 of 9** since 09-27 (one more stop). Same discounts as
below; still a watch item.

**Seven disjoint 24h cards since the booking fix:** 190 trades, **−0.020R**
before costs, **−0.099R** after. Drifting toward breakeven before costs;
still negative after them.

**09-30 card (24h):** n=25 `eff=5`, meanR +0.092, netR +0.021, all 22
buckets `padj = 1.000`. Window 09-29 14:46 → 09-30 14:46, disjoint from
09-29. Wins 12 of 25 against a 44.0% bar; fill back up to 48/32/28.
`learn:` flipped again (BOOST +0.350 over PENALTY −0.300).

**Three first-look `conv:` days pooled:** AI_PICK n=23 **+0.035R**, PASSED
n=62 **−0.050R** — a gap of +0.085R across readings of −0.61, +0.78 and
+0.22. Still nothing.

**Six disjoint 24h cards since the booking fix:** 168 trades, **−0.041R**
before costs, **−0.122R** after.

**Watch, not finding: SWING has lost 8 of 8 since 09-27** (0/1, 0/2, 0/2,
0/3). `0.5^8` is 0.4%, which overstates it three ways: the streak was found
by scanning six strategies after the fact; three of the eight are one day at
`sd=0.00` and two fell on 09-28 when every strategy lost, so the independent
count is nearer four or five; and SWING won its one trade on 09-24.
Discounted for those it is ordinary. Recorded because Broken #6 calls SWING
the healthiest detector — if the streak survives another week of disjoint
cards it is worth a paired look, not before.

**09-29 card (24h):** n=38 `eff=7`, meanR −0.036, netR −0.113, all 22
buckets `padj = 1.000`. Window 09-28 12:33 → 09-29 12:33, disjoint from the
09-28 card. **Near-total reversal of the day before at strategy level:**
MOMENTUM, the only winner on 09-28 (+0.304), was the worst multi-trade row
(−0.500); SCALP and TRAP, near the bottom on 09-28, were the two winners;
`learn:PENALTY` went from worst to best again. Recorded because the owner
asked the day before whether the bot should learn from each day's best
trade — this card is the answer to that question.

**Second clean `conv:` reading — the opposite sign of the first.** AI_PICK
n=8 wr 87.5% +0.674R against PASSED n=26 −0.107R, the day after AI_PICK
printed −0.743R. **Pooled over both first-look days: AI_PICK n=17 −0.076R,
PASSED n=45 −0.118R — a gap of +0.04R.** The room has added nothing
detectable. UNRANKED is now 14 of 14 losses across the two days: signals
that resolve before the hourly ranking first sees them are fast stop-outs
by construction, which is the exposure asymmetry the first-look rule was
built for, observed directly rather than simulated.

**On-chain went from 2 of 15 to none:** "snapshot 0m old but carries no
symbols". Broken #1, now empty rather than thin.

**Five disjoint 24h cards since the booking fix:** 143 trades, meanR
**−0.064R** before costs, **−0.147R** after.

**09-28 card (24h):** n=39 **`eff=13`** — the largest effective sample a 24h
card has carried — meanR **−0.437**, netR −0.522, t −1.83, ci95 [−0.904,
+0.030]. 25 of 39 stopped out; wins 10 of 37 against a 53.3% bar; rung fill
collapsed to 26/8/5. Every strategy but MOMENTUM (+0.304, n=8) was negative,
and so was every AI, learning and whale label — one bad day seen through
twenty labels, not twenty findings. Lowest `padj` yet (0.502) and still
nothing survives. `max_open=12` at 1% each is ~−15.7% on the paper balance
in one day: correlated positions, not independent bets. MOMENTUM took 13 of
the 15 chase drops — noted, not re-argued (Broken #5, settled).

**First clean `conv:` reading** (first look, deployed 09-27): AI_PICK n=9
wr 11% −0.743R against PASSED n=20 wr 47% −0.133R. The +1.22R of the day
before is gone, as the simulation said it would be; the clean reading does
not show the room adding anything, and on one card cannot show it
subtracting anything either. UNRANKED n=10 at 0% is where fast stop-outs
land — they resolve before any ranking sees them, which is the exposure
asymmetry the first-look rule was built around.

**Four disjoint 24h cards since the booking fix** (09-21, 09-24, 09-27,
09-28; resolution windows do not overlap): 105 trades, n-weighted meanR
**−0.075R**, about **−0.16R** after costs. The owner sends cards on the days
they choose, so these four are not a random sample of days — but the good
day (+0.389) and the bad one (−0.437) are both in it, and together they read
as roughly flat before costs and negative after.

**09-27 card (24h):** n=18 `eff=5`, meanR −0.023, netR −0.117, all 20
buckets `padj = 1.000`. **Rung fill reversed**: 60/36/28 on 09-24 → **50/11/6**.
The three-card rise was described to the owner as the one line improving for
a reason other than luck; three points is not a trend, and it went back the
same way it came. Required WR 51.2% against 50.0% achieved. `conv:` printed
AI_PICK +0.823R vs PASSED −0.398R — discarded, see Broken #4. **TRAP's 1R was
1.54% → cost 0.13R**, now approaching `max_cost_r` the way SCALP did.
On-chain 2 of 15.

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
1.5R because, when this was written, it had never emitted a signal and there
was nothing to contaminate. That premise expired once PREPUMP began trading
(see the 10-04 card); the exemption now stands on no argument at all.

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

1. **On-chain valuation collector** asks for 15 symbols and returned 2 for
   weeks; on 2026-09-29 it returned **none** ("carries no symbols"). The card
   names the cause (rate limit vs universe drift vs empty) — read it before
   guessing.
2. **Whale veto still hard-blocks**, dropping candidates before
   `record_signal`. Every day it runs, the evidence needed to judge it is
   discarded. The only queued item whose cost grows while it waits.
3. **Windowing uses resolution time** where era hygiene wants creation time.
4. **High-Conviction room — now recorded, not yet measured.**
   `wolf/reports/conviction.py` arrived via PR #32 and could not be measured:
   the pick lived only in one overwritten key. Since 2026-09-24 picks are
   stamped on the signal and the Trade Report prints the badge (best rank
   ever reached — what the owner was told). **The diag reads something else:
   the first look** (`conviction_first_rank`/`_first_source`, since
   2026-09-27), because "ever picked" credits the room with whatever makes a
   signal live longer — see the lesson. `conv:` = AI_PICK, PASSED,
   SCORE_ORDERED, UNRANKED. **Read AI_PICK against PASSED.** The first
   reading (+1.22R, 09-27) was discarded as the artifact. Registry:
   `conviction-room-picks-unrecorded`.
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
   **The first read (2026-10-07) was biased against every winner and is
   withdrawn** — losers were priced at the bar close, winners at the live
   re-quote. Fixed the same day; the 122 pre-fix pairs are excluded as
   `legacy` and the clean sample starts at zero. **Clean read 2026-10-20:
   +0.000R over 56 pairs, p 0.998 — closed as not a lever.** Even the biased read had
   MOMENTUM ahead of displaced PREPUMP (+0.234R, n=16), so the floor-bias
   worry above has no trade behind it.

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
python -m pytest            # 1085 tests, ~12s
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
| `wolf/montecarlo.py` | day-block bootstrap of the ledger, net of cost: the losing run to size against (`/montecarlo [STRATEGY]`) |
| `wolf/rebank.py` | one-shot re-booking of timeout rows that forgot a banked rung (`/rebank`, `python -m wolf.rebank`) |
| `wolf/screener.py` | the gate order — cheap disqualifiers before expensive ones |
| `wolf/hypotheses.json` | what has been settled, and what settled it |

Commit messages here explain **why**, in prose, including what was rejected and
what the change costs. Match that. Tests are named as the sentence they prove.
