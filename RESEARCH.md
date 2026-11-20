# Strategy research plan

Written and committed on 2026-11-16, **before** any of these tests were run.
The rules below do not change after results come in.

## Goal

Find a few small, independent edges that together give a smooth enough equity
curve to pass the FTMO 2-step challenge (Phase 1 +10%, Phase 2 +5%, max loss 10%,
max daily loss 5%) with good odds.

## Data

- 1H candles from Twelve Data, UTC, Jan 2024 to Nov 2026. Weekend candles dropped.
- Daily candles built from the 1H candles (UTC days).
- **In-sample:** 2024-01 to 2025-12. **Out-of-sample:** 2026-01 onwards.

## Markets

| Market | Cost per trade (spread + commission, approx.) |
|---|---|
| XAU/USD (gold) | 0.30 |
| EUR/USD | 0.00012 (1.2 pips) |
| USD/JPY | 0.015 (1.5 pips) |

## Strategies

Stops are in ATR (average true range) so the same rules work on every market.
If a stop and a target are both hit inside one candle, it counts as a loss.
One open position per strategy per market.

| # | Name | Entry | Stop | Exit |
|---|---|---|---|---|
| S1 | EMA pullback (current bot) | Live bot rules, gold only | Live bot rules | 2R target |
| S2 | Previous-day breakout | First 1H close above yesterday's high (buy) or below yesterday's low (sell), 06-15 UTC, once a day | 1.5 x ATR(14, 1H) | 2R target, or close at 20:00 UTC |
| S3 | Asian-session fade | 1H close outside Bollinger Bands (20, 2) during 00-05 UTC, trade back toward the middle | 1.5 x ATR(14, 1H) | Middle band, or close at 07:00 UTC. Skip if the target is less than 0.5R away |
| S4 | Short-term dip in daily trend | Daily bias = yesterday's close vs daily EMA 50. Bull bias: buy when RSI(2, 1H) < 10. Bear bias: sell when RSI(2) > 90. 00-20 UTC | 2 x ATR(14, 1H) | RSI(2) back past 60 (buys) / 40 (sells), or 24 hours |
| S5 | 20-day breakout (trend) | 1H close above the 20-day high (buy) or below the 20-day low (sell) | 2 x daily ATR(14) | Close beyond the 10-day low/high, or Friday 20:00 UTC (no weekend holds) |

S1 runs on gold only (its rules use gold-sized point values). S2 to S5 run on all three markets.

## Pass rules

A strategy/market pair **passes** only if all of these hold:

1. Profit factor at least **1.2 in 2024-2025** and at least **1.2 in 2026**.
2. At least **20 trades in 2026**.

## Combining

Passing pairs are combined at equal risk per trade. Then a Monte Carlo simulation
(re-shuffling whole weeks of real results) estimates the chance of passing both FTMO
phases at 0.25%, 0.5%, 0.75%, 1% and 1.25% risk per trade. The live bot will use the
combination and risk level with the best chance of passing both phases.

If nothing passes, the answer is "no tradable edge found yet", and no challenge is bought.

## Round 1 result (2026-11-16)

No pair passed. Per the plan above: no challenge is bought on this evidence.

## Round 2 (declared 2026-11-16, after seeing Round 1)

Round 1 used all the history we have, so the near-misses can only be judged fairly on
data that did not exist yet when they were picked.

**Candidates** (rule: net profit in BOTH periods, and profit factor at least 1.2 in 2026):

- S1 EMA pullback, XAU/USD (current bot)
- S2 Prev-day breakout, USD/JPY
- S2 Prev-day breakout, XAU/USD
- S4 RSI(2) dip with daily trend, XAU/USD

Rules are frozen exactly as written above. Judged **only on trades from 2026-11-17 onwards**.

**Pass rule:** profit factor at least 1.2 on the new data with at least 25 trades.
**Basket:** the candidates that pass are combined and run through the same FTMO simulation.

The research workflow re-runs automatically on the 1st of every month and posts a summary
to Telegram. First realistic verdict: around February 2027.

## Round 3: day-trading batch (declared 2026-11-19, before testing)

Gold (XAU/USD) only, **15-minute candles**, weekend candles dropped, cost 0.30 per trade.
Every trade is closed the same day. One position per strategy. If stop and target are hit
in the same candle, it counts as a loss. Session times follow daylight saving
(New York = America/New_York, London = Europe/London).

| # | Name | Entry | Stop | Exit |
|---|---|---|---|---|
| D1 | NY opening range breakout | Range = high/low of the 9:30-10:00 New York 15M candles. First 15M close outside the range between 10:00 and 12:00 New York | Opposite side of the range | 2R target, or close at 15:45 New York |
| D2 | Yesterday's high/low fake-out | 07:00-16:00 UTC: a 15M candle trades above yesterday's high but closes back below it (SELL), or below yesterday's low and closes back above it (BUY) | That candle's extreme +/- 0.5 | 2R target, or close at 20:00 UTC |
| D3 | 15M EMA pullback, 1H trend | Bot's pullback rules on 15M (20/50 EMA, wick within 0.1% of EMA20, close on trend side, body >= 35%), only when the 1H EMA20/50 agree. Candles starting 06:00-09:45 or 12:00-15:45 UTC | EMA20 -/+ 0.7 x candle size | 2R target, or close at 20:00 UTC |
| D4 | London open momentum | First London hour (08:00-09:00 London) moves at least 1 x ATR(14, 1H). Then the first 15M candle between 09:00 and 11:00 London that dips to the 15M EMA20 and closes back in the move's direction | That candle's low/high -/+ 0.5 | 2R target, or close at 12:00 London |

Minimum risk 2.0 points (smaller setups skipped). D1, D2, D4: at most one trade a day.

**Pass rule (stricter, since more ideas have now been tried):** profit factor at least
**1.3** in 2024-2025 **and** in 2026, with at least 30 trades in 2026.
Passing ideas join the monthly forward test; only forward-test passes justify a paid challenge.

## Round 3 result (2026-11-19)

No idea passed. Best: D3 (15M pullback with 1H trend) PF 1.00 before 2026 and 1.07 in 2026;
D1 (NY opening range) 1.05 and 1.00. D2 and D4 lost money. Note: 15M history reached back to
Oct 2022, so the "before 2026" sample is Oct 2022 to Dec 2025 (longer than planned; the
verdict is the same either way). Nothing is added to the forward test.

## Target check (2026-11-20)

Tested 2.5R and 3R targets on the bot's unchanged entries. Profit factor rose steadily with the
target in both periods (3R: 1.18 before 2026, 1.50 in 2026; +0.19R per trade vs +0.11R at 2R;
max drawdown -21.5R vs -17.8R). The live bot moves to 3R. Because the target was picked from four
tested values, **"S1 EMA pullback 3R" is added to the Round 2 forward test**, judged only on trades
from 2026-11-20 onward, same pass rule (25+ trades, PF 1.2+). Suggested risk 0.4% per trade so the
backtest's worst drawdown stays under FTMO's 10% limit.

## Target exploration (2026-11-20, later the same day)

Tested 4R-10R. Bigger targets earned more only because winners were held up to the 72-bar
(3 trading day) limit; with a same-day exit every target landed near PF 1.17. Holding 1-3 days
was profitable across the whole 4R-10R range in both periods. The bot moves to **4R, hold up to
3 days** (PF 1.40 before 2026 and 1.40 in 2026, +0.28R per trade, max drawdown -22.4R): the most
consistent value, chosen from the low end of a range that worked, not the single best result.
The forward-test candidate added this morning is changed from 3R to **4R**, judged on trades
from 2026-11-20 onward. Funded FTMO accounts need the **Swing** type to hold over weekends.

## Time limit check (2026-11-20)

The 72-market-hour limit was a default, never tested. Checked 24-120 hours at 4R: every limit
from 48 to 120 hours gave +0.28R to +0.31R per trade and PF 1.37-1.47 in both periods; 24 hours
was a little lower (+0.20R). 72 is not a fragile setting, so it stays.
