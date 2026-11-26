# Brief for the OpenBot challenger

You are building a crypto trading system from scratch and running it on paper.
A separate system, **Wolf**, has spent three months doing the same thing. This
brief gives you what Wolf learned, so you do not spend your time walking into
the same dead ends. **It gives you no strategy.** The strategy is yours.

Wolf's result so far, honestly: about **zero before costs, about −0.1R per
trade after costs**, over thousands of trades. No strategy it ran has a
demonstrated edge. If you find one, it is new.

---

## 1. The rules of this experiment

1. **Paper only.** Never create or use exchange API keys. Never place real
   orders. No Telegram.
2. **Universe:** BTC, ETH, SOL, BNB, XRP, ADA, AVAX, LINK, LTC, DOGE
   (USDT pairs).
3. **Every decision goes to the referee the moment you make it:**

   ```
   POST {WOLF_URL}/openbot/signals
   Header: X-API-Key: {KEY}
   {"symbol": "BTC", "direction": "LONG", "sl": 58000, "tp": 62000,
    "hours": 24, "reason": "one sentence"}
   ```

   - The referee fills you at the **live market price when the call arrives**.
     Your SL and TP must be valid against that price.
   - SL 0.2–15% from entry, time limit 1–72 hours, at most 5 open positions,
     one per coin. Anything else is refused, and the refusal is counted.
   - Grading uses 15m bars after the entry. A bar that touches both SL and TP
     counts as SL. If neither is hit, the trade closes at the last price
     inside the time limit.
   - Every trade pays **20bps round trip, charged against your own stop**.
     A 0.5% stop pays 0.4R in cost; a 4% stop pays 0.05R.
   - **A trade not sent to the referee does not exist.**
4. Never edit, cancel or resend a refused trade with tweaked numbers to get it
   through. Fix the system instead.
5. **Your scorecard is only** `GET {WOLF_URL}/openbot?format=text`. Do not
   report a win rate or profit you computed yourself.
6. **The read happens at 100 closed trades.** "Edge" means mean net R > 0 AND
   the t-statistic of daily mean returns ≥ 2. Anything less is "not shown",
   however good it looks.
7. Write your strategy down **before** the first trade. A change resets the
   count. Say so when you make one.
8. Scan on a fixed schedule (for example every 4 hours), not only when the
   market looks exciting.

---

## 2. What Wolf learned (read before designing anything)

**Costs decide everything on short timeframes.** Cost in R grows as the stop
shrinks: roughly 0.05R per trade on 4h stops, 0.12R on 1h, 0.3R on 15m and
more below that. Every low-timeframe idea Wolf tested had a gross edge smaller
than its cost. A strategy that is +0.05R before costs on 15m is a losing
strategy.

**Win rate is the wrong target.** One rule won 66% of its trades and lost
money. Another won 28% and was flat. Only expectancy after costs matters. A
high win rate usually just means a near target and a far stop.

**Beat a random baseline, not zero.** Crypto drifted up for years, so almost
any long rule looks profitable. Wolf always compares a rule against **the same
trades shifted to random days** (same coin, side, stop, target, holding time).
Only the timing is being tested. A rule that earns +0.35R while random days
with the same exits earn +0.19R has an edge of +0.16R, not +0.35R. The baseline
must move all trades together, because the ten coins move together.

**Much of any result is just BTC.** Over half of Wolf's gross result was BTC
moving under the positions. A long book in a rising market looks skilled.
Check how much of your result is BTC's move.

**Overlapping trades are not independent.** Five positions open at once in a
correlated basket is closer to one bet than five. One bad day stopped out a
dozen positions together. Count days, not trades, when you judge a streak.

**The best of many is positive by chance.** If you try ten variants and keep
the best one, it will look good on pure noise. Decide the rule and the pass
bar before you look at results. Do not tune on the same data you test on.

**Streaks lie.** Patterns held for four or eight readings and then reversed on
the same day. A streak built from overlapping windows is nearly one
observation.

**An LLM veto adds nothing measurable here.** Wolf ran an AI debate layer, an
AI veto and an AI "conviction ranking" over its signals. None of them separated
from chance. If you use an LLM to decide trades, you are testing exactly this
again, so make it count.

**A filter you cannot measure is a blind spot.** Anything that decides what
*does not* trade must record what it dropped. Otherwise its value can never be
judged.

**Silence is not information.** A rule that never fires may be broken, not
strict. Check that your system actually runs and that its conditions can be
met at the same time.

---

## 3. Already tested and rejected: do not retry without a genuinely new idea

All on the same ten coins, with fixed rules written before the test, against a
random-timing baseline, after costs.

| Idea | Result |
|---|---|
| Elliott Wave 2→3 entry (fixed-rule wave count) | worse than random timing, both sides |
| Bollinger lower-band dip, 4h longs | beat only 6% of random long entries; bull market did the work |
| Ichimoku cloud breakout | failed both sides |
| Ichimoku TK cross, long | failed out of sample (2024+ negative) |
| Liquidity sweep taken only in London/New York hours | no better than other hours |
| D1/H4 trend + H1 sweep of a swing, target opposite swing (1:5 style) | gross ≈ 0, net −0.31R |
| Daily high/low "prediction" zones, fade the zone | 66% win rate, −0.08R net |
| Averaging down with a half-gap exit on the added lot | higher win rate, 50% deeper worst losses, mean not better |
| Weekly relative momentum (long top 3, short bottom 3) | failed; negative from 2024 |
| BTC-regime 20-bar breakout on 4h, ATR trail | +0.08R net but beat only 95% of random (bar was 97.5%); now running forward on paper as "v2" |
| The same breakout on 1h / 15m | net −0.03R / −0.27R: cost wall |
| The same breakout on 1D, spot long only in a bull regime | +0.35R net, but random days earned +0.19R: mostly drift |
| Tighter entry filters to raise win rate | win rate up, expectancy not |
| Re-cutting take-profit ladders (6 variants) | no change in expectancy |
| Tighter cost gate (only wide stops) | net unchanged |
| Session, whale-flow, liquidity and "combo" filters on the old detectors | patterns, none separated from chance |

Still open in Wolf: whether TK cross SHORT on 4h (passed one offline test, just
over the bar) survives forward, and whether the 4h breakout beats simply
holding the basket while BTC is above its 300-day mean.

---

## 4. How to judge yourself before the referee does

1. Write the rule and the pass bar down first.
2. Backtest on years of data, after 20bps cost on your own stop.
3. Compare against the same trades shifted to random days. Report the share of
   random runs you beat; 97.5% is a reasonable bar for one test, higher if you
   have tried several.
4. Check both halves of the history separately. A rule that only worked in
   2020–21 is a bull-market rule.
5. Only then trade it forward through the referee, unchanged.
