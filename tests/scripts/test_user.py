"""scripts/user.py: operator-created users with a password from a hidden prompt."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("user", ROOT / "scripts" / "user.py")
user = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(user)


class Exists(Exception):
    pass


class FakeClient:
    class exceptions:  # noqa: N801 - mirrors the boto3 client attribute
        UsernameExistsException = Exists

    def __init__(self, exists=False):
        self.exists, self.calls = exists, []

    def admin_create_user(self, **kwargs):
        self.calls.append(("create", kwargs))
        if self.exists:
            raise Exists

    def admin_set_user_password(self, **kwargs):
        self.calls.append(("password", kwargs))


def test_new_user_is_created_without_a_message_and_gets_a_permanent_password():
    client = FakeClient()
    assert user.set_user(client, "pool-1", "alice", "pw") == "created"
    (_, create), (_, password) = client.calls
    assert create == {"UserPoolId": "pool-1", "Username": "alice", "MessageAction": "SUPPRESS"}
    assert password == {
        "UserPoolId": "pool-1",
        "Username": "alice",
        "Password": "pw",
        "Permanent": True,
    }


def test_existing_user_gets_a_new_password():
    client = FakeClient(exists=True)
    assert user.set_user(client, "pool-1", "alice", "pw") == "password reset"
    assert [name for name, _ in client.calls] == ["create", "password"]


def test_password_is_asked_twice_and_must_match():
    answers = iter(["one", "one"])
    assert user.read_password(lambda _: next(answers)) == "one"
    answers = iter(["one", "two"])
    with pytest.raises(ValueError, match="differ"):
        user.read_password(lambda _: next(answers))


def test_an_empty_answer_is_no_password_and_a_random_one_has_every_group():
    assert user.read_password(lambda _: "") is None
    password = user.random_password()
    assert len(password) == 24 and password != user.random_password()
    for group in (
        "abcdefghijklmnopqrstuvwxyz",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "0123456789",
        "-_.+=",
    ):
        assert set(password) & set(group)


@pytest.mark.parametrize("name", ["Alice", "", "1alice", "a b", "a'b", "x" * 33])
def test_invalid_username_stops_before_the_password_prompt(name, monkeypatch, capsys):
    monkeypatch.setattr(user, "read_password", lambda: pytest.fail("prompted"))
    args = ["--profile", "p", "--region", "r", "--user-pool-id", "pool-1", name]
    assert user.main(args) == 1
    assert "invalid USER" in capsys.readouterr().err


def test_the_password_is_not_a_command_line_option():
    source = (ROOT / "scripts" / "user.py").read_text()
    assert "--password" not in source
