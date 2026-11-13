"""Create a user in the environment's Cognito user pool, or reset an existing user's password.

    python scripts/user.py --profile <profile> --region <region> --user-pool-id <id> <username>

The password is read twice from a hidden prompt. It is never a command-line argument or an
environment variable, so it does not appear in the process list or in shell history. No
message is sent to the user (the pool has no e-mail or SMS configuration): the operator tells
the user the password.
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys

USERNAME = re.compile(r"[a-z][a-z0-9._-]{0,31}")


def set_user(client, user_pool_id: str, username: str, password: str) -> str:
    """Create the user if missing, then set a permanent password. Returns what was done."""
    try:
        client.admin_create_user(
            UserPoolId=user_pool_id, Username=username, MessageAction="SUPPRESS"
        )
        done = "created"
    except client.exceptions.UsernameExistsException:
        done = "password reset"
    client.admin_set_user_password(
        UserPoolId=user_pool_id, Username=username, Password=password, Permanent=True
    )
    return done


def read_password(prompt=getpass.getpass) -> str:
    first = prompt("Password: ")
    if not first:
        raise ValueError("empty password")
    if prompt("Password again: ") != first:
        raise ValueError("the two passwords differ")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--user-pool-id", required=True)
    parser.add_argument("username")
    args = parser.parse_args(argv)
    if not USERNAME.fullmatch(args.username):
        print(
            "error: invalid USER: lowercase letters, digits and . _ -, starting with a letter, "
            "at most 32 characters",
            file=sys.stderr,
        )
        return 1
    try:
        password = read_password()
    except ValueError as error:
        print(f"error: {error}; nothing done", file=sys.stderr)
        return 1
    import boto3  # imported here so the checks above need no AWS library

    client = boto3.Session(profile_name=args.profile, region_name=args.region).client("cognito-idp")
    try:
        done = set_user(client, args.user_pool_id, args.username, password)
    except client.exceptions.InvalidPasswordException as error:
        # The service's message states the password policy; it does not contain the password.
        print(f"error: {error.response['Error']['Message']}", file=sys.stderr)
        return 1
    print(f"user: {args.username} {done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
