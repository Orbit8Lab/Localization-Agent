"""HTTP Basic auth for the beta deployment.

Deliberately the smallest thing that closes the door. It is not the
final auth story — per-user accounts and real sessions arrive with the
commercial phase — but an API that creates jobs and spends model tokens
must not sit open on a public URL for even a week.

Two properties that matter more than the mechanism:

* **Fail closed.** If `ORBIT8_BASIC_AUTH` is unset in a deployment, the
  app refuses to start rather than serving unauthenticated. A missing
  env var is the most likely way to accidentally publish an open API,
  so it cannot be the quiet default.
* **Constant-time comparison.** `==` on a secret leaks its prefix
  through timing. `secrets.compare_digest` does not.
"""
from __future__ import annotations

import os
import secrets
from typing import Dict, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_scheme = HTTPBasic(auto_error=False)


def parse_users(raw: Optional[str]) -> Dict[str, str]:
    """`"alice:pw1,bob:pw2"` -> {"alice": "pw1", "bob": "pw2"}.

    A password containing ':' is preserved: only the first colon splits.
    """
    users: Dict[str, str] = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise ValueError(
                f"malformed ORBIT8_BASIC_AUTH entry {pair!r}; want user:password")
        user, password = pair.split(":", 1)
        if not user or not password:
            raise ValueError("ORBIT8_BASIC_AUTH entries need a user and a password")
        users[user] = password
    return users


def build_auth_dependency(users: Dict[str, str]):
    """Return a dependency enforcing `users`, or a no-op if empty.

    Empty is allowed only because local development and the test suite
    run without credentials; `create_app` is what refuses to start an
    unauthenticated *deployment*.
    """
    if not users:
        async def allow_all() -> Optional[str]:
            return None
        return allow_all

    async def verify(
            creds: Optional[HTTPBasicCredentials] = Depends(_scheme)) -> str:
        unauthorized = HTTPException(
            status.HTTP_401_UNAUTHORIZED, "not authenticated",
            headers={"WWW-Authenticate": "Basic"})
        if creds is None:
            raise unauthorized
        expected = users.get(creds.username)
        # Compare even when the user is unknown, against a dummy of the
        # same shape: returning early on an unknown username makes user
        # enumeration a timing measurement.
        reference = expected if expected is not None else "\0" * 32
        ok = secrets.compare_digest(creds.password, reference)
        if expected is None or not ok:
            raise unauthorized
        return creds.username

    return verify


def users_from_env() -> Dict[str, str]:
    return parse_users(os.environ.get("ORBIT8_BASIC_AUTH"))
