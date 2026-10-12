"""kalshi-vol-edge: data engineering around a live Kalshi crypto bracket trading system.

Modules:
    config      pydantic-settings configuration (credentials only from env / .env)
    kalshi      RSA-PSS signed REST client, fee schedule, exchange-as-truth reconciliation
    data        price feeds (Coinbase spot, recorded fixtures) and bracket market records
    strategy    realized-vs-implied vol edge, binary greeks, indicators, Monte Carlo scenarios
    backtest    deterministic replay of the strategy over recorded fixtures
    storage     SQLite schema with idempotent writes
    execution   decision -> order with deterministic idempotency keys
    web         dashboard (FastAPI + Jinja) and the static report built from the same templates
"""

__version__ = "0.2.0"
