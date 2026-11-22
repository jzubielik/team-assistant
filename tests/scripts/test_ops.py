"""scripts/ops.sh guards: the account check before a deploy and the typed confirmation of a
destroy. The aws and npx commands are stubs on PATH that record their arguments."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = "111111111111"
OTHER = "222222222222"

# The developer's own settings (or an outer make's) must not reach the script under test.
DROPPED = (
    "ENV", "PROFILE", "REGION", "ACCOUNT", "GITHUB_CLIENT_ID", "LOGIN_SUFFIX", "CONFIG_ORIGINS",
    "AWS_PROFILE", "AWS_REGION",
)  # fmt: skip
CLEAN = {k: v for k, v in os.environ.items() if k not in DROPPED}
CALLER_ARN = "arn:aws:sts::%s:assumed-role/AWSReservedSSO_Admin_0123456789abcdef/jane"
STOCK = ROOT / "infra/node_modules/aws-cdk/lib/api/bootstrap/bootstrap-template.yaml"

AWS_STUB = """#!/usr/bin/env bash
echo "aws $*" >> "$STUB_LOG"
case "$*" in
*"sts get-caller-identity"*) printf '%s\\t%s\\n' "$STUB_ACCOUNT" "$STUB_ARN" ;;
*"ssm get-parameter"*) echo "30" ;;
*"cloudformation list-stacks"*) echo "${STUB_OTHER_STACKS:-}" ;;
*"ecr list-images"*)
  # STUB_IMAGES digests, each listed twice (an image with two tags), tab-separated.
  for i in $(seq 1 "${STUB_IMAGES:-0}"); do printf 'sha256:%064d\tsha256:%064d\t' "$i" "$i"; done
  echo ;;
*"OutputKey=='RuntimeArn'"*)
  echo "arn:aws:bedrock-agentcore:eu-west-1:$STUB_ACCOUNT:runtime/$STUB_RUNTIME" ;;
*"OutputKey=='GatewayUrl'"*) echo "https://gw-1.gateway.example" ;;
*"OutputKey=='LoginDomain'"*) echo "https://ta-dev-0a1b2c3d.auth.example" ;;
*"OutputKey=='UserPoolClientId'"*) echo "client-1" ;;
*"OutputKey=='Audience'"*) echo "https://team-assistant-dev.agent" ;;
*"OutputKey=='WebUrl'"*) echo "https://d111.cloudfront.example" ;;
*"OutputKey=='WebBucket'"*) echo "ta-dev-web-0a1b2c3d" ;;
*"OutputKey=='WebDistributionId'"*) echo "E1EXAMPLE" ;;
# Last: a case matches its first pattern, and the line above is a describe-stacks call too.
*"cloudformation describe-stacks"*)
  if [ -n "${STUB_NO_STACK:-}" ]; then echo "Stack with id x does not exist" >&2; exit 254; fi ;;
esac
"""
NPX_STUB = """#!/usr/bin/env bash
echo "npx $*" >> "$STUB_LOG"
"""
NPM_STUB = """#!/usr/bin/env bash
echo "npm $* (in $PWD)" >> "$STUB_LOG"
"""


@pytest.fixture
def stubs(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # A built page outside the repository: deploy and web-sync publish it (WEB_DIST).
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<head>\n<meta charset="utf-8" />\n</head>\n')
    for name, body in (("aws", AWS_STUB), ("npx", NPX_STUB), ("npm", NPM_STUB)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "calls.log"
    log.touch()
    return bin_dir, log


def ops(
    stubs,
    command,
    *,
    caller=EXPECTED,
    account=EXPECTED,
    stdin="",
    github="Iv1.example",
    suffix="0a1b2c3d",
    extra=None,
):
    bin_dir, log = stubs
    env = {
        **CLEAN,
        **(extra or {}),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ENV": "dev",
        "PROFILE": "fixture-admin",
        "REGION": "eu-west-1",
        "ACCOUNT": account,
        "GITHUB_CLIENT_ID": github,
        "LOGIN_SUFFIX": suffix,
        "STUB_LOG": str(log),
        "WEB_DIST": str(bin_dir.parent / "dist"),
        "STUB_ACCOUNT": caller,
        "STUB_ARN": CALLER_ARN % caller,
        "STUB_RUNTIME": "team_assistant_dev_runtime-AbCdEf1234",
    }
    result = subprocess.run(
        [str(ROOT / "scripts" / "ops.sh"), command],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return result, log.read_text()


@pytest.mark.parametrize("command", ["bootstrap", "diff", "deploy", "destroy"])
def test_account_mismatch_stops_before_cdk(stubs, command):
    result, calls = ops(stubs, command, caller=OTHER, stdin="TeamAssistant-dev\n")
    assert result.returncode != 0
    assert "ACCOUNT" in result.stderr
    assert "npx" not in calls
    assert "cloudformation" not in calls


@pytest.mark.parametrize("command", ["bootstrap", "diff", "deploy", "destroy"])
def test_missing_expected_account_stops_before_cdk(stubs, command):
    result, calls = ops(stubs, command, account="", stdin="TeamAssistant-dev\n")
    assert result.returncode != 0
    assert "ACCOUNT_dev" in result.stderr
    assert "npx" not in calls


def test_error_does_not_print_the_account_ids(stubs):
    result, _ = ops(stubs, "deploy", caller=OTHER)
    assert EXPECTED not in result.stderr + result.stdout
    assert OTHER not in result.stderr + result.stdout


def test_deploy_calls_cdk_for_the_matching_account(stubs):
    result, calls = ops(stubs, "deploy")
    assert result.returncode == 0, result.stderr
    assert "cdk deploy" in calls
    assert "--profile fixture-admin" in calls
    assert "env=dev" in calls
    assert "github_client_id=Iv1.example" in calls


def test_deploy_sets_the_github_return_url_on_the_runtimes_workload_identity(stubs):
    result, calls = ops(stubs, "deploy")
    assert result.returncode == 0, result.stderr
    (update,) = [c for c in calls.splitlines() if "update-workload-identity" in c]
    assert "--name team_assistant_dev_runtime-AbCdEf1234" in update
    assert (
        "--allowed-resource-oauth2-return-urls http://localhost:8977/github/callback"
        " http://localhost:5173/callback.html https://d111.cloudfront.example/callback.html"
    ) in update
    assert calls.index("cdk deploy") < calls.index("update-workload-identity")


def test_deploy_without_the_github_client_id_stops_before_any_change(stubs):
    result, calls = ops(stubs, "deploy", github="")
    assert result.returncode != 0
    assert "GITHUB_CLIENT_ID_dev is not set" in result.stderr
    assert "cdk deploy" not in calls and "cloudformation deploy" not in calls


@pytest.mark.parametrize("command", ["deploy", "diff"])
def test_deploy_and_diff_without_the_login_suffix_stop_before_any_change(stubs, command):
    result, calls = ops(stubs, command, suffix="")
    assert result.returncode != 0
    assert "LOGIN_SUFFIX_dev is not set" in result.stderr
    assert "openssl rand -hex 4" in result.stderr
    assert "npx" not in calls and "cloudformation deploy" not in calls


def test_deploy_passes_the_login_suffix_to_cdk(stubs):
    result, calls = ops(stubs, "deploy")
    assert result.returncode == 0, result.stderr
    (deploy,) = [c for c in calls.splitlines() if "cdk deploy" in c]
    assert "login_suffix=0a1b2c3d" in deploy


def test_destroy_with_wrong_confirmation_does_not_call_cdk(stubs):
    result, calls = ops(stubs, "destroy", stdin="TeamAssistant-prod\n")
    assert result.returncode != 0
    assert "npx" not in calls


def test_destroy_with_the_stack_name_calls_cdk(stubs):
    result, calls = ops(stubs, "destroy", stdin="TeamAssistant-dev\n")
    assert result.returncode == 0, result.stderr
    assert "cdk destroy" in calls


def test_destroy_deletes_both_secrets_at_once_before_the_stack(stubs):
    # The GitHub client secret and the token exchange provider's placeholder.
    result, calls = ops(stubs, "destroy", stdin="TeamAssistant-dev\n")
    assert result.returncode == 0, result.stderr
    deletes = [c for c in calls.splitlines() if "secretsmanager delete-secret" in c]
    ids = sorted(c.split("--secret-id ")[1].split()[0] for c in deletes)
    assert ids == ["team-assistant/dev/exchange", "team-assistant/dev/github"]
    assert all("--force-delete-without-recovery" in c for c in deletes)
    assert calls.rindex("delete-secret") < calls.index("cdk destroy")


def image_deletions(calls: str) -> list[list[str]]:
    """The image digests of each ecr batch-delete-image call."""
    return [
        [word.removeprefix("imageDigest=") for word in line.split() if "imageDigest=" in word]
        for line in calls.splitlines()
        if "ecr batch-delete-image" in line
    ]


def test_destroy_deletes_every_agent_image_by_digest_after_the_stack(stubs):
    result, calls = ops(stubs, "destroy", stdin="TeamAssistant-dev\n", extra={"STUB_IMAGES": "3"})
    assert result.returncode == 0, result.stderr
    (deleted,) = image_deletions(calls)
    # Each image once, although the listing names it once per tag.
    assert deleted == [f"sha256:{i:064d}" for i in (1, 2, 3)]
    (line,) = [c for c in calls.splitlines() if "ecr batch-delete-image" in c]
    assert f"--repository-name cdk-teamasst-container-assets-{EXPECTED}-eu-west-1" in line
    assert calls.index("cdk destroy") < calls.index("ecr batch-delete-image")
    assert "agent images: deleting 3" in result.stdout


def test_destroy_deletes_images_in_calls_of_at_most_100(stubs):
    # BatchDeleteImage takes at most 100 image ids (ECR API reference).
    result, calls = ops(stubs, "destroy", stdin="TeamAssistant-dev\n", extra={"STUB_IMAGES": "205"})
    assert result.returncode == 0, result.stderr
    assert [len(batch) for batch in image_deletions(calls)] == [100, 100, 5]


def test_destroy_keeps_the_images_while_another_environment_is_deployed(stubs):
    extra = {"STUB_IMAGES": "3", "STUB_OTHER_STACKS": "TeamAssistant-qa"}
    result, calls = ops(stubs, "destroy", stdin="TeamAssistant-dev\n", extra=extra)
    assert result.returncode == 0, result.stderr
    assert "ecr list-images" not in calls and image_deletions(calls) == []
    assert "agent images kept" in result.stdout and "TeamAssistant-qa" in result.stdout


def test_destroy_with_an_empty_repository_deletes_nothing(stubs):
    result, calls = ops(stubs, "destroy", stdin="TeamAssistant-dev\n")
    assert result.returncode == 0, result.stderr
    assert image_deletions(calls) == []


@pytest.mark.parametrize("command", ["smoke"])
def test_a_missing_stack_stops_at_the_first_output_with_one_message(stubs, command):
    # A failed $(stack_output ...) inside an argument list does not stop the script, which
    # would then start Python with empty arguments.
    result, _ = ops(stubs, command, extra={"STUB_NO_STACK": "1"})
    assert result.returncode != 0
    assert result.stderr.count("could not read the outputs") == 1, result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("command", ["bootstrap", "diff", "deploy", "destroy", "status"])
def test_aws_commands_need_env_profile_and_region(stubs, command):
    bin_dir, log = stubs
    result = subprocess.run(
        [str(ROOT / "scripts" / "ops.sh"), command],
        capture_output=True,
        text=True,
        check=False,
        env={**CLEAN, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_LOG": str(log)},
    )
    assert result.returncode != 0
    assert log.read_text() == ""


@pytest.mark.skipif(not STOCK.exists(), reason="CDK CLI not installed (runs in the runner)")
def test_bootstrap_uses_the_aws_managed_key_and_the_project_policy(stubs):
    result, calls = ops(stubs, "bootstrap")
    assert result.returncode == 0, result.stderr
    (cdk,) = [line for line in calls.splitlines() if "cdk bootstrap" in line]
    assert "--bootstrap-kms-key-id AWS_MANAGED_KEY" in cdk
    assert "--qualifier teamasst" in cdk
    assert "--toolkit-stack-name TeamAssistantToolkit" in cdk
    assert f"--cloudformation-execution-policies arn:aws:iam::{EXPECTED}:policy/" in cdk
    assert ":policy/TeamAssistantCdkExecution," in cdk
    assert ":policy/TeamAssistantCdkExecutionRoles," in cdk
    assert ":policy/TeamAssistantCdkExecutionGateway," in cdk
    assert ":policy/TeamAssistantCdkExecutionWeb," in cdk
    assert ":policy/TeamAssistantCdkExecutionGuardrail " in cdk
    assert "AdministratorAccess" not in calls
    # The terminal output names the trusted roles without the account id.
    assert EXPECTED not in result.stdout
    assert "AWSReservedSSO_Admin_" in result.stdout


def test_deploy_creates_the_boundary_before_the_stack(stubs):
    _, calls = ops(stubs, "deploy")
    lines = calls.splitlines()
    boundary = next(i for i, c in enumerate(lines) if "team-assistant-dev-boundary" in c)
    stack = next(i for i, c in enumerate(lines) if "cdk deploy" in c)
    assert boundary < stack
    assert "Env=dev" in lines[boundary]
    assert "RuntimeName=team_assistant_dev_runtime" in lines[boundary]
    assert "MemoryName=team_assistant_dev_memory" in lines[boundary]


def test_status_says_what_outlasts_an_environment(stubs):
    result, _ = ops(stubs, "status")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    kept, expiring = out.split("cannot be deleted")
    # Kept on purpose: named, with the way to remove each or the reason it stays.
    for name in (
        "TeamAssistantToolkit",
        "team-assistant-cdk-policies",
        "token vault",
        "GitHub OAuth App",
    ):
        assert name in kept, name
    # Not deletable by anyone: named with the time after which AWS drops it.
    for name, period in (("CloudWatch metrics", "15 months"), ("CloudTrail", "90 days")):
        assert name in expiring and period in expiring, name
    assert EXPECTED not in out


def test_status_reads_only(stubs):
    result, calls = ops(stubs, "status")
    assert result.returncode == 0, result.stderr
    assert "npx" not in calls
    for line in calls.splitlines():
        assert "describe-stacks" in line or "get-caller-identity" in line


def test_web_writes_the_pages_config_from_the_stack_then_starts_the_dev_server(stubs, tmp_path):
    config = tmp_path / "config.json"
    result, calls = ops(stubs, "web", extra={"WEB_CONFIG": str(config)})
    assert result.returncode == 0, result.stderr
    assert json.loads(config.read_text()) == {
        "gatewayUrl": "https://gw-1.gateway.example",
        "loginDomain": "https://ta-dev-0a1b2c3d.auth.example",
        "clientId": "client-1",
        "audience": "https://team-assistant-dev.agent",
        "env": "dev",
    }
    assert "npm run --silent dev" in calls and calls.rstrip().endswith("/web)")


def test_web_with_mock_touches_no_aws(stubs):
    result, calls = ops(stubs, "web", extra={"TA_MOCK": "1"})
    assert result.returncode == 0, result.stderr
    assert "aws " not in calls
    assert "npm run --silent mock" in calls


def test_deploy_publishes_the_page_after_the_stack(stubs):
    result, calls = ops(stubs, "deploy")
    assert result.returncode == 0, result.stderr
    lines = calls.splitlines()
    build = next(i for i, c in enumerate(lines) if "npm run --silent build" in c)
    assert next(i for i, c in enumerate(lines) if "cdk deploy" in c) < build
    syncs = [c for c in lines if "s3 sync" in c]
    assert len(syncs) == 3 and all("s3://ta-dev-web-0a1b2c3d" in c for c in syncs)
    # Hashed assets first, then the HTML that names them, and deletes only at the end.
    assert "/assets s3://ta-dev-web-0a1b2c3d/assets" in syncs[0] and "immutable" in syncs[0]
    assert "--exclude assets/*" in syncs[1] and "no-cache" in syncs[1]
    assert "--delete" in syncs[2] and "--delete" not in syncs[0] + syncs[1]
    (invalidation,) = [c for c in lines if "create-invalidation" in c]
    assert "--distribution-id E1EXAMPLE" in invalidation and "/*" in invalidation
    assert "https://d111.cloudfront.example" in result.stdout


def test_web_sync_publishes_without_a_stack_update(stubs):
    result, calls = ops(stubs, "web-sync")
    assert result.returncode == 0, result.stderr
    assert "cdk deploy" not in calls and "cloudformation deploy" not in calls
    assert "s3 sync" in calls and "create-invalidation" in calls


def test_destroy_empties_the_page_bucket_before_the_stack(stubs):
    result, calls = ops(stubs, "destroy", stdin="TeamAssistant-dev\n")
    assert result.returncode == 0, result.stderr
    lines = calls.splitlines()
    removal = "s3 rm s3://ta-dev-web-0a1b2c3d --recursive"
    empty = next(i for i, c in enumerate(lines) if removal in c)
    assert empty < next(i for i, c in enumerate(lines) if "cdk destroy" in c)


def test_unbootstrap_refuses_while_an_environment_is_deployed(stubs):
    result, calls = ops(
        stubs,
        "unbootstrap",
        stdin="TeamAssistantToolkit\n",
        extra={"STUB_OTHER_STACKS": "TeamAssistant-qa"},
    )
    assert result.returncode != 0
    assert "TeamAssistant-qa" in result.stderr and "make destroy" in result.stderr
    assert "delete-stack" not in calls


def test_unbootstrap_needs_the_toolkit_name_typed(stubs):
    result, calls = ops(stubs, "unbootstrap", stdin="TeamAssistant-dev\n")
    assert result.returncode != 0
    assert "delete-stack" not in calls


def test_unbootstrap_deletes_the_toolkit_then_the_policies(stubs):
    result, calls = ops(stubs, "unbootstrap", stdin="TeamAssistantToolkit\n")
    assert result.returncode == 0, result.stderr
    deletes = [c for c in calls.splitlines() if "delete-stack" in c]
    assert [d.split("--stack-name ")[1].split()[0] for d in deletes] == [
        "TeamAssistantToolkit",
        "team-assistant-cdk-policies",
    ]
    # Only this project's bootstrap: never the account's default one.
    assert "CDKToolkit" not in calls
