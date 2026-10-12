"""kve doctor, offline."""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from kalshi_vol_edge.cli import EXIT_OK, EXIT_USAGE, main
from kalshi_vol_edge.config import Settings
from kalshi_vol_edge.doctor import check_credentials, check_fixtures, run_doctor


def _pem(key, path: Path) -> Path:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


def _settings(**kw) -> Settings:
    return Settings(
        _env_file=None, **{"kalshi_api_key_id": "", "kalshi_private_key_path": None, **kw}
    )


def test_missing_key_is_optional_not_a_failure(sample_dir):
    checks = run_doctor(_settings(kve_fixtures_dir=sample_dir), offline=True)
    by_name = {c.name: c for c in checks}
    assert by_name["kalshi key"].status == "optional"
    assert by_name["fixtures"].status == "ok"
    assert not any(c.status == "fail" for c in checks)


def test_credential_problems_are_explained(tmp_path):
    assert check_credentials(_settings(kalshi_api_key_id="k")).status == "fail"
    missing = check_credentials(
        _settings(kalshi_api_key_id="k", kalshi_private_key_path=tmp_path / "x")
    )
    assert missing.status == "fail" and "does not exist" in missing.detail
    garbage = tmp_path / "bad.pem"
    garbage.write_text("not a key")
    assert (
        "could not be parsed"
        in check_credentials(
            _settings(kalshi_api_key_id="k", kalshi_private_key_path=garbage)
        ).detail
    )
    ec_key = _pem(ec.generate_private_key(ec.SECP256R1()), tmp_path / "ec.pem")
    assert (
        "not an RSA key"
        in check_credentials(
            _settings(kalshi_api_key_id="k", kalshi_private_key_path=ec_key)
        ).detail
    )
    rsa_key = _pem(
        rsa.generate_private_key(public_exponent=65537, key_size=2048), tmp_path / "r.pem"
    )
    ok = check_credentials(_settings(kalshi_api_key_id="k", kalshi_private_key_path=rsa_key))
    assert ok.status == "ok" and "demo" in ok.detail


def test_missing_fixture_fails(tmp_path):
    assert check_fixtures(_settings(kve_fixtures_dir=tmp_path / "nope")).status == "fail"


def test_doctor_cli_exit_codes(sample_dir, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(sample_dir.parents[1])
    for var in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(var, raising=False)
    assert main(["doctor", "--offline"]) == EXIT_OK
    assert "ready" in capsys.readouterr().out
    monkeypatch.setenv("KVE_FIXTURES_DIR", str(tmp_path / "nope"))
    assert main(["doctor", "--offline"]) == EXIT_USAGE
