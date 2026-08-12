"""Store Sugar credentials in the OS keychain, so no password sits in a plaintext file.

The alternatives are both plaintext on disk: `.env`, or the `env` block of the MCP client's
config (`claude_desktop_config.json`). The keychain is encrypted at rest, access-controlled by
the OS, machine-local, and cannot be committed to a repo or synced to a cloud drive by
accident — which is the failure mode that actually happens.

Nothing is sent anywhere. This writes to the local keychain only, and the server reads it back
at startup.

    uv run scripts/set_credentials.py                # prompts, stores
    uv run scripts/set_credentials.py --show         # what is stored (never the secret)
    uv run scripts/set_credentials.py --delete       # remove
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from sugar.config import KEYRING_SERVICE, keyring_account

try:
    import keyring
except ImportError:
    print(
        "The 'keyring' package is not installed. Install it with:\n"
        "    uv sync --extra keyring\n"
        "or set SUGAR_PASSWORD in the environment instead."
    )
    raise SystemExit(2)


def resolve_target(args) -> tuple[str, str]:
    """Work out which (instance, user) pair we are storing for."""
    load_dotenv(override=False)
    url = (args.url or os.environ.get("SUGAR_URL") or "").strip().rstrip("/")
    username = (args.username or os.environ.get("SUGAR_USERNAME") or "").strip()

    if not url:
        url = input("Sugar URL (e.g. https://sugar.example.com): ").strip().rstrip("/")
    if not username:
        username = input("Sugar username: ").strip()
    if not url or not username:
        raise SystemExit("A URL and username are both required.")
    return url, username


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="instance URL; defaults to SUGAR_URL")
    parser.add_argument("--username", help="Sugar username; defaults to SUGAR_USERNAME")
    parser.add_argument("--client-secret", action="store_true",
                        help="store the OAuth client secret instead of the password")
    parser.add_argument("--show", action="store_true",
                        help="report whether an entry exists, without revealing it")
    parser.add_argument("--delete", action="store_true", help="remove the stored secret")
    args = parser.parse_args()

    url, username = resolve_target(args)
    account = keyring_account(url, username)
    if args.client_secret:
        account = f"{account}|client_secret"
    label = "client secret" if args.client_secret else "password"

    print(f"backend  : {keyring.get_keyring().__class__.__name__}")
    print(f"service  : {KEYRING_SERVICE}")
    print(f"account  : {account}")

    if args.show:
        stored = keyring.get_password(KEYRING_SERVICE, account)
        print(f"stored   : {'yes — ' + str(len(stored)) + ' chars (not shown)' if stored else 'no'}")
        return 0

    if args.delete:
        try:
            keyring.delete_password(KEYRING_SERVICE, account)
            print(f"\nDeleted the stored {label}.")
        except keyring.errors.PasswordDeleteError:
            print(f"\nNothing stored for that account — nothing to delete.")
        return 0

    secret = getpass.getpass(f"\nSugar {label} (not echoed): ")
    if not secret:
        raise SystemExit("Nothing entered; aborted.")
    confirm = getpass.getpass(f"Confirm {label}: ")
    if secret != confirm:
        raise SystemExit("Entries did not match; nothing was stored.")

    keyring.set_password(KEYRING_SERVICE, account, secret)
    print(f"\nStored the {label} in the OS keychain.")
    print(
        "\nYou can now remove SUGAR_PASSWORD from .env and from your MCP client config.\n"
        "SUGAR_URL and SUGAR_USERNAME are still needed — they are not secrets, and the\n"
        "server uses them to find the right keychain entry.\n\n"
        "Verify with:  uv run scripts/check_session.py"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
