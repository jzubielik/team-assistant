"""Print the names scripts/ops.sh needs, as shell assignments, from infra's naming.py.

    eval "$(python scripts/names.py --env dev)"

naming.py is the only place that builds names; this script only prints them. Every value
consists of letters, digits and ``/_-:``, so the output is safe to eval.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))

from team_assistant_infra import naming  # noqa: E402

from team_assistant.config import GITHUB_RETURN_URL, WEB_LOCAL_RETURN_URL  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", required=True)
    args = parser.parse_args(argv)
    try:
        # The names printed here do not contain the account, the region or the login suffix.
        n = naming.names(args.env, "0" * 12, "none", "0" * 8)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    values = {
        "STACK": n.stack,
        "BOUNDARY_STACK": n.boundary_stack,
        "RUNTIME": n.runtime,
        "MEMORY": n.memory,
        "GITHUB_PROVIDER": n.github_provider,
        "GITHUB_SECRET": n.github_secret,
        "EXCHANGE_SECRET": n.exchange_secret,
        "GITHUB_RETURN_URL": GITHUB_RETURN_URL,
        "WEB_LOCAL_RETURN_URL": WEB_LOCAL_RETURN_URL,
        "GATEWAY_TARGET": naming.GATEWAY_TARGET,
        "QUALIFIER": naming.CDK_QUALIFIER,
        "TOOLKIT_STACK": naming.TOOLKIT_STACK,
        "POLICIES_STACK": naming.POLICIES_STACK,
        "EXECUTION_POLICIES": ",".join(naming.EXECUTION_POLICIES),
        "BOOTSTRAP_PARAMETER": naming.BOOTSTRAP_PARAMETER,
    }
    for key, value in values.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
