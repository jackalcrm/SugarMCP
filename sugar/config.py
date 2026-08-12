"""Configuration loaded from the environment.

Every value has a documented default except the three credentials, which are required.
See .env.example for the annotated list.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Highest version advertised by Sugar 26.1. Negotiated down on a 301 incorrect_version,
# so a lower instance still works without configuration.
log = logging.getLogger("sugarmcp.config")

DEFAULT_API_VERSION = "v11_27"

# Sugar auto-creates this OAuth key in SugarOAuth2Storage::getClientDetails() when it
# is missing, which makes it the safe fallback for an instance without our package.
FALLBACK_CLIENT_ID = "sugar"


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


# Service name under which secrets are filed in the OS keychain.
KEYRING_SERVICE = "sugarmcp"


def keyring_account(url: str, username: str) -> str:
    """Keychain account name for one (instance, user) pair.

    Includes the URL so the same username on a sandbox and on production are separate
    entries — mixing those up is how someone writes test data into production.
    """
    return f"{url}|{username}"


def _keyring_lookup(url: str, username: str, suffix: str = "") -> str | None:
    """Read a secret from the OS keychain, or None if unavailable.

    Preferred over putting a password in ``.env`` or in the MCP client's config file, both
    of which are plaintext on disk. The keychain is encrypted at rest, access-controlled by
    the OS, and machine-local — it cannot be committed to a repo or synced to a cloud drive
    by accident.

    Optional: the server works fine without ``keyring`` installed, and without any entry.
    """
    # One try around everything, deliberately. Importing `keyring` can fail for more than a
    # missing package — a backend can raise while initialising on a headless or locked
    # system — and none of that is worth taking the server down for when the environment
    # may well supply the password anyway.
    try:
        import keyring

        account = keyring_account(url, username)
        if suffix:
            account = f"{account}|{suffix}"
        return keyring.get_password(KEYRING_SERVICE, account)
    except ImportError:
        return None
    except Exception as exc:  # noqa: BLE001 - a locked or broken keychain must not crash
        log.warning("Could not read the keychain (%s); falling back to environment", exc)
        return None


@dataclass(frozen=True)
class SugarConfig:
    url: str
    username: str
    password: str
    platform: str = "mcp"
    client_id: str = "mcp"
    client_secret: str = ""
    api_version: str = DEFAULT_API_VERSION
    read_only: bool = False
    max_records: int = 20
    max_records_ceiling: int = 100
    cache_dir: Path = Path.home() / ".sugarmcp"
    verify_ssl: bool = True
    timeout: float = 60.0

    @classmethod
    def from_env(cls, *, env_file: str | os.PathLike[str] | None = None) -> "SugarConfig":
        load_dotenv(env_file, override=False)

        url = (os.environ.get("SUGAR_URL") or "").strip().rstrip("/")
        username = os.environ.get("SUGAR_USERNAME") or ""
        password = os.environ.get("SUGAR_PASSWORD") or ""

        # Fall back to the OS keychain so the password need not sit in plaintext in either
        # .env or the MCP client's config file. Environment still wins, so nothing that
        # works today stops working.
        from_keychain = False
        if not password and url and username:
            stored = _keyring_lookup(url, username)
            if stored:
                password, from_keychain = stored, True

        missing = [
            name
            for name, value in (
                ("SUGAR_URL", url),
                ("SUGAR_USERNAME", username),
                ("SUGAR_PASSWORD", password),
            )
            if not value
        ]
        if missing:
            hint = "Copy .env.example to .env and fill them in."
            if missing == ["SUGAR_PASSWORD"]:
                hint = (
                    "Set SUGAR_PASSWORD, or store it in the OS keychain with:\n"
                    "    uv run scripts/set_credentials.py\n"
                    "which keeps it out of every plaintext file."
                )
            raise ConfigError(
                f"Missing required configuration: {', '.join(missing)}. {hint}"
            )

        if from_keychain:
            log.info("Password loaded from the OS keychain for %s", username)

        if url.endswith("/rest"):
            raise ConfigError(
                f"SUGAR_URL should be the instance root, not the REST base — got {url!r}. "
                "Drop the trailing /rest."
            )

        return cls(
            url=url,
            username=username,
            password=password,
            platform=(os.environ.get("SUGAR_PLATFORM") or "mcp").strip(),
            client_id=(os.environ.get("SUGAR_CLIENT_ID") or "mcp").strip(),
            client_secret=(
                os.environ.get("SUGAR_CLIENT_SECRET")
                or _keyring_lookup(url, username, "client_secret")
                or ""
            ),
            api_version=(os.environ.get("SUGAR_API_VERSION") or DEFAULT_API_VERSION).strip(),
            read_only=_bool("SUGAR_READ_ONLY", False),
            max_records=_int("SUGAR_MAX_RECORDS", 20),
            max_records_ceiling=_int("SUGAR_MAX_RECORDS_CEILING", 100),
            cache_dir=Path(
                os.path.expanduser(os.environ.get("SUGAR_CACHE_DIR") or "~/.sugarmcp")
            ),
            verify_ssl=_bool("SUGAR_VERIFY_SSL", True),
            timeout=float(_int("SUGAR_TIMEOUT", 60)),
        )

    def rest_base(self, api_version: str | None = None) -> str:
        return f"{self.url}/rest/{api_version or self.api_version}"

    @property
    def identity(self) -> str:
        """Stable per-(instance, user, platform) key for token and cache paths.

        Hashed rather than slugified because the tuple contains a username and the
        files sit in a shared home directory.
        """
        raw = f"{self.url}|{self.username}|{self.platform}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @property
    def token_path(self) -> Path:
        return self.cache_dir / "tokens" / f"{self.identity}.json"

    @property
    def metadata_cache_dir(self) -> Path:
        return self.cache_dir / "cache" / self.identity
