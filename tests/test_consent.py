"""consent.py: the agent binds a consent session to the user of the request's token."""

from __future__ import annotations

import pytest

from team_assistant import consent

URI = "urn:ietf:params:oauth:request_uri:abc-123_x.y~z"


class Client:
    made = []

    def __init__(self, region):
        self.region = region
        Client.made.append(self)
        self.calls = []

    def complete_resource_token_auth(self, session_uri, user_identifier):
        self.calls.append((session_uri, user_identifier))


def test_the_session_is_completed_with_the_requests_token():
    Client.made = []
    consent.complete_consent(URI, "header.payload.sig", "eu-west-1", client_class=Client)
    ((client,),) = [Client.made]
    assert client.region == "eu-west-1"
    ((uri, identifier),) = client.calls
    assert uri == URI and identifier.user_token == "header.payload.sig"


@pytest.mark.parametrize(
    "bad",
    [None, "", 5, "urn:ietf:params:oauth:request_uri:", "https://evil.example/x", URI + " x"],
)
def test_a_malformed_session_uri_is_refused_before_any_aws_call(bad):
    Client.made = []
    with pytest.raises(consent.ConsentError, match="malformed"):
        consent.complete_consent(bad, "header.payload.sig", "eu-west-1", client_class=Client)
    assert Client.made == []
