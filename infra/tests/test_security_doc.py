"""docs/security.md lists exactly the actions of the agent's role and of the gateway's role."""

from __future__ import annotations

import re
from pathlib import Path

import aws_cdk as cdk
import pytest
from team_assistant_infra.stack import build_app

DOC = (Path(__file__).resolve().parents[2] / "docs" / "security.md").read_text()


def role_statements(gateway: bool | None = None, effect: str = "Allow") -> list[dict]:
    """Policy statements of the stack's roles: the gateway's, the Runtime's, or (None) both."""
    context = {"account": "123456789012", "region": "eu-west-1", "login_suffix": "0a1b2c3d"}
    app = build_app(cdk.App(context=context))
    template = app.synth().get_stack_by_name("TeamAssistant-dev").template
    return [
        statement
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::IAM::Policy"
        and gateway in (None, "GatewayRole" in str(resource["Properties"]["Roles"]))
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]
        if statement["Effect"] == effect
    ]


def as_list(value):
    return value if isinstance(value, list) else [value]


@pytest.mark.parametrize(("heading", "gateway"), [("Runtime role", False), ("Gateway role", True)])
def test_every_action_of_the_role_is_in_its_table_and_the_other_way_round(heading, gateway):
    section = DOC.split(f"## {heading}")[1].split("\n## ")[0]
    allowed, denied = section.split("Explicitly denied")
    for part, effect in ((allowed, "Allow"), (denied, "Deny")):
        documented = set(re.findall(r"^\| `([a-z-]+:[A-Za-z0-9]+)` \|", part, re.M))
        actions = {a for s in role_statements(gateway, effect) for a in as_list(s["Action"])}
        assert actions and actions == documented, effect


def test_no_wildcard_action_and_every_statement_on_all_resources_is_a_known_one():
    known = {
        "ecr:GetAuthorizationToken",
        "cloudwatch:PutMetricData",
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets",
    }
    for statement in role_statements():
        for action in as_list(statement["Action"]):
            assert "*" not in action
        if statement["Resource"] == "*":
            assert set(as_list(statement["Action"])) <= known, statement
