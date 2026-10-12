# Post-mortem: a vol-edge signal that predicted the wrong side

**Summary.** The hourly version of the volatility-edge strategy ("V6") traded 35 live hourly brackets and won 1 of them, a 2.9% win rate. A strategy with no edge at all should have won roughly half. The result was anti-predictive at a z-score of about -2.87. It was shut down on 2026-03-27.

## What happened

The same realized-vs-implied volatility logic that looked reasonable on 15-minute contracts was extended to hourly brackets with many strikes per event. Offline, an associated model reported high accuracy. Live, the strategy consistently bought the side that lost.

## Likely causes

These are the explanations that best fit the trade log. With 35 trades they are hypotheses, not proofs.

- **Selection, not prediction.** On hourly events with dozens of strikes, the signal tended to fire on strikes where the market price looked "too cheap" relative to realized vol. Many of those were strikes the market was correctly pricing as unlikely, so the strategy was effectively betting against the market's own information.
- **Realized vol over the last hour is a poor estimate of the next hour.** Volatility clusters, and the regime frequently changed inside the holding window.
- **The offline accuracy number measured the wrong thing.** It scored direction over all candles, not the conditional outcome on the trades the strategy would actually take.

## What changed

- Only the 15-minute arms survived, and they are evaluated per arm (early, mid, late), because the arms behaved very differently live. The early arm was the only one with a positive record over its sample.
- The backtester scores only the trades the strategy would place, at the moment it would place them, with no look-ahead ([`backtest/engine.py`](../../src/kalshi_vol_edge/backtest/engine.py)).
- A win rate far below 50% is treated as a bug report, not bad luck. A significantly anti-predictive signal is evidence the model is wired to the wrong side or selecting on the market's information.

## Lesson

A signal that is reliably wrong is as informative as one that is reliably right, and it usually means the evaluation was measuring something other than the trades you actually take.
