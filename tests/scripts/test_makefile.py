"""The Makefile takes operator settings from the command line or local.mk, never from the shell."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TARGETS = (
    "help", "runner", "shell", "arm64", "setup", "check", "synth",
    "bootstrap", "diff", "deploy", "destroy", "status", "user", "login", "chat", "ask",
    "memory", "smoke", "measure", "github", "web", "web-sync", "unbootstrap",
    "hooks", "leaks", "lint", "audit",
)  # fmt: skip
# Variables that must not leak from the developer's shell (or from an outer make) into a test.
DROPPED = {
    "ENV", "PROFILE", "REGION", "ACCOUNT", "GITHUB_CLIENT_ID", "LOGIN_SUFFIX", "CONFIG_ORIGINS",
    "TA_USER", "TA_SESSION", "TA_PROMPT", "TA_MOCK",
    "NATIVE", "MAKEFLAGS", "MAKELEVEL", "MFLAGS",
    "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION",
}  # fmt: skip

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")


def make(*args: str, env: dict[str, str] | None = None, local_mk: str = "/dev/null"):
    per_env = ("ACCOUNT_", "GITHUB_CLIENT_ID_", "LOGIN_SUFFIX_")
    base = {k: v for k, v in os.environ.items() if k not in DROPPED and not k.startswith(per_env)}
    return subprocess.run(
        ["make", "--no-print-directory", "-C", str(ROOT), f"LOCAL_MK={local_mk}", *args],
        capture_output=True,
        text=True,
        check=False,
        env={**base, **(env or {})},
    )


@pytest.fixture
def local_mk(tmp_path):
    path = tmp_path / "local.mk"
    path.write_text("PROFILE = fixture-admin\nREGION = eu-west-1\nACCOUNT_dev = 111111111111\n")
    return str(path)


def test_help_is_the_default_target_and_lists_every_phony_target():
    result = make()
    assert result.returncode == 0, result.stderr
    for target in TARGETS:
        assert f"\n  {target} " in result.stdout, target


def test_help_does_not_call_docker():
    result = make("-n", "help")
    assert result.returncode == 0, result.stderr
    assert "docker" not in result.stdout


def test_profile_from_local_mk_is_accepted(local_mk):
    result = make("help", local_mk=local_mk)
    assert result.returncode == 0, result.stderr
    assert "fixture-admin" in result.stdout
    assert "eu-west-1" in result.stdout


def test_command_line_wins_over_local_mk(local_mk):
    result = make("help", "PROFILE=from-cli", local_mk=local_mk)
    assert "from-cli" in result.stdout
    assert "fixture-admin" not in result.stdout


def test_profile_from_shell_is_not_shown_as_a_setting():
    result = make("help", env={"PROFILE": "from-shell", "REGION": "from-shell"})
    assert result.returncode == 0, result.stderr
    assert "from-shell" not in result.stdout


def test_env_from_shell_is_refused():
    result = make("-n", "synth", "REGION=eu-west-1", env={"ENV": "dev"})
    assert result.returncode != 0
    assert "command line" in result.stderr
    assert "docker" not in result.stdout


def test_profile_from_shell_is_refused():
    result = make("-n", "synth", "REGION=eu-west-1", env={"PROFILE": "from-shell"})
    assert result.returncode != 0
    assert "PROFILE set only in your shell environment" in result.stderr
    assert "docker" not in result.stdout


def test_check_runs_in_the_runner_by_default():
    result = make("-n", "check")
    assert result.returncode == 0, result.stderr
    assert "docker run" in result.stdout
    assert "team-assistant-runner:" in result.stdout
    assert "make --no-print-directory check NATIVE=1" in result.stdout


def test_check_runs_on_the_host_with_native():
    result = make("-n", "check", "NATIVE=1")
    assert result.returncode == 0, result.stderr
    assert "scripts/ops.sh check" in result.stdout
    assert "docker" not in result.stdout


def test_aws_profile_and_region_are_not_passed_to_the_runner():
    shell = {"AWS_PROFILE": "x", "AWS_REGION": "y", "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE"}
    result = make("-n", "check", env=shell)
    assert result.returncode == 0, result.stderr
    assert "AWS_PROFILE" not in result.stdout
    assert "AWS_REGION" not in result.stdout
    # Credentials are passed by name only: the value never appears on a command line.
    assert "-e AWS_ACCESS_KEY_ID" in result.stdout
    assert "AKIAEXAMPLE" not in result.stdout


@pytest.mark.parametrize("script", ["ops.sh", "lib.sh"])
def test_bash_scripts_parse(script):
    result = subprocess.run(
        ["bash", "-n", str(ROOT / "scripts" / script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_ops_rejects_an_unknown_command():
    result = subprocess.run(
        [str(ROOT / "scripts" / "ops.sh"), "no-such-command"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "no-such-command" in result.stderr


def test_synth_needs_region_and_defaults_env_to_dev():
    assert "REGION is required" in make("-n", "synth").stderr
    result = make("-n", "synth", "REGION=eu-west-1", "NATIVE=1")
    assert result.returncode == 0, result.stderr
    assert "scripts/ops.sh synth" in result.stdout


AWS_TARGETS = ("bootstrap", "diff", "deploy", "destroy", "status")


@pytest.mark.parametrize("target", AWS_TARGETS)
def test_aws_targets_need_env_on_the_command_line(target, local_mk):
    assert "ENV is required" in make("-n", target, local_mk=local_mk).stderr
    refused = make("-n", target, local_mk=local_mk, env={"ENV": "dev"})
    assert refused.returncode != 0
    assert "command line" in refused.stderr


@pytest.mark.parametrize("target", AWS_TARGETS)
@pytest.mark.parametrize("missing", ["PROFILE", "REGION"])
def test_aws_targets_need_profile_and_region(target, missing):
    args = [a for a in ("PROFILE=fixture-admin", "REGION=eu-west-1") if not a.startswith(missing)]
    result = make("-n", target, "ENV=dev", *args)
    assert result.returncode != 0
    assert f"{missing} is required" in result.stderr


@pytest.mark.parametrize("target", AWS_TARGETS)
def test_aws_targets_run_ops_with_settings_from_local_mk(target, local_mk):
    result = make("-n", target, "ENV=dev", "NATIVE=1", local_mk=local_mk)
    assert result.returncode == 0, result.stderr
    assert f"scripts/ops.sh {target}" in result.stdout


def test_help_and_banner_never_show_the_account_id(local_mk):
    result = make("help", "ENV=dev", local_mk=local_mk)
    assert "111111111111" not in result.stdout
    assert "<set>" in result.stdout


def test_help_never_shows_the_login_suffix(tmp_path):
    path = tmp_path / "local.mk"
    path.write_text("PROFILE = fixture-admin\nREGION = eu-west-1\nLOGIN_SUFFIX_dev = 0a1b2c3d\n")
    result = make("help", "ENV=dev", local_mk=str(path))
    assert "0a1b2c3d" not in result.stdout
    assert "LOGIN_SUFFIX" in result.stdout


def test_user_takes_the_name_only_from_the_command_line(local_mk):
    # Every shell exports USER (the login name); that one must not create a pool user.
    shell_only = make("-n", "user", "ENV=dev", local_mk=local_mk, env={"USER": "login-name"})
    assert shell_only.returncode != 0
    assert "USER is required" in shell_only.stderr
    given = make("-n", "user", "ENV=dev", "USER=alice", "NATIVE=1", local_mk=local_mk)
    assert given.returncode == 0, given.stderr
    assert "scripts/ops.sh user" in given.stdout


def test_login_runs_on_the_host_and_needs_user(local_mk):
    # The browser and the callback listener must share a machine, so no runner here.
    result = make("-n", "login", "ENV=dev", "USER=alice", local_mk=local_mk)
    assert result.returncode == 0, result.stderr
    assert "scripts/ops.sh login" in result.stdout
    assert "docker" not in result.stdout
    assert "USER is required" in make("-n", "login", "ENV=dev", local_mk=local_mk).stderr


def test_chat_and_ask_run_on_the_host(local_mk):
    chat = make("-n", "chat", "ENV=dev", "USER=alice", local_mk=local_mk)
    assert chat.returncode == 0, chat.stderr
    assert "scripts/ops.sh chat" in chat.stdout and "docker" not in chat.stdout
    ask = make("-n", "ask", "ENV=dev", "USER=alice", "PROMPT=hi", local_mk=local_mk)
    assert ask.returncode == 0, ask.stderr
    assert "scripts/ops.sh ask" in ask.stdout and "docker" not in ask.stdout


def test_ask_takes_the_prompt_only_from_the_command_line(local_mk):
    # zsh and bash set PROMPT for themselves; that one is not a question for the agent.
    result = make("-n", "ask", "ENV=dev", "USER=alice", local_mk=local_mk, env={"PROMPT": "%m>"})
    assert result.returncode != 0
    assert "PROMPT is required" in result.stderr


def test_web_publishes_the_dev_server_on_loopback_only_and_passes_mock():
    result = make("-n", "web", "MOCK=1")
    assert result.returncode == 0, result.stderr
    assert "-p 127.0.0.1:5173:5173" in result.stdout
    assert "-e TA_MOCK" in result.stdout


def test_no_other_target_publishes_a_port():
    result = make("-n", "check")
    assert result.returncode == 0, result.stderr
    assert ":5173" not in result.stdout


def test_web_without_mock_needs_an_environment():
    result = make("-n", "web")
    assert result.returncode != 0
    assert "ENV is required" in result.stderr
