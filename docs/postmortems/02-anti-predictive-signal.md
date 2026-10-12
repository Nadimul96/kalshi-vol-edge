# Post-mortem: the hourly vol-edge strategy bought longshots

**Summary.** The hourly version of the volatility-edge strategy ("V6") placed 35 live trades on Kalshi's hourly BTC markets on 24 and 25 March 2026. Only 1 of the 35 contracts it bought settled in the money. That sounds damning, but the strategy was buying longshots at an average of 14.4 cents, so a buyer with no edge at all should expect about 5 wins, not half of 35. Against that baseline, 1 win is low (z ≈ −1.96, exact one-sided p ≈ 0.029). That is borderline evidence that the signal picked worse longshots than the market's prices implied, not proof that it was reliably wrong. The strategy was retired by 27 March.

## The numbers

[`scripts/postmortem_02_stats.py`](../../scripts/postmortem_02_stats.py) reproduces every figure here from [`data/02-v6-hourly-trades.csv`](data/02-v6-hourly-trades.csv). That file is cut down from the live system's trade log to three columns per trade: entry price, whether the side bought settled in the money, and the rule that closed the position. It has no tickers, balances, order ids or account data.

```
uv run python scripts/postmortem_02_stats.py
```

| | |
| --- | --- |
| Trades | 35 |
| Bought side settled in the money | 1 (2.9%) |
| Entry price | mean $0.144, range $0.09 to $0.28 |
| Expected wins at the prices paid, Σ p<sub>i</sub> | 5.05 |
| Variance, Σ p<sub>i</sub>(1 − p<sub>i</sub>) | 4.27 (sd 2.07) |
| z = (1 − 5.05) / 2.07 | −1.96 |
| Exact one-sided P(≤ 1 win) | 0.029 |

**The null hypothesis.** If the market's price is a fair probability, a contract bought at p<sub>i</sub> settles in the money with probability p<sub>i</sub>, independently of the other trades. The win count is then Poisson-binomial with mean Σ p<sub>i</sub> and variance Σ p<sub>i</sub>(1 − p<sub>i</sub>). The script computes the exact tail by convolving the 35 Bernoulli distributions; the normal approximation gives 0.025.

**Why it is only borderline.**

- The test is one-sided and was chosen after a bad result was seen. Doubling it for a two-sided test gives about 0.06.
- p<sub>i</sub> is the price paid, not the market's mid. A fill at the ask overstates the market's probability and a fill at the bid understates it, and the log does not record which.
- Prediction markets tend to overprice longshots (the favourite-longshot bias), so some shortfall below Σ p<sub>i</sub> is expected of any longshot buyer, with or without a signal.
- This was one of several strategies reviewed after the fact, and the p-value does not account for that.

An earlier version of this post-mortem compared the win rate with 50% and reported z ≈ −2.87. Both were wrong. A 50% baseline is wrong for 14-cent contracts (it gives z ≈ −5.58), and −2.87 cannot be reproduced from the trade log under either baseline.

## Early exits drove the P&L

27 of the 35 positions were closed before settlement by a THESIS_BREAK rule, which sold when the model's probability fell below a threshold. 4 were held to settlement, 2 hit a trailing stop, 1 took a profit lock and 1 was already closed on the exchange. The win count above is the settlement result for the side bought, which the live system recorded even for positions it had already sold. It measures how the entries were chosen. The realized P&L mostly reflects where the stop rule sold, which a settlement win rate does not capture.

## Likely causes

With 35 trades these are hypotheses, not findings.

- **Selection on the market's information.** On hourly events with dozens of strikes, the signal tended to fire on far strikes that looked cheap relative to realized vol. Many of those were strikes the market was correctly pricing as unlikely.
- **Last hour's realized vol is a poor estimate of the next hour's.** Volatility clusters, and the regime often changed inside the holding window.
- **The offline accuracy number measured the wrong thing.** It scored direction over all candles, not the outcome of the trades the strategy would actually take.

## What changed

- Only the 15-minute arms survived, and they are evaluated per arm (early, mid, late), because the arms behaved very differently live.
- The backtester scores only the trades the strategy would place, at the moment it would place them, with no look-ahead ([`backtest/engine.py`](../../src/kalshi_vol_edge/backtest/engine.py)).
- A win rate is judged against the prices paid. The baseline for a set of trades is the sum of their entry prices, not 50%.

## Lesson

Choose the null before reading the result. A buyer of 14-cent contracts with no edge wins about one time in seven, so 1 in 35 is disappointing, but on its own it is not proof of a signal wired backwards.
