"""Capture an ACL fixture as another user, via ``POST /oauth2/sudo/:user_name``.

The ACL logic is the highest-risk part of this server — because Sugar strips ``yes`` values,
an inverted check grants everything silently rather than failing loudly. Testing it needs
payloads from users who are actually restricted, and an admin's own ACL block shows almost
no denials.

Sudo is the right tool for this: it is admin-only, and per its own documentation "the calling
user does not lose their existing token, this one is granted in addition", so capturing a
fixture does not disturb the admin session. Two details worth knowing:

* It defaults to platform ``base``, which would evict that user's Sugar web UI session. This
  script always passes ``mcp``.
* It returns no refresh token, so the sudo session simply expires after ``expires_in``.

**Only the permission map is written to disk.** These are real user accounts, and fixtures
are committed, so the user name, id, email and every other personal field are dropped — an
ACL block is module and field permissions and nothing else. Files are named for the role
being exercised, not the person holding it.

    uv run scripts/capture_acl_fixture.py <user_name> <fixture-label>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sugar import SugarConfig, SugarSession
from sugar.acl import AclIndex

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# A platform slot of its own. Sudo on the *caller's* platform evicts the caller's own token
# — SugarOAuth2StorageBase allows one session per platform, and sudo takes the slot. Verified
# the hard way: sudo-ing as self on `mcp` killed the admin session mid-script.
SUDO_PLATFORM = "mcp_fixture"


def sudo_token(session: SugarSession, user_name: str) -> str:
    """Obtain an access token for another user. Requires the caller to be an admin."""
    response = session.http.post(
        f"{session.config.rest_base()}/oauth2/sudo/{user_name}",
        json={
            "client_id": session.caps.client_id,
            # Never `base` (the endpoint's own default) — that would end the target user's
            # Sugar web UI session. Never the caller's platform either; see above.
            "platform": SUDO_PLATFORM,
        },
        headers={"OAuth-Token": session.access_token()},
    )
    payload = response.json()
    if response.status_code >= 300 or "access_token" not in payload:
        raise SystemExit(
            f"sudo failed ({response.status_code}): "
            f"{payload.get('error')}: {payload.get('error_message')}"
        )
    return payload["access_token"]


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    user_name, label = sys.argv[1], sys.argv[2]

    config = SugarConfig.from_env()
    session = SugarSession(config)

    token = sudo_token(session, user_name)
    response = session.http.get(
        f"{config.rest_base()}/me", headers={"OAuth-Token": token}
    )
    me = response.json().get("current_user", {})

    acl = me.get("acl") or {}
    index = AclIndex({"acl": acl} and acl, is_admin=str(me.get("type")) == "admin")

    denied = index.denied_modules()
    restricted_fields = {
        name: sorted(module_acl.fields)
        for name, module_acl in index._modules.items()
        if module_acl.fields
    }
    hidden = {
        name: sorted(f for f, spec in module_acl.fields.items() if spec.hidden)
        for name, module_acl in index._modules.items()
        if any(spec.hidden for spec in module_acl.fields.values())
    }

    print(f"user           {user_name}")
    print(f"is_admin       {index.is_admin}")
    print(f"acl modules    {len(acl)}")
    print(f"denied modules {len(denied)}  {denied[:8]}")
    print(f"modules with field restrictions  {len(restricted_fields)}")
    print(f"modules with HIDDEN fields       {len(hidden)}  {list(hidden)[:6]}")
    if hidden:
        first = next(iter(hidden.items()))
        print(f"  e.g. {first[0]}: {first[1][:6]}")

    # Permission map only — no personal data reaches the fixture.
    out = FIXTURES / f"me_acl_{label}.json"
    out.write_text(json.dumps(acl, indent=1, sort_keys=True))
    print(f"\nwrote {out.relative_to(Path.cwd())} ({out.stat().st_size:,} bytes, "
          "permissions only)")

    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
