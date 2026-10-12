"""kve order: demo by default, limits enforced, live only with a typed confirmation."""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_vol_edge.cli import EXIT_OK, EXIT_USAGE, main
from kalshi_vol_edge.config import Environment
from kalshi_vol_edge.kalshi.client import KalshiClient
from kalshi_vol_edge.trading import OrderRefusedError, limit_violations, validate


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def keyed(monkeypatch, tmp_path: Path):
    """Credentials in the environment and a stubbed exchange that records orders."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "kalshi.pem"
    pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(pem))
    monkeypatch.setenv("KALSHI_ENVIRONMENT", "prod")  # must NOT be enough to trade prod
    sent: list[dict] = []
    held = {"position_fp": "0"}

    async def positions(self, settlement_status="unsettled", ticker=None):
        return [{"ticker": ticker, **held}]

    async def place(self, ticker, side, price, contracts, client_order_id, post_only=False):
        sent.append(
            dict(
                env=self.settings.kalshi_environment,
                url=self.base_url,
                ticker=ticker,
                side=side,
                price=price,
                contracts=contracts,
                cid=client_order_id,
            )
        )
        return {"order_id": "o-1", "status": "resting"}

    monkeypatch.setattr(KalshiClient, "get_positions", positions)
    monkeypatch.setattr(KalshiClient, "place_limit_order", place)
    return sent, held


ARGS = ["order", "--ticker", "KXBTC15M-T", "--side", "yes", "--price", "0.40", "--contracts", "5"]


def test_validate_defaults_to_demo_and_rejects_bad_input():
    assert validate("T", "yes", 0.5, 1, live=False).environment == Environment.DEMO
    assert validate("T", "no", 0.5, 1, live=True).environment == Environment.PROD
    for bad in [
        ("", "yes", 0.5, 1),
        ("T", "maybe", 0.5, 1),
        ("T", "yes", 1.2, 1),
        ("T", "yes", 0.5, 0),
    ]:
        with pytest.raises(OrderRefusedError):
            validate(*bad, live=False)


def test_limit_violations_cover_cost_and_position():
    order = validate("T", "yes", 0.50, 6, live=False)
    assert limit_violations(order, Decimal("0"), max_contracts=10, max_usd=10.0) == []
    problems = limit_violations(order, Decimal("-5"), max_contracts=10, max_usd=2.0)
    assert len(problems) == 2 and "KVE_MAX_ORDER_USD" in problems[0]
    assert "holding 5" in problems[1]


def test_demo_order_is_sent_to_demo_even_when_environment_says_prod(keyed, monkeypatch, capsys):
    sent, _ = keyed
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert main(ARGS) == EXIT_OK
    assert len(sent) == 1 and sent[0]["env"] == Environment.DEMO
    assert "demo-api.kalshi.co" in sent[0]["url"]
    assert sent[0]["cid"].startswith("kve-manual-")
    out = capsys.readouterr().out
    assert "environment : DEMO" in out and "sent to demo" in out
    assert "test-key" not in out


def test_demo_order_declined_is_not_sent(keyed, monkeypatch, capsys):
    sent, _ = keyed
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert main(ARGS) == EXIT_USAGE
    assert sent == [] and "not confirmed" in capsys.readouterr().err


def test_position_limit_blocks_before_confirmation(keyed, monkeypatch, capsys):
    sent, held = keyed
    held["position_fp"] = "8"

    def no_prompt(prompt=""):
        raise AssertionError("must not prompt when limits fail")

    monkeypatch.setattr("builtins.input", no_prompt)
    assert main([*ARGS, "--yes"]) == EXIT_USAGE
    assert sent == [] and "KVE_MAX_POSITION_CONTRACTS" in capsys.readouterr().err


def test_live_requires_typed_ticker_at_a_terminal(keyed, monkeypatch, capsys):
    sent, _ = keyed
    assert main([*ARGS, "--live", "--yes"]) == EXIT_USAGE  # --yes never skips a live prompt
    assert main([*ARGS, "--live"]) == EXIT_USAGE  # stdin is not a terminal under pytest
    monkeypatch.setattr("sys.stdin", _TTY())
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert main([*ARGS, "--live"]) == EXIT_USAGE  # "y" is not the ticker
    assert sent == []
    monkeypatch.setattr("builtins.input", lambda prompt="": "KXBTC15M-T")
    assert main([*ARGS, "--live"]) == EXIT_OK
    assert sent[0]["env"] == Environment.PROD and "api.elections.kalshi.com" in sent[0]["url"]
    assert "(real money)" in capsys.readouterr().out


def test_order_without_key_points_to_the_free_demo(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    for var in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(var, raising=False)
    assert main(ARGS) == EXIT_USAGE
    assert "demo exchange is free" in capsys.readouterr().err
