"""Who is calling: the actor id taken from the request's bearer token.

AgentCore Runtime verifies the JWT (signature, issuer, expiry, client, audience,
and that the gateway relayed it) before the request
reaches the agent, and passes the ``Authorization`` header on because the stack allowlists it.
This module therefore only reads the token: it decodes the payload and returns the ``sub``
claim, the user's stable id in the user pool. It does not verify the signature.

A request without a usable token is an error. There is no default actor: a fallback id would
make every such request share one conversation history.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping


class IdentityError(Exception):
    """The request carries no token from which an actor id can be read.

    Messages name what is wrong, never the header value or a claim: they end up in logs.
    """


def bearer_from_headers(headers: Mapping[str, str] | None) -> str:
    """The JWT of the ``Authorization: Bearer <jwt>`` header, checked for its three parts.

    The agent passes it on unchanged to the Memory gateway (memory.py) and when it completes a
    user's GitHub consent (consent.py): it is the token the Runtime verified, so it names the
    user who sent this request.
    """
    value = next((v for k, v in (headers or {}).items() if k.lower() == "authorization"), None)
    if not value:
        raise IdentityError("missing Authorization header")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer":
        raise IdentityError("Authorization header is not a Bearer token")
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise IdentityError("bearer token is not a JWT (expected three dot-separated parts)")
    return token


def actor_from_headers(headers: Mapping[str, str] | None) -> str:
    """Return the ``sub`` claim of the ``Authorization: Bearer <jwt>`` header."""
    payload = bearer_from_headers(headers).split(".")[1]
    try:
        # JWT segments are base64url without padding; restore it before decoding.
        raw = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
        claims = json.loads(raw)
    except (binascii.Error, ValueError):
        # "from None": the decoder's own message can quote bytes of the token.
        raise IdentityError("bearer token payload is not base64url-encoded JSON") from None
    if not isinstance(claims, dict):
        raise IdentityError("bearer token payload is not a JSON object")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise IdentityError("bearer token has no 'sub' claim")
    return subject
