"""Shared fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = REPO_ROOT / "data" / "sample"


@pytest.fixture(scope="session")
def sample_dir() -> Path:
    """Path to the committed sample fixture."""
    assert SAMPLE_DIR.is_dir(), f"sample fixture missing: {SAMPLE_DIR}"
    return SAMPLE_DIR
