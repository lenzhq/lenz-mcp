"""The OAuth bridge's per-call credential: an act-as-user assertion.

An OAuth tool call cannot forward the user's WorkOS token to the API (it is
audience-bound to the connector), so each call carries a short-lived
assertion naming the user, signed with a key only this service and the API
share. The API verifies it and re-checks the user's
status on every call.

The assertion is an HS256 JWT: ``iss`` this service, ``aud`` the API, ``sub``
the user's id as a decimal string, ``iat``/``exp`` exactly
``ASSERTION_LIFETIME_S`` apart, and a random ``jti`` the API logs for
correlation. The API accepts it only under a key in its
``MCP_SERVICE_SIGNING_KEYS``; the connector signs with the first of them.
"""

from __future__ import annotations

import secrets
import time

import jwt

from lenz_mcp import config

# The verifier's contract, as the API expects it. A contract test on the API
# side pins each value, and that a token minted here verifies there.
ASSERTION_ALGORITHM = 'HS256'
ASSERTION_ISSUER = 'lenz-mcp'
ASSERTION_AUDIENCE = 'lenz-api'
# One call's credential: short-lived, and every poll of a long wait mints its
# own.
ASSERTION_LIFETIME_S = 60


class BridgeNotConfigured(RuntimeError):
    """No signing key is set, so no assertion can be minted."""


def mint_service_assertion(user_id: int) -> str:
    """Sign a short-lived act-as-user assertion for one API call."""
    key = config.SERVICE_SIGNING_KEY
    if not key:
        raise BridgeNotConfigured('MCP_SERVICE_SIGNING_KEYS must be set to mint service assertions.')
    now = int(time.time())
    claims = {
        'iss': ASSERTION_ISSUER,
        'aud': ASSERTION_AUDIENCE,
        'sub': str(int(user_id)),
        'iat': now,
        'exp': now + ASSERTION_LIFETIME_S,
        'jti': secrets.token_hex(16),
    }
    return jwt.encode(claims, key, algorithm=ASSERTION_ALGORITHM)
