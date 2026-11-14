"""scripts/github_secret.py: the GitHub OAuth App's client secret goes into Secrets Manager only."""

from __future__ import annotations

import json

import pytest
from test_smoke import load

github_secret = load("github_secret")
SECRET = "not-a-real-github-secret"


class Client:
    def __init__(self):
        self.calls = []

    def put_secret_value(self, **kwargs):
        self.calls.append(kwargs)
        return {"VersionId": "v1"}


def test_the_secret_is_written_as_json_under_the_key_the_provider_reads():
    client = Client()
    github_secret.store(client, "team-assistant/dev/github", SECRET)
    ((call,),) = [client.calls]
    assert call["SecretId"] == "team-assistant/dev/github"
    assert json.loads(call["SecretString"]) == {"client_secret": SECRET}


@pytest.mark.parametrize("typed", ["", "   "])
def test_an_empty_secret_is_refused_before_any_aws_call(typed):
    with pytest.raises(ValueError, match="empty"):
        github_secret.read_secret(prompt=lambda _: typed)


def test_the_secret_is_read_at_a_hidden_prompt_and_stripped():
    asked = []

    def prompt(text):
        asked.append(text)
        return f"  {SECRET}\n"

    assert github_secret.read_secret(prompt=prompt) == SECRET
    assert asked == ["GitHub OAuth App client secret (hidden): "]


def test_main_prints_no_part_of_the_secret(monkeypatch, capsys):
    client = Client()
    monkeypatch.setattr(github_secret, "read_secret", lambda: SECRET)
    monkeypatch.setattr(github_secret, "client_for", lambda profile, region: client)
    code = github_secret.main(
        ["--profile", "p", "--region", "r", "--secret-id", "team-assistant/dev/github"]
    )
    out = capsys.readouterr()
    assert code == 0
    assert SECRET[:8] not in out.out + out.err
    assert "stored" in out.out
