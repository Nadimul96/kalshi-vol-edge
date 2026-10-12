# Post-mortem: fees ate the thin edges

**Summary.** Every multi-leg structure tried live (condor, ladder, theta, scalp) lost money, and the single-leg arms were less profitable than estimated because fees were modeled as maker when most fills were taker.

## What happened

Two separate mistakes compounded:

1. **Wrong fee type.** Edge thresholds were computed with the maker fee. In practice 68.6% of fills (497 of 724) were taker. Thin bracket books meant limit orders crossed the spread, and `post_only` rejections fell back to taker orders at four times the maker fee.
2. **Fee multiplication.** Kalshi charges per leg. A two-leg structure pays two entry fees and, if closed early, two exit fees. Structures designed around a few cents of edge per contract had that edge removed before the market moved.

## The math

Kalshi's taker fee is `0.07 × C × P × (1 − P)` dollars for `C` contracts at price `P`. For a direct member's single fill it rounds up to the nearest hundredth of a cent; accounts that clear through an FCM round to the cent, with a per-order rebate of the overpayment ([Kalshi's fee-rounding notes](https://docs.kalshi.com/getting_started/fee_rounding); the assumptions are spelled out in [`kalshi/fees.py`](../../src/kalshi_vol_edge/kalshi/fees.py)). It peaks at P = 0.50, exactly where most two-sided structures sit. A round trip on a two-leg structure near the money pays that four times. Holding a single leg to expiry pays it once, because settlement has no exit fee.

## What changed

- Fee math lives in one module and defaults to taker ([`kalshi/fees.py`](../../src/kalshi_vol_edge/kalshi/fees.py)). `multi_leg_fee` makes per-leg multiplication explicit so a structure cannot hide it.
- The surviving arms are single-leg and hold to expiry.
- The reconciliation layer flags any fill whose recorded fee type disagrees with the exchange (`kve reconcile --dry-run` shows one on the sample).

## Lesson

On a venue with per-contract fees that peak at 50 cents, model the fee you will actually pay, per leg, and assume taker until a fill proves otherwise.
