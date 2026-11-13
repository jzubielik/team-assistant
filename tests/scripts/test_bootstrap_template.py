"""scripts/bootstrap_template.py: the CDK deploy roles trust only the caller of make bootstrap."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("bt", ROOT / "scripts" / "bootstrap_template.py")
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)

ACCOUNT = "123456789012"
SSO = f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSReservedSSO_Admin_0123456789abcdef/jane"
SSO_BASE = f"arn:aws:iam::{ACCOUNT}:role/aws-reserved/sso.amazonaws.com/"


def test_identity_center_role_gives_the_permission_set_in_any_region():
    name = "AWSReservedSSO_Admin_" + "?" * 16
    assert bt.derive_patterns(SSO) == [SSO_BASE + name, SSO_BASE + "*/" + name]


def test_plain_role_and_user_give_themselves():
    role = f"arn:aws:sts::{ACCOUNT}:assumed-role/deployer/session"
    assert bt.derive_patterns(role) == [f"arn:aws:iam::{ACCOUNT}:role/deployer"]
    user = f"arn:aws:iam::{ACCOUNT}:user/jane"
    assert bt.derive_patterns(user) == [user]


@pytest.mark.parametrize("arn", [f"arn:aws:iam::{ACCOUNT}:root", "", "not-an-arn"])
def test_root_and_unknown_callers_are_refused_without_echoing_the_arn(arn):
    with pytest.raises(ValueError) as error:
        bt.derive_patterns(arn)
    assert ACCOUNT not in str(error.value)


def _role(statements):
    return {"Properties": {"AssumeRolePolicyDocument": {"Statement": statements}}}


def test_restrict_adds_the_condition_and_drops_cross_account_trust():
    same_account = {"Action": "sts:AssumeRole", "Effect": "Allow", "Principal": {"AWS": bt.ACCOUNT}}
    cross = {"Fn::If": ["HasTrustedAccounts", {"Effect": "Allow"}, bt.NO_VALUE]}
    template = {"Resources": {n: _role([same_account, cross]) for n in bt.RESTRICTED_ROLES}}
    template["Resources"]["CloudFormationExecutionRole"] = _role([{"Effect": "Allow"}])
    out = bt.restrict_template(template, "PATTERN")
    for name in bt.RESTRICTED_ROLES:
        (statement,) = out["Resources"][name]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        assert statement["Condition"] == {"ArnLike": {"aws:PrincipalArn": "PATTERN"}}
    untouched = out["Resources"]["CloudFormationExecutionRole"]
    assert untouched == template["Resources"]["CloudFormationExecutionRole"]


def test_restrict_refuses_a_trust_statement_it_does_not_know():
    odd = {"Effect": "Allow", "Principal": {"Service": "example.amazonaws.com"}}
    template = {"Resources": {n: _role([odd]) for n in bt.RESTRICTED_ROLES}}
    with pytest.raises(ValueError, match="unexpected trust statement"):
        bt.restrict_template(template, "PATTERN")


@pytest.mark.skipif(not bt.STOCK.exists(), reason="CDK CLI not installed (runs in the runner)")
def test_the_installed_cli_template_can_be_restricted(tmp_path):
    out = tmp_path / "template.json"
    assert bt.main(["--caller-arn", SSO, str(out)]) == 0
    stock = yaml.safe_load(bt.STOCK.read_text())
    written = yaml.safe_load(out.read_text())
    assert set(written["Resources"]) == set(stock["Resources"])
    for name in bt.RESTRICTED_ROLES:
        doc = written["Resources"][name]["Properties"]["AssumeRolePolicyDocument"]
        for statement in doc["Statement"]:
            assert "aws:PrincipalArn" in str(statement["Condition"])
