"""Central configuration.

Every value can be set from the environment or a ``.env`` file in the current working
directory. Nothing in this module reads an absolute path: the private key location and the
database path are supplied by the operator, and default to paths relative to the working
directory.

The one behavioural rule inherited from the live system: the demo/prod switch must never be
silent. :meth:`Settings.kalshi_base_url` is explicit about which environment it returns, and
the client logs the environment name (never the key) on first use.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """Kalshi API environment."""

    DEMO = "demo"
    PROD = "prod"


class CredentialsError(RuntimeError):
    """Raised when an authenticated call is attempted without usable credentials."""


class Settings(BaseSettings):
    """Runtime settings. Field names map to upper-cased environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Kalshi credentials. Only these two are required for live use.
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: Path | None = None

    kalshi_environment: Environment = Environment.DEMO
    kalshi_demo_url: str = "https://demo-api.kalshi.co/trade-api/v2"
    kalshi_prod_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_request_timeout_s: float = Field(default=30.0, gt=0)

    # Public market data (open markets, quotes) needs no key and is always read from the
    # production exchange, where the real BTC 15-minute markets trade. Account reads and
    # orders use ``kalshi_environment`` above, which defaults to the demo exchange.
    kalshi_market_data_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_series: str = "KXBTC15M"

    # Order guard rails for `kve order`. Both are checked before anything is sent.
    kve_max_position_contracts: int = Field(default=10, ge=1)
    kve_max_order_usd: float = Field(default=10.0, gt=0)

    # Price feed used for spot. Kalshi settles BTC on CF Benchmarks BRTI, whose constituents
    # include Coinbase; see data/prices.py for why this matters.
    coinbase_base_url: str = "https://api.exchange.coinbase.com"

    # Local state. Relative paths resolve against the working directory.
    kve_db_path: Path = Path("data/kve.sqlite3")
    kve_fixtures_dir: Path = Path("data/sample")

    @property
    def kalshi_base_url(self) -> str:
        """REST base URL for the configured environment."""
        if self.kalshi_environment == Environment.DEMO:
            return self.kalshi_demo_url
        return self.kalshi_prod_url

    def for_environment(self, environment: Environment) -> Settings:
        """A copy of these settings pointed at ``environment``."""
        return self.model_copy(update={"kalshi_environment": environment})

    @property
    def has_credentials(self) -> bool:
        """True when both an API key id and an existing private-key file are configured."""
        return bool(self.kalshi_api_key_id) and bool(
            self.kalshi_private_key_path and self.kalshi_private_key_path.exists()
        )

    def read_private_key_pem(self) -> bytes:
        """Return the PEM bytes of the configured private key.

        Raises:
            CredentialsError: if the path is unset or the file does not exist.
        """
        if not self.kalshi_private_key_path:
            raise CredentialsError("KALSHI_PRIVATE_KEY_PATH is not set")
        if not self.kalshi_private_key_path.exists():
            raise CredentialsError(
                f"KALSHI_PRIVATE_KEY_PATH does not exist: {self.kalshi_private_key_path}"
            )
        return self.kalshi_private_key_path.read_bytes()


def load_settings() -> Settings:
    """Build a Settings instance from the environment (and ``.env`` if present)."""
    return Settings()
