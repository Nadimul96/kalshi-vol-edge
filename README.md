# kalshi-vol-edge

A small trading system for Kalshi's 15-minute BTC bracket contracts, built around the parts that are easy to get wrong: which index actually settles the contract, what the exchange says happened versus what your database thinks happened, and what fees really cost.

[![CI](https://github.com/Nadimul96/kalshi-vol-edge/actions/workflows/ci.yml/badge.svg)](https://github.com/Nadimul96/kalshi-vol-edge/actions/workflows/ci.yml)
![Python 3.11 | 3.12](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

> **Not financial advice.** The live version of this system traded real money for about seven months and ended with a small net loss. The [post-mortems](#post-mortems) explain what the data showed.

## Architecture

```
  Coinbase 1m candles ─┐                    (BRTI constituent; basis measured, not assumed)
                       ├─▶ strategy/vol_edge ──▶ execution ──▶ kalshi/client ──▶ Kalshi API
  Kalshi market data ──┘    realized vs implied   idempotent      RSA-PSS signed
                            vol, z-score entry    client_order_id
                            fee-aware edge              │
                                                        ▼
                                      kalshi/truth ◀── fills, positions, settlements
                                      exchange = source of truth
                                                        │
                                                        ▼
                                      storage/sqlite   idempotent writes, Decimal money

  backtest/engine replays the same strategy over recorded fixtures, minute by minute.
```

## What's technically interesting

| Piece | Where | Why it matters |
| --- | --- | --- |
| Settlement-aware pricing | [`data/prices.py`](src/kalshi_vol_edge/data/prices.py) | Kalshi BTC settles on the CF Benchmarks BRTI, a 60-second average of five exchanges. Binance is not one of them. The feed uses a constituent, and every backtest reports the measured basis between feed and settlement. |
| Exchange as source of truth | [`kalshi/truth.py`](src/kalshi_vol_edge/kalshi/truth.py) | Local state is a claim; exchange fills are facts. Reconciliation catches phantom orders, unlogged fills after a crash, and fees recorded as maker that were charged as taker. |
| Idempotent execution | [`execution.py`](src/kalshi_vol_edge/execution.py), [`storage/sqlite.py`](src/kalshi_vol_edge/storage/sqlite.py) | A deterministic `client_order_id` per strategy, arm and event means a crash-and-retry is rejected rather than double-filled. Fills are keyed by exchange id, so re-syncing is a no-op. Money is stored as decimal text, never floats. |
| Fee-aware edge | [`kalshi/fees.py`](src/kalshi_vol_edge/kalshi/fees.py) | Edge is computed with taker fees by default, because most live fills were taker. Multi-leg fees are multiplied explicitly. |
| Loud-failure backtesting | [`backtest/engine.py`](src/kalshi_vol_edge/backtest/engine.py) | No silent fallback to synthetic data, no look-ahead, no randomness. Quotes that weren't recorded are labeled as modeled in the output. |
| Signed client | [`kalshi/client.py`](src/kalshi_vol_edge/kalshi/client.py) | RSA-PSS request signing as a pure, unit-tested function. Credentials only from the environment, never logged. |

## Quickstart

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

kve backtest --fixtures data/sample      # replay the recorded sample, no credentials needed
kve reconcile --dry-run                  # reconcile recorded local orders against recorded fills
pytest -q && ruff check .
```

Live endpoints need `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` (see [`.env.example`](.env.example)). Nothing in this README requires them.

## Results on the sample fixture

The sample in [`data/sample`](data/sample) is about 36 hours of recorded Coinbase candles and 30 settled 15-minute Kalshi markets from March 2026. Order books were not recorded, so entry quotes are modeled; the output says so.

```
kalshi-vol-edge backtest
  fixture          : data/sample
  series / asset   : KXBTC15M / BTC
  quotes           : MODELED (no recorded order books): fair value at implied vol 40% + 2c half-spread
  markets replayed : 30  (decision minutes: 420)

  trades           : 13
  wins / win rate  : 5 / 38.5%
  capital deployed : $21.45
  gross P&L        : $+7.55
  fees (taker)     : $1.05
  net P&L          : $+6.50
  max drawdown     : $6.98

  by arm:
    early  trades=13  win_rate= 38.5% net=$+6.50 fees=$1.05

  settlement basis (Coinbase last 1m close - BRTI settlement):
    markets=30  mean|basis|=$17.31  median|basis|=$13.15  max|basis|=$52.38
    direction flips (Coinbase side of strike != settled side): 2
```

Thirteen trades on modeled quotes is not evidence of an edge, and the P&L line depends on the quote model. The settlement-basis line does not: in 2 of 30 markets the feed was on the wrong side of the strike at expiry.

The reconciliation dry run on the recorded fixtures finds one fee recorded as maker that the exchange charged as taker, one phantom local order, and one exchange fill missing from local state.

## Post-mortems

1. [Pricing against the wrong index](docs/postmortems/01-settlement-source.md): a position priced correctly against Binance lost by $32 against BRTI.
2. [A vol-edge signal that predicted the wrong side](docs/postmortems/02-anti-predictive-signal.md): 1 win in 35 live hourly trades.
3. [Fees ate the thin edges](docs/postmortems/03-fees-and-multi-leg.md): taker fills and per-leg fees.

## Verify

Run on macOS, Python 3.11.6:

| Command | Result |
| --- | --- |
| `ruff check .` | All checks passed |
| `pytest -q` | 90 passed |
| `kve backtest --fixtures data/sample` | report above, exit 0 |
| `kve backtest --fixtures /nonexistent` | `fixture directory not found`, exit 2 |

CI runs the same on Python 3.11 and 3.12.

## Scope

Extracted from a larger private system. Deliberately left out: sports and other non-crypto markets, other venues, model-training code, and the deployment scripts. What remains is the core that can be run and verified without an account.

## License

MIT. See [LICENSE](LICENSE).
