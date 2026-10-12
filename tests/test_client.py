"""RSA-PSS signing and credential handling."""

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_vol_edge.config import CredentialsError, Environment, Settings
from kalshi_vol_edge.kalshi.client import (
    KalshiClient,
    auth_headers,
    full_signing_path,
    sign_request,
)


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_signing_path_always_carries_api_prefix():
    assert full_signing_path("/portfolio/balance") == "/trade-api/v2/portfolio/balance"
    assert full_signing_path("/trade-api/v2/markets") == "/trade-api/v2/markets"
    assert full_signing_path("markets?limit=5") == "/trade-api/v2/markets"


def test_signature_verifies_with_public_key(key):
    sig = sign_request(key, "get", "/portfolio/balance", 1711234567000)
    key.public_key().verify(
        base64.b64decode(sig),
        b"1711234567000GET/trade-api/v2/portfolio/balance",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )


def test_auth_headers_contain_only_the_three_kalshi_fields(key):
    headers = auth_headers(key, "key-id", "POST", "/portfolio/orders", 1711234567000)
    assert set(headers) == {
        "KALSHI-ACCESS-KEY",
        "KALSHI-ACCESS-TIMESTAMP",
        "KALSHI-ACCESS-SIGNATURE",
    }
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1711234567000"
    assert "BEGIN" not in str(headers)  # no key material


def test_client_without_credentials_allows_public_and_rejects_portfolio(tmp_path):
    settings = Settings(_env_file=None, kalshi_api_key_id="", kalshi_private_key_path=None)
    client = KalshiClient(settings)
    assert client._headers_for("GET", "/markets") == {}
    with pytest.raises(CredentialsError):
        client._headers_for("GET", "/portfolio/balance")
    with pytest.raises(CredentialsError):
        settings.read_private_key_pem()


def test_client_with_key_file_signs_requests(tmp_path, key):
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    key_path = tmp_path / "k.pem"
    key_path.write_bytes(pem)
    settings = Settings(
        _env_file=None,
        kalshi_api_key_id="abc",
        kalshi_private_key_path=key_path,
        kalshi_environment=Environment.PROD,
    )
    assert settings.has_credentials
    assert settings.kalshi_base_url.startswith("https://api.elections.kalshi.com")
    client = KalshiClient(settings)
    headers = client._headers_for("GET", "/portfolio/fills")
    assert headers["KALSHI-ACCESS-KEY"] == "abc" and headers["KALSHI-ACCESS-SIGNATURE"]


async def test_place_limit_order_validates_inputs():
    client = KalshiClient(Settings(_env_file=None))
    with pytest.raises(ValueError):
        await client.place_limit_order("T", "maybe", 0.5, 1, "cid")
    with pytest.raises(ValueError):
        await client.place_limit_order("T", "yes", 1.5, 1, "cid")
