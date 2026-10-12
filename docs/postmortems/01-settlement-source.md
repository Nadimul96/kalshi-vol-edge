# Post-mortem: pricing against the wrong index

**Summary.** A NO position on a BTC hourly bracket at the $68,400 strike lost by $32. The position was priced correctly against Binance. Kalshi settles on the CF Benchmarks Bitcoin Real-Time Index (BRTI), and BRTI finished on the other side of the strike.

## What happened

The first version of the price feed used Binance because it had the friendliest public API and the deepest book. The strategy compared spot to strike, estimated the probability the lead would hold, and bought the cheap side. On this contract the Binance price sat comfortably below the strike at expiry. BRTI settled $32 above it.

## Why

- BRTI is a volume-weighted aggregate of Coinbase, Bitstamp, Kraken, Gemini and LMAX Digital. **Binance is not a constituent.**
- The settlement value for 15-minute and hourly contracts is the average of the 60 one-second BRTI readings in the final minute, not a single tick.
- During fast markets the Binance/BRTI gap was routinely $50 to $200, which is larger than the lead the strategy needed to be "safe" near expiry.

## What changed

- The reference feed is Coinbase, a BRTI constituent ([`data/prices.py`](../../src/kalshi_vol_edge/data/prices.py)).
- Coinbase is still only a proxy, so the backtester measures the gap directly. On the recorded sample in this repo, the last Coinbase one-minute close differs from the reported BRTI settlement by a mean of about $17, with a max of about $52, and lands on the opposite side of the strike in 2 of 30 markets. That is printed on every backtest run under "settlement basis".
- Near-expiry entries require a lead that clears the measured basis, not just the volatility band.

## Lesson

Know exactly what instrument settles your contract, and measure the basis between your feed and that instrument instead of assuming it is zero.
