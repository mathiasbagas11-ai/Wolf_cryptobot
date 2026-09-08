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
outcome, so the veto's own correctness is unmeasurable. Found three times —
the AI veto (rejected), the whale veto (identified, still hard-blocking), and
the learning blacklist (fixed 2026-09-07, and the worst of the three because a
bench is an absorbing state). **Whenever a component decides what does not
happen, ask what it makes unmeasurable.**

**Level questions are unaffordable; paired questions are not.** "Does the bot
make money" needs thousands of trades. "Is rule A better than B on these same
trades" got a decisive answer at n=66, because the market move cancels. Reach
for `/whatif` before reaching for patience.

**Two numbers under one name is how a component hides.** The cost card quoted
an assumed round trip (fees + slippage) beside a measured one (fees + spread)
and called both a round trip. The diag reported `eff_n_floor` on one line and
undiscounted `t` on the next. In both cases the reader was left to reconcile
figures that were never the same quantity.

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
| **2026-09-07** | learning moved to monitor mode — **current sample starts here** |

Eras should be segmented by signal **creation** date. `/diag` and `/whatif`
currently window on **resolution** time; that is a known, unfixed gap.

## Already tested — check before proposing anything

`wolf/hypotheses.json`, readable in Telegram via `/tested`. Fifteen entries.
`OPEN` (not measured) is deliberately distinct from `INCONCLUSIVE` (measured,
separated nothing). **Add an entry whenever something is settled, and never
duplicate its content into this file** — one of them would go stale.

Large rejections worth knowing without opening it: exit-geometry re-cut (was
believed the biggest lever; measured across 6 variants, does not move),
tighter entries for win rate, cost-model refinement, LLM in the signal path,
and the 350-trade sample target.

## Status — 2026-09-08, HEAD `8719ecb`

950 tests green. Working tree clean.

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

Not yet built, offered and not taken: Deflated Sharpe Ratio (the registry is
its trial counter); splitting `sentiment` from `materiality` in
`wolf/news/signal.py`.

## Practical

```bash
pip install -r requirements-dev.txt
python -m pytest            # 950 tests, ~5s
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
