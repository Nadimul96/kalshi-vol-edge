"""Kalshi REST client with RSA-PSS request signing.

Authentication
--------------
Every authenticated request carries three headers::

    KALSHI-ACCESS-KEY        the API key id
    KALSHI-ACCESS-TIMESTAMP  milliseconds since the epoch
    KALSHI-ACCESS-SIGNATURE  base64(RSA-PSS-SHA256(timestamp + METHOD + full_path))

``full_path`` must include the ``/trade-api/v2`` prefix even though httpx is given a relative
path; signing the relative path produces a signature the server rejects. The signing function
is pure (:func:`sign_request`) so it can be unit-tested against a freshly generated key.

Credentials are read only from :class:`~kalshi_vol_edge.config.Settings`. The key id and the
signature are never logged. A missing key is an error for authenticated endpoints, not a
silent fall-through to an unsigned request.

Public market-data endpoints (``/markets``, ``/events``, ``/series``) do not require
authentication and are sent unsigned when no credentials are configured, so the client can
be used read-only without a key.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from kalshi_vol_edge.config import CredentialsError, Settings, load_settings
from kalshi_vol_edge.logging import get_logger

logger = get_logger(__name__)

API_PREFIX = "/trade-api/v2"


def full_signing_path(path: str) -> str:
    """Return the path Kalshi expects in the signed message (prefix included, query stripped)."""
    bare = path.split("?", 1)[0]
    if bare.startswith(API_PREFIX):
        return bare
    if not bare.startswith("/"):
        bare = "/" + bare
    return API_PREFIX + bare


def sign_request(
    private_key: rsa.RSAPrivateKey,
    method: str,
    path: str,
    timestamp_ms: int,
) -> str:
    """Return the base64 RSA-PSS-SHA256 signature of ``timestamp + METHOD + full_path``."""
    message = f"{timestamp_ms}{method.upper()}{full_signing_path(path)}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def auth_headers(
    private_key: rsa.RSAPrivateKey,
    api_key_id: str,
    method: str,
    path: str,
    timestamp_ms: int | None = None,
) -> dict[str, str]:
    """Build the three Kalshi authentication headers."""
    ts = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": sign_request(private_key, method, path, ts),
    }


def _unwrap(resp: dict[str, Any], key: str) -> dict[str, Any]:
    """Return ``resp[key]`` when the API wraps the object under ``key``, else ``resp``."""
    inner = resp.get(key)
    return inner if isinstance(inner, dict) else resp


class KalshiClient:
    """Async REST client. Use as an async context manager so the connection pool is closed."""

    PUBLIC_PREFIXES = ("/markets", "/events", "/series", "/exchange")

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.base_url = self.settings.kalshi_base_url
        self._http: httpx.AsyncClient | None = None
        self._private_key: rsa.RSAPrivateKey | None = None
        logger.info("kalshi_client_init", environment=self.settings.kalshi_environment.value)

    async def __aenter__(self) -> KalshiClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the HTTP connection pool."""
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.settings.kalshi_request_timeout_s,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        return self._http

    def _load_private_key(self) -> rsa.RSAPrivateKey:
        if self._private_key is None:
            key = load_pem_private_key(self.settings.read_private_key_pem(), password=None)
            if not isinstance(key, rsa.RSAPrivateKey):
                raise CredentialsError("Kalshi private key must be an RSA key")
            self._private_key = key
        return self._private_key

    def _headers_for(self, method: str, path: str) -> dict[str, str]:
        is_public = method.upper() == "GET" and path.startswith(self.PUBLIC_PREFIXES)
        if not self.settings.has_credentials:
            if is_public:
                return {}
            raise CredentialsError(
                f"{method.upper()} {path} requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH"
            )
        return auth_headers(self._load_private_key(), self.settings.kalshi_api_key_id, method, path)

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one request and return the decoded JSON body.

        Raises:
            CredentialsError: authenticated endpoint without credentials.
            httpx.HTTPStatusError: non-2xx response.
        """
        headers = self._headers_for(method, path)
        resp = await self._get_http().request(
            method=method, url=path, params=params, json=json_body, headers=headers
        )
        logger.debug("kalshi_request", method=method.upper(), path=path, status=resp.status_code)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError(f"{method.upper()} {path}: expected a JSON object, got {type(body)}")
        return body

    async def paginate(
        self,
        path: str,
        key: str,
        params: dict[str, Any] | None = None,
        limit: int = 1000,
        max_pages: int = 500,
    ) -> list[dict[str, Any]]:
        """Follow ``cursor`` pagination and return every item under ``key``."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            q: dict[str, Any] = {"limit": limit, **(params or {})}
            if cursor:
                q["cursor"] = cursor
            page = await self.request("GET", path, params=q)
            batch = page.get(key, [])
            items.extend(batch)
            cursor = page.get("cursor") or None
            if not cursor or not batch:
                break
        return items

    # ── Market data (public) ─────────────────────────────────────────────────────

    async def get_markets(
        self,
        series_ticker: str | None = None,
        status: str = "open",
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List markets, optionally filtered by series."""
        params: dict[str, Any] = {"limit": limit, "status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        return await self.request("GET", "/markets", params=params)

    async def get_market(self, ticker: str) -> dict[str, Any]:
        """Fetch one market; for settled markets ``result`` is ``yes``/``no``."""
        resp = await self.request("GET", f"/markets/{ticker}")
        return _unwrap(resp, "market")

    async def get_event(
        self, event_ticker: str, with_nested_markets: bool = True
    ) -> dict[str, Any]:
        """Fetch one event with its markets (one event = one 15-minute or hourly window)."""
        resp = await self.request(
            "GET",
            f"/events/{event_ticker}",
            params={"with_nested_markets": str(with_nested_markets).lower()},
        )
        return _unwrap(resp, "event")

    async def get_orderbook(self, ticker: str) -> dict[str, Any]:
        """Fetch the order book for a market."""
        return await self.request("GET", f"/markets/{ticker}/orderbook")

    # ── Portfolio (authenticated) ────────────────────────────────────────────────

    async def get_balance(self) -> dict[str, Any]:
        """Account balance. ``balance`` and ``portfolio_value`` are integer cents."""
        return await self.request("GET", "/portfolio/balance")

    async def get_fills(self, **params: Any) -> list[dict[str, Any]]:
        """All fills, fully paginated."""
        return await self.paginate("/portfolio/fills", "fills", params)

    async def get_positions(self, settlement_status: str = "unsettled") -> list[dict[str, Any]]:
        """All positions for one settlement bucket (``settled`` or ``unsettled``)."""
        return await self.paginate(
            "/portfolio/positions", "market_positions", {"settlement_status": settlement_status}
        )

    async def get_settlements(self) -> list[dict[str, Any]]:
        """All settlement records, fully paginated."""
        return await self.paginate("/portfolio/settlements", "settlements")

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """Fetch one order by exchange id."""
        resp = await self.request("GET", f"/portfolio/orders/{order_id}")
        return _unwrap(resp, "order")

    # ── Orders (authenticated) ───────────────────────────────────────────────────

    async def place_limit_order(
        self,
        ticker: str,
        side: str,
        price: float,
        contracts: int,
        client_order_id: str,
        post_only: bool = False,
    ) -> dict[str, Any]:
        """Place a buy limit order.

        ``client_order_id`` is the idempotency key: resubmitting the same id is rejected by the
        exchange, so a crash between "order sent" and "order recorded" cannot double-fill.
        Prices are sent as fixed-point dollar strings (``yes_price_dollars`` /
        ``no_price_dollars``), per the 2026 API.
        """
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
        if not 0.01 <= price <= 0.99:
            raise ValueError(f"price must be within [0.01, 0.99], got {price}")
        body: dict[str, Any] = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": int(contracts),
            f"{side}_price_dollars": f"{price:.4f}",
            "client_order_id": client_order_id,
        }
        if post_only:
            body["post_only"] = True
        resp = await self.request("POST", "/portfolio/orders", json_body=body)
        order = _unwrap(resp, "order")
        logger.info(
            "kalshi_order_placed",
            ticker=ticker,
            side=side,
            price=price,
            contracts=contracts,
            status=order.get("status"),
            order_id=order.get("order_id"),
        )
        return order

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns False (and logs) on an HTTP error."""
        try:
            await self.request("DELETE", f"/portfolio/orders/{order_id}")
            return True
        except httpx.HTTPStatusError as exc:
            logger.error("kalshi_cancel_failed", order_id=order_id, status=exc.response.status_code)
            return False
