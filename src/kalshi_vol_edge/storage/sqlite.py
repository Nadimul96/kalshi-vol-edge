"""SQLite persistence for orders, fills and settlements.

Design rules:

* Idempotent writes. Orders are keyed by our ``client_order_id`` and carry a UNIQUE
  ``platform_order_id``; fills are keyed by the exchange ``fill_id``; settlements by ticker.
  Replaying the same exchange data is a no-op, so a sync can be re-run after any crash.
* Money is stored as TEXT decimals, never REAL. Everything that computes with it goes through
  :class:`decimal.Decimal`.
* Connections are closed. :class:`TradeStore` is a context manager and ``close()`` is explicit;
  the live system's ``status`` command once hung for exactly this reason.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id   TEXT PRIMARY KEY,
    platform_order_id TEXT UNIQUE,
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('yes', 'no')),
    action            TEXT NOT NULL DEFAULT 'buy',
    price             TEXT NOT NULL,
    contracts         INTEGER NOT NULL,
    status            TEXT NOT NULL,
    strategy          TEXT,
    arm               TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id         TEXT PRIMARY KEY,
    order_id        TEXT,
    client_order_id TEXT,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL,
    action          TEXT,
    count           TEXT NOT NULL,
    yes_price       TEXT,
    no_price        TEXT,
    is_taker        INTEGER,
    fee_cost        TEXT,
    created_time    TEXT
);

CREATE TABLE IF NOT EXISTS settlements (
    ticker        TEXT PRIMARY KEY,
    market_result TEXT NOT NULL,
    yes_count     TEXT,
    no_count      TEXT,
    revenue       TEXT,
    fee_cost      TEXT,
    settled_time  TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_ticker ON orders(ticker);
CREATE INDEX IF NOT EXISTS idx_fills_ticker ON fills(ticker);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TradeStore:
    """Thin wrapper around a sqlite3 connection with idempotent upserts."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL") if self.path != ":memory:" else None
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def __enter__(self) -> TradeStore:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        """Commit and close. Safe to call twice."""
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    @property
    def closed(self) -> bool:
        """True after :meth:`close`."""
        return self._conn is None

    def _count(self, table: str) -> int:
        row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])

    def commit(self) -> None:
        """Commit pending writes."""
        self._conn.commit()

    # ── Orders ───────────────────────────────────────────────────────────────────

    def upsert_order(
        self,
        client_order_id: str,
        ticker: str,
        side: str,
        price: Any,
        contracts: int,
        status: str,
        platform_order_id: str | None = None,
        strategy: str | None = None,
        arm: str | None = None,
    ) -> bool:
        """Insert or update an order. Returns True if a new row was created.

        Dedupes on ``client_order_id`` (our idempotency key) and, through the UNIQUE
        constraint, on ``platform_order_id``: a second row claiming the same exchange order id
        updates the existing row instead of creating a duplicate.
        """
        now = _now()
        existing_key: str | None = None
        row = self._conn.execute(
            "SELECT client_order_id FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        if row is None and platform_order_id:
            row = self._conn.execute(
                "SELECT client_order_id FROM orders WHERE platform_order_id = ?",
                (platform_order_id,),
            ).fetchone()
        if row is not None:
            existing_key = row["client_order_id"]

        if existing_key is None:
            self._conn.execute(
                """
                INSERT INTO orders (client_order_id, platform_order_id, ticker, side, action, price,
                                    contracts, status, strategy, arm, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'buy', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_order_id,
                    platform_order_id,
                    ticker,
                    side,
                    str(price),
                    int(contracts),
                    status,
                    strategy,
                    arm,
                    now,
                    now,
                ),
            )
            return True

        self._conn.execute(
            """
            UPDATE orders SET
                platform_order_id = COALESCE(?, platform_order_id),
                status = ?, price = ?, contracts = ?, updated_at = ?
            WHERE client_order_id = ?
            """,
            (platform_order_id, status, str(price), int(contracts), now, existing_key),
        )
        return False

    def get_order(self, client_order_id: str) -> dict[str, Any] | None:
        """Fetch one order by our idempotency key."""
        row = self._conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        return dict(row) if row else None

    def has_order(self, client_order_id: str) -> bool:
        """True if we already submitted an order with this idempotency key."""
        return self.get_order(client_order_id) is not None

    def count_orders(self) -> int:
        """Number of order rows."""
        return self._count("orders")

    # ── Fills ────────────────────────────────────────────────────────────────────

    def record_fill(self, fill: dict[str, Any]) -> bool:
        """Insert an exchange fill if unseen. Returns True when a row was inserted."""
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO fills (fill_id, order_id, client_order_id, ticker, side,
                                         action, count, yes_price, no_price, is_taker,
                                         fee_cost, created_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(fill["fill_id"]),
                fill.get("order_id"),
                fill.get("client_order_id"),
                str(fill.get("ticker", "")),
                str(fill.get("side", "")),
                fill.get("action"),
                str(fill.get("count_fp", fill.get("count", "0"))),
                fill.get("yes_price_dollars"),
                fill.get("no_price_dollars"),
                None if fill.get("is_taker") is None else int(bool(fill["is_taker"])),
                fill.get("fee_cost"),
                fill.get("created_time"),
            ),
        )
        return cur.rowcount == 1

    def list_fills(self, ticker: str | None = None) -> list[dict[str, Any]]:
        """Fills, optionally filtered by ticker, in the API's field names."""
        rows = self._conn.execute(
            "SELECT * FROM fills"
            + (" WHERE ticker = ?" if ticker else "")
            + " ORDER BY created_time",
            (ticker,) if ticker else (),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["count_fp"] = d.pop("count")
            d["yes_price_dollars"] = d.pop("yes_price")
            d["no_price_dollars"] = d.pop("no_price")
            d["is_taker"] = None if d["is_taker"] is None else bool(d["is_taker"])
            out.append(d)
        return out

    def count_fills(self) -> int:
        """Number of fill rows."""
        return self._count("fills")

    # ── Settlements ──────────────────────────────────────────────────────────────

    def upsert_settlement(self, settlement: dict[str, Any]) -> bool:
        """Insert or replace a settlement record keyed by ticker. Returns True if changed."""
        before = self._conn.execute(
            "SELECT market_result, settled_time FROM settlements WHERE ticker = ?",
            (settlement["ticker"],),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO settlements (ticker, market_result, yes_count, no_count, revenue, fee_cost,
                                     settled_time)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                market_result = excluded.market_result,
                yes_count = excluded.yes_count, no_count = excluded.no_count,
                revenue = excluded.revenue, fee_cost = excluded.fee_cost,
                settled_time = excluded.settled_time
            """,
            (
                str(settlement["ticker"]),
                str(settlement.get("market_result", "")),
                settlement.get("yes_count_fp"),
                settlement.get("no_count_fp"),
                None if settlement.get("revenue") is None else str(settlement["revenue"]),
                settlement.get("fee_cost"),
                settlement.get("settled_time"),
            ),
        )
        if before is None:
            return True
        return (before["market_result"], before["settled_time"]) != (
            str(settlement.get("market_result", "")),
            settlement.get("settled_time"),
        )

    def count_settlements(self) -> int:
        """Number of settlement rows."""
        return self._count("settlements")
