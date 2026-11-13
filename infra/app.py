#!/usr/bin/env python3
"""Entry point of the CDK app (cdk.json runs it). Context comes from scripts/ops.sh."""

import sys

import aws_cdk as cdk
from team_assistant_infra.stack import build_app

try:
    # The CLI must name the environment; tests call build_app() without it and get "dev".
    build_app(cdk.App(), require_env=True).synth()
except ValueError as error:
    sys.exit(f"error: {error}")
