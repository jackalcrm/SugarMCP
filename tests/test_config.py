"""Tests for credential resolution, including the OS keychain fallback."""

from __future__ import annotations

import pytest

from sugar import config as config_module
from sugar.config import ConfigError, SugarConfig, keyring_account


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for key in ("SUGAR_URL", "SUGAR_USERNAME", "SUGAR_PASSWORD", "SUGAR_CLIENT_SECRET",
                "SUGAR_PLATFORM", "SUGAR_READ_ONLY", "SUGAR_CACHE_DIR"):
        monkeypatch.delenv(key, raising=False)
    # Never let a real .env leak into these tests.
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("SUGAR_CACHE_DIR", str(tmp_path))


def test_environment_password_is_used(monkeypatch):
    monkeypatch.setenv("SUGAR_URL", "https://s.test")
    monkeypatch.setenv("SUGAR_USERNAME", "u")
    monkeypatch.setenv("SUGAR_PASSWORD", "from-env")
    assert SugarConfig.from_env().password == "from-env"


def test_keychain_used_when_environment_has_no_password(monkeypatch):
    monkeypatch.setenv("SUGAR_URL", "https://s.test")
    monkeypatch.setenv("SUGAR_USERNAME", "u")
    monkeypatch.setattr(
        config_module, "_keyring_lookup",
        lambda url, user, suffix="": "from-keychain" if not suffix else None,
    )
    assert SugarConfig.from_env().password == "from-keychain"


def test_environment_wins_over_keychain(monkeypatch):
    """Env must keep working for anyone already configured that way."""
    monkeypatch.setenv("SUGAR_URL", "https://s.test")
    monkeypatch.setenv("SUGAR_USERNAME", "u")
    monkeypatch.setenv("SUGAR_PASSWORD", "from-env")
    monkeypatch.setattr(config_module, "_keyring_lookup",
                        lambda *a, **k: "from-keychain")
    assert SugarConfig.from_env().password == "from-env"


def test_missing_password_names_the_keychain_helper(monkeypatch):
    monkeypatch.setenv("SUGAR_URL", "https://s.test")
    monkeypatch.setenv("SUGAR_USERNAME", "u")
    monkeypatch.setattr(config_module, "_keyring_lookup", lambda *a, **k: None)
    with pytest.raises(ConfigError) as caught:
        SugarConfig.from_env()
    assert "set_credentials.py" in str(caught.value)


def test_keychain_failure_does_not_crash(monkeypatch):
    """A locked or broken keychain must degrade, not take the server down."""
    def boom(*a, **k):
        raise RuntimeError("keychain locked")
    monkeypatch.setattr(config_module, "keyring_account", boom)
    monkeypatch.setenv("SUGAR_URL", "https://s.test")
    monkeypatch.setenv("SUGAR_USERNAME", "u")
    monkeypatch.setenv("SUGAR_PASSWORD", "from-env")
    assert SugarConfig.from_env().password == "from-env"


def test_account_separates_instances():
    """Same username on sandbox and production must not share one entry."""
    assert keyring_account("https://sandbox", "u") != keyring_account("https://prod", "u")


def test_identity_separates_instances_and_users():
    a = SugarConfig(url="https://a", username="u", password="x")
    b = SugarConfig(url="https://b", username="u", password="x")
    c = SugarConfig(url="https://a", username="v", password="x")
    assert len({a.identity, b.identity, c.identity}) == 3
