"""Every physical name is built in naming.py and fits the limit of its service."""

from __future__ import annotations

import dataclasses
import re

import pytest
from team_assistant_infra import naming
from team_assistant_infra.naming import names, validate_env

ACCOUNT = "123456789012"
REGION = "ap-southeast-2"
SUFFIX = "0a1b2c3d"
LONGEST_ENV = "ab1-cb1-cb1-cdz"

# Limits, with the place each was read from (see the naming.py docstring).
LIMITS = {
    "stack": (r"[A-Za-z][A-Za-z0-9-]*", 128),
    "runtime": (r"[a-zA-Z][a-zA-Z0-9_]{0,47}", 48),
    "memory": (r"[a-zA-Z][a-zA-Z0-9_]{0,47}", 48),
    "user_pool": (r"[\w\s+=,.@-]+", 128),
    "domain_prefix": (r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?", 63),
    "boundary_policy": (r"[\w+=,.@-]+", 128),
    "boundary_stack": (r"[A-Za-z][A-Za-z0-9-]*", 128),
    "github_provider": (r"[a-zA-Z0-9\-_]+", 128),
    "github_secret": (r"[A-Za-z0-9/_+=.@-]+", 512),
    "exchange_provider": (r"[a-zA-Z0-9\-_]+", 128),
    "exchange_secret": (r"[A-Za-z0-9/_+=.@-]+", 512),
    "gateway": (r"([0-9a-zA-Z][-]?){1,48}", 48),
    "web_bucket": (r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", 63),
    "guardrail": (r"[0-9a-zA-Z_-]+", 50),
    "audience": (r"[\x21\x23-\x5B\x5D-\x7E]+", 256),
}


def test_longest_env_has_15_characters():
    assert len(LONGEST_ENV) == 15
    assert validate_env(LONGEST_ENV) == LONGEST_ENV


@pytest.mark.parametrize("env", ["dev", "a-b", "p1", "custom-test", "gateway2", LONGEST_ENV])
def test_valid_env(env):
    assert validate_env(env) == env


@pytest.mark.parametrize(
    "env",
    [
        "",
        "Dev",
        "a--b",
        "-a",
        "a-",
        "1a",
        "a_b",
        "a b",
        LONGEST_ENV + "x",
        # Its gateway would match the permissions of environment "dev" (…-dev-gateway-*).
        "dev-gateway-x",
        "gateway",
    ],
)
def test_invalid_env(env):
    with pytest.raises(ValueError, match="ENV"):
        validate_env(env)


def test_examples():
    n = names("dev", ACCOUNT, REGION, SUFFIX)
    assert n.env == "dev"
    assert n.account == ACCOUNT
    assert n.region == REGION
    assert n.stack == "TeamAssistant-dev"
    assert n.runtime == "team_assistant_dev_runtime"
    assert n.memory == "team_assistant_dev_memory"
    assert n.user_pool == "team-assistant-dev-users"
    assert n.domain_prefix == f"ta-dev-{SUFFIX}"
    assert n.boundary_policy == "TeamAssistantCdkPermissionsBoundary-dev"
    assert n.boundary_stack == "team-assistant-dev-boundary"
    assert n.github_provider == "team-assistant-dev-github"
    assert n.github_secret == "team-assistant/dev/github"
    assert n.audience == "https://team-assistant-dev.agent"
    assert n.gateway == "team-assistant-dev-gateway"
    assert n.exchange_provider == "team-assistant-dev-exchange"
    assert n.exchange_secret == "team-assistant/dev/exchange"


def test_secret_name_does_not_end_like_a_secrets_manager_suffix():
    # Secrets Manager appends "-" and six characters to the ARN; a name ending the same way
    # makes partial-ARN lookups ambiguous (CloudFormation reference, AWS::SecretsManager::Secret).
    for env in ("dev", "p1", "custom-test", LONGEST_ENV):
        n = names(env, ACCOUNT, REGION, SUFFIX)
        assert not re.search(r"-[A-Za-z0-9]{6}$", n.github_secret)
        assert not re.search(r"-[A-Za-z0-9]{6}$", n.exchange_secret)


def test_bootstrap_names():
    assert re.fullmatch(r"[a-z0-9]{1,10}", naming.CDK_QUALIFIER)
    assert naming.BOOTSTRAP_PARAMETER == "/cdk-bootstrap/teamasst/version"
    assert naming.ROLE_PATH.startswith("/") and naming.ROLE_PATH.endswith("/")


def test_hyphen_in_env_becomes_underscore_where_the_service_forbids_hyphens():
    n = names("custom-test", ACCOUNT, REGION, SUFFIX)
    assert n.runtime == "team_assistant_custom_test_runtime"
    assert n.memory == "team_assistant_custom_test_memory"
    assert n.stack == "TeamAssistant-custom-test"


@pytest.mark.parametrize("env", ["dev", "p1", "custom-test", LONGEST_ENV])
@pytest.mark.parametrize("field", sorted(LIMITS))
def test_every_name_fits_its_service_limit(env, field):
    pattern, max_length = LIMITS[field]
    value = getattr(names(env, ACCOUNT, REGION, SUFFIX), field)
    assert len(value) <= max_length, value
    assert re.fullmatch(pattern, value), value


@pytest.mark.parametrize("env", ["dev", LONGEST_ENV])
def test_domain_prefix_has_no_reserved_word(env):
    prefix = names(env, ACCOUNT, REGION, SUFFIX).domain_prefix
    # Cognito refuses a prefix domain with one of these (developer guide, "Reserved terms").
    assert not any(word in prefix for word in ("aws", "amazon", "cognito"))


def test_names_of_two_environments_do_not_collide():
    a, b = names("dev", ACCOUNT, REGION, SUFFIX), names("dev-2", ACCOUNT, REGION, SUFFIX)
    for field in LIMITS:
        assert getattr(a, field) != getattr(b, field), field


@pytest.mark.parametrize("account", ["", "12345678901", "1234567890123", "12345678901a"])
def test_account_must_be_12_digits(account):
    with pytest.raises(ValueError, match="account"):
        names("dev", account, REGION, SUFFIX)


@pytest.mark.parametrize("env", ["dev", LONGEST_ENV])
def test_domain_prefix_has_no_account(env):
    # The browser shows the prefix in the login address.
    assert ACCOUNT not in names(env, ACCOUNT, REGION, SUFFIX).domain_prefix


@pytest.mark.parametrize("suffix", ["", "0a1b2c3", "0a1b2c3d4", "0A1B2C3D", "0a1b2c3g", ACCOUNT])
def test_login_suffix_must_be_8_hex_characters(suffix):
    with pytest.raises(ValueError, match="login suffix"):
        names("dev", ACCOUNT, REGION, suffix)


def test_invalid_env_is_refused_by_names():
    with pytest.raises(ValueError, match="ENV"):
        names("Dev", ACCOUNT, REGION, SUFFIX)


def test_names_are_immutable():
    n = names("dev", ACCOUNT, REGION, SUFFIX)
    with pytest.raises(dataclasses.FrozenInstanceError):
        n.stack = "other"
