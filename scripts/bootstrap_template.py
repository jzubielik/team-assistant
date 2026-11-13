"""Write the CDK bootstrap template with the deploy-side roles trusting only the deployer.

The stock template (the installed CDK CLI's bootstrap-template.yaml) lets every principal of
the account assume cdk-<qualifier>-deploy-role, -file-publishing-role, -image-publishing-role
and -lookup-role (Principal: the account; IAM policies of the caller decide). This script
copies it and narrows those four trust policies to principals whose ARN matches the caller of
`make bootstrap` (aws:PrincipalArn, ArnLike), and drops the cross-account trust statements.
`make bootstrap` passes the result to `cdk bootstrap --template` (docs/adr/0001).

    python scripts/bootstrap_template.py --caller-arn <sts arn> OUT

The patterns are derived from the caller: an IAM Identity Center role
(AWSReservedSSO_<PermissionSet>_<16 hex>) gives every role of that permission set, under
aws-reserved/sso.amazonaws.com/ with and without a region segment; another assumed role gives
that role (path /); an IAM user gives that user. The patterns are printed on stdout.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
STOCK = ROOT / "infra/node_modules/aws-cdk/lib/api/bootstrap/bootstrap-template.yaml"
RESTRICTED_ROLES = (
    "DeploymentActionRole",
    "FilePublishingRole",
    "ImagePublishingRole",
    "LookupRole",
)
ACCOUNT = {"Ref": "AWS::AccountId"}
NO_VALUE = {"Ref": "AWS::NoValue"}

_STS = re.compile(r"^arn:(aws[\w-]*):sts::(\d{12}):assumed-role/([\w+=,.@-]+)/[\w+=,.@-]+$")
_IAM_USER = re.compile(r"^arn:(aws[\w-]*):iam::(\d{12}):user/([\w+=,.@/-]+)$")
_SSO_ROLE = re.compile(r"^AWSReservedSSO_(.+)_[0-9a-f]{16}$")
SSO_PATH = "aws-reserved/sso.amazonaws.com/"


def derive_patterns(caller_arn: str) -> list[str]:
    """Trust patterns for the caller of make bootstrap (see the module docstring)."""
    if m := _STS.match(caller_arn):
        partition, account, role = m.groups()
        if sso := _SSO_ROLE.match(role):
            name = f"AWSReservedSSO_{sso[1]}_{'?' * 16}"
            base = f"arn:{partition}:iam::{account}:role/{SSO_PATH}"
            return [f"{base}{name}", f"{base}*/{name}"]
        return [f"arn:{partition}:iam::{account}:role/{role}"]
    if _IAM_USER.match(caller_arn):
        return [caller_arn]
    raise ValueError(
        "cannot derive the deployer from the caller identity (root or federated user): "
        "bootstrap as an IAM Identity Center role, an IAM role or an IAM user"
    )


def _restrict(condition, pattern):
    """Add ArnLike aws:PrincipalArn to a statement condition (plain or Fn::If)."""
    arn_like = {"ArnLike": {"aws:PrincipalArn": pattern}}
    if condition is None or condition == NO_VALUE:
        return arn_like
    if isinstance(condition, dict) and set(condition) == {"Fn::If"}:
        name, then, other = condition["Fn::If"]
        return {"Fn::If": [name, _restrict(then, pattern), _restrict(other, pattern)]}
    if isinstance(condition, dict) and "ArnLike" not in condition:
        return {**condition, **arn_like}
    raise ValueError(f"unexpected trust condition in the bootstrap template: {condition!r}")


def restrict_template(template: dict, pattern) -> dict:
    out = copy.deepcopy(template)
    for name in RESTRICTED_ROLES:
        doc = out["Resources"][name]["Properties"]["AssumeRolePolicyDocument"]
        kept = []
        for st in doc["Statement"]:
            if "Fn::If" in st:  # cross-account trust (--trust / --trust-for-lookup): dropped
                continue
            if st.get("Principal") != {"AWS": ACCOUNT}:
                raise ValueError(f"{name}: unexpected trust statement {st!r}")
            kept.append({**st, "Condition": _restrict(st.get("Condition"), pattern)})
        if not kept:
            raise ValueError(f"{name}: no same-account trust statement found")
        doc["Statement"] = kept
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--caller-arn", required=True)
    parser.add_argument("--stock", type=Path, default=STOCK)
    parser.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    try:
        patterns = derive_patterns(args.caller_arn)
        template = yaml.safe_load(args.stock.read_text())
        trusted = patterns[0] if len(patterns) == 1 else patterns
        args.out.write_text(json.dumps(restrict_template(template, trusted), indent=1))
    except (ValueError, OSError, KeyError) as e:
        print(f"bootstrap_template: {e}", file=sys.stderr)
        return 2
    print(",".join(patterns))
    return 0


if __name__ == "__main__":
    sys.exit(main())
