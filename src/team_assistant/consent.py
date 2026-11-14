"""Bind a user's GitHub consent to that user: the agent's half of the session binding.

After consent in GitHub the browser returns to the user's own make chat, which forwards the
consent session URI here in an ordinary request with the user's Cognito token. The agent calls
CompleteResourceTokenAuth with its role, naming the user by that token (AgentCore devguide,
"OAuth 2.0 authorization URL session binding"). Users have no AWS credentials, so the client
cannot make this call itself.
"""

from __future__ import annotations

import re

from bedrock_agentcore.services.identity import IdentityClient, UserTokenIdentifier

# The pattern of sessionUri in the API reference (CompleteResourceTokenAuth).
SESSION_URI = re.compile(r"urn:ietf:params:oauth:request_uri:[A-Za-z0-9\-._~]+")


class ConsentError(Exception):
    """The consent request is malformed. Messages never quote the request."""


def complete_consent(
    session_uri: object, user_token: str, region: str, client_class=IdentityClient
):
    """Bind the consent session to the user of ``user_token``; ConsentError if malformed."""
    if not isinstance(session_uri, str) or not SESSION_URI.fullmatch(session_uri):
        raise ConsentError("the consent session URI is missing or malformed")
    client_class(region).complete_resource_token_auth(
        session_uri=session_uri, user_identifier=UserTokenIdentifier(user_token=user_token)
    )
