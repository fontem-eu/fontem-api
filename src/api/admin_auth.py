"""Who may call the operator endpoints.

`fontem-api` is a public read API and has no session of its own — every
other route here is deliberately open. Two are not: merging entity
candidates and deciding a value review both change what the platform
says is true, and until 2026-09-07 both were reachable by anyone on the
internet, because nginx proxies all of /api/ here and nothing checked.

Identity comes from the access token the community API mints, verified
with the shared HS256 secret. The token carries `roles` and
`trust_level` precisely so this service can authorize without reaching
into that service's database or calling it per request.

The definition of "administrator" mirrors authz/policy.py `_is_admin`
there: either signal. Two services disagreeing about who an
administrator is would be a worse bug than the one this closes.
"""
from __future__ import annotations

import os

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

JWT_ALGORITHM = "HS256"

#: Roles that may operate on platform data.
ADMIN_ROLES = frozenset({"admin"})
#: trust_level values that mean the same thing.
ADMIN_TRUST_LEVELS = frozenset({"admin"})

_bearer = HTTPBearer(auto_error=True)


def _secret() -> str:
    """The signing secret, or refuse to authorize anything.

    Deliberately NOT defaulted to a development value. The community API
    can afford that default because a wrong secret there only breaks its
    own logins; here it would mean a deployment that forgot to wire the
    secret silently accepting tokens signed with a string published in
    this repository. A 503 is a loud, fixable failure; that would be a
    quiet reopening of the hole this module exists to close.
    """
    secret = os.environ.get("JWT_SECRET", "")
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="operator endpoints unavailable: JWT_SECRET is not configured",
        )
    return secret


def is_data_admin(claims: dict) -> bool:
    """Mirror of the community API's `_is_admin`, over token claims."""
    roles = claims.get("roles") or []
    if not isinstance(roles, list):
        roles = []
    return (
        bool(ADMIN_ROLES.intersection(roles))
        or claims.get("trust_level") in ADMIN_TRUST_LEVELS
    )


def require_data_admin(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """FastAPI dependency: a valid token belonging to a platform data admin.

    Returns the claims so a handler can attribute the action.
    """
    try:
        claims = jwt.decode(
            credentials.credentials, _secret(), algorithms=[JWT_ALGORITHM],
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=401, detail="Invalid or expired token",
        ) from exc

    if not is_data_admin(claims):
        # 403, not 404: the caller is known, and pretending the route does
        # not exist would make a misconfigured operator account look like
        # a deployment fault.
        raise HTTPException(
            status_code=403,
            detail="this endpoint is restricted to platform data admins",
        )
    return claims
