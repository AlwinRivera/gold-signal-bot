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
