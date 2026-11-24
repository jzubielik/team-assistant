"""config/agent.toml is read strictly: a wrong or misspelled setting stops the agent."""

from __future__ import annotations

import pytest

from team_assistant import config
from team_assistant.config import (
    NAMESPACES,
    TOKEN_CLAIMS,
    AgentConfig,
    claim_namespaces,
    load_config,
)

VALID = 'model_id = "m"\nmax_output_tokens = 100\n[memory]\nevent_expiry_days = 30\n'


def write(tmp_path, text):
    path = tmp_path / "agent.toml"
    path.write_text(text)
    return path


def test_repository_config_loads():
    config = load_config()
    assert config == AgentConfig(
        model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0",
        max_output_tokens=4000,
        event_expiry_days=30,
        max_preferences=20,
        facts_top_k=20,
        summaries_top_k=5,
    )


def test_max_preferences_is_read_and_absent_means_none(tmp_path):
    assert load_config(write(tmp_path, VALID)).max_preferences == 0
    assert load_config(write(tmp_path, VALID + "max_preferences = 100\n")).max_preferences == 100


@pytest.mark.parametrize("value", ["-1", "101", "5.0", '"5"', "true"])
def test_wrong_max_preferences_is_refused(tmp_path, value):
    with pytest.raises(ValueError, match="memory.max_preferences"):
        load_config(write(tmp_path, VALID + f"max_preferences = {value}\n"))


@pytest.mark.parametrize("key", ["facts_top_k", "summaries_top_k"])
def test_a_top_k_is_read_and_absent_means_five(tmp_path, key):
    assert getattr(load_config(write(tmp_path, VALID)), key) == 5
    assert getattr(load_config(write(tmp_path, VALID + f"{key} = 100\n")), key) == 100


@pytest.mark.parametrize("key", ["facts_top_k", "summaries_top_k"])
@pytest.mark.parametrize("value", ["0", "101", "5.0", '"5"', "true"])
def test_a_wrong_top_k_is_refused(tmp_path, key, value):
    with pytest.raises(ValueError, match=f"memory.{key} must be an integer from 1 to 100"):
        load_config(write(tmp_path, VALID + f"{key} = {value}\n"))


def test_namespaces_start_and_end_with_a_slash():
    # A search matches a prefix: without the trailing slash, /preferences/abc would also
    # match /preferences/abcd... (AgentCore devguide, "Memory organization").
    assert all(t.startswith("/") and t.endswith("/") for t in NAMESPACES.values())


def test_every_strategy_has_a_token_claim():
    assert set(TOKEN_CLAIMS) == set(NAMESPACES)


def test_claim_namespaces_are_one_actors_namespaces():
    assert claim_namespaces("u-1") == {
        "memory_preferences": "/preferences/u-1/",
        "memory_facts": "/facts/u-1/",
        "memory_summaries": "/summaries/u-1/",
    }


@pytest.mark.parametrize(
    "template", ["/facts/{tenant}/", "/facts/{actorId}/{sessionId}/x/", "/facts/"]
)
def test_a_template_no_claim_can_hold_is_refused(monkeypatch, template):
    monkeypatch.setitem(config.NAMESPACES, "facts", template)
    with pytest.raises(ValueError, match="facts"):
        claim_namespaces("u-1")


def test_values_are_read(tmp_path):
    assert load_config(write(tmp_path, VALID)) == AgentConfig("m", 100, 30)


def test_tracing_is_off_unless_switched_on(tmp_path):
    assert load_config(write(tmp_path, VALID)).tracing is False
    assert load_config().tracing is False  # the repository's setting
    assert load_config(write(tmp_path, VALID + "[tracing]\nenabled = true\n")).tracing is True
    assert load_config(write(tmp_path, VALID + "[tracing]\nenabled = false\n")).tracing is False


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[tracing]\nenabled = "yes"\n', "tracing.enabled must be true or false"),
        ("[tracing]\nenabled = 1\n", "tracing.enabled must be true or false"),
        ("[tracing]\n", "tracing.enabled must be true or false"),
        ("[tracing]\nenabled = true\nsampling = 1\n", "unknown key.*tracing.sampling"),
    ],
)
def test_wrong_tracing_settings_are_refused(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        load_config(write(tmp_path, VALID + text))


@pytest.mark.parametrize("days", [7, 365])
def test_expiry_limits_are_accepted(tmp_path, days):
    text = VALID.replace("= 30", f"= {days}")
    assert load_config(write(tmp_path, text)).event_expiry_days == days


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (VALID.replace("= 30", "= 6"), "event_expiry_days"),
        (VALID.replace("= 30", "= 366"), "event_expiry_days"),
        (VALID.replace("= 30", '= "30"'), "event_expiry_days"),
        (VALID.replace("event_expiry_days = 30", ""), "event_expiry_days"),
        (VALID + "event_expiry = 5\n", "memory.event_expiry"),
        ("modelid = 1\n" + VALID, "modelid"),
        (VALID.replace('"m"', '""'), "model_id"),
        (VALID.replace("= 100", "= 0"), "max_output_tokens"),
        (VALID.replace("= 100", "= true"), "max_output_tokens"),
    ],
)
def test_wrong_settings_are_refused(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        load_config(write(tmp_path, text))


def test_the_return_url_follows_the_client_and_never_the_request():
    from team_assistant.config import return_url

    assert return_url(None, None) == "http://localhost:8977/github/callback"
    assert return_url("cli", "https://d.example") == "http://localhost:8977/github/callback"
    assert return_url("web-local", None) == "http://localhost:5173/callback.html"
    assert (
        return_url("web", "https://d111.cloudfront.net/")
        == "https://d111.cloudfront.net/callback.html"
    )


@pytest.mark.parametrize(
    ("client", "web_url"),
    [("web", None), ("web", ""), ("https://evil.example", None), ("CLI", None), (["cli"], None)],
)
def test_an_unknown_client_or_a_missing_web_address_is_refused(client, web_url):
    from team_assistant.config import return_url

    with pytest.raises(ValueError, match="unknown client"):
        return_url(client, web_url)
