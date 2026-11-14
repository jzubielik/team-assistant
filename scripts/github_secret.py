"""Store the GitHub OAuth App's client secret for one environment.

    python scripts/github_secret.py --profile <p> --region <r> --secret-id <name>

Reads the secret at a hidden prompt and writes it, as {"client_secret": ...}, into the Secrets
Manager secret that the stack created and that the GitHub credential provider of AgentCore
Identity references (infra/team_assistant_infra/stack.py, _github). Prints no part of it.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

# The JSON key the credential provider reads (stack.py, SecretReferenceProperty json_key).
JSON_KEY = "client_secret"


def read_secret(prompt=getpass.getpass) -> str:
    value = prompt("GitHub OAuth App client secret (hidden): ").strip()
    if not value:
        raise ValueError("empty secret; nothing stored")
    return value


def store(client, secret_id: str, value: str) -> None:
    client.put_secret_value(SecretId=secret_id, SecretString=json.dumps({JSON_KEY: value}))


def client_for(profile: str, region: str):
    import boto3

    return boto3.Session(profile_name=profile, region_name=region).client("secretsmanager")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in ("profile", "region", "secret-id"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    try:
        value = read_secret()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    store(client_for(args.profile, args.region), args.secret_id, value)
    print(f"stored in {args.secret_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
