"""Make the built page (web/dist) ready for one environment, before it goes to S3.

    python scripts/web_files.py --dist web/dist --gateway-url <url> --login-domain <url> \
        --client-id <id> --audience <url> --env <env>

Writes config.json from the stack outputs, and into every HTML file a Content-Security-Policy
meta tag whose connect-src names exactly this environment's gateway and login domain. The
distribution's header policy is fixed (no environment's address, which would make a loop in
the stack: infra stack.py, WEB_CSP) and allows any https host for connect-src; the browser
enforces both policies, so the page can reach only these two. A page with an inline script is
refused: the policy would block it anyway.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

CHARSET = re.compile(r'^(\s*)<meta charset="utf-8"\s*/?>\s*$', re.MULTILINE | re.IGNORECASE)
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)


def origin(url: str) -> str:
    """The https origin of ``url``; ValueError for anything else (it goes into a policy)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not re.fullmatch(r"[a-z0-9.-]+", parts.hostname or ""):
        raise ValueError(f"not an https address: {url!r}")
    return f"https://{parts.hostname}" + (f":{parts.port}" if parts.port else "")


def connect_policy(gateway_url: str, login_domain: str) -> str:
    return f"connect-src 'self' {origin(gateway_url)} {origin(login_domain)}"


def with_policy(html: str, policy: str) -> str:
    """``html`` with the policy as a meta tag right after the charset."""
    if INLINE_SCRIPT.search(html):
        raise ValueError("the page has an inline script; the Content Security Policy blocks it")
    match = CHARSET.search(html)
    if not match:
        raise ValueError('the page has no <meta charset="utf-8" /> to put the policy after')
    tag = f'{match.group(1)}<meta http-equiv="Content-Security-Policy" content="{policy}" />'
    return html[: match.end()] + "\n" + tag + html[match.end() :]


def publish(
    dist: Path, *, gateway_url: str, login_domain: str, client_id: str, audience: str, env: str
) -> None:
    pages = sorted(dist.glob("*.html"))
    if not pages:
        raise ValueError(f"no HTML in {dist}: build the page first (npm run build)")
    policy = connect_policy(gateway_url, login_domain)
    for page in pages:
        page.write_text(with_policy(page.read_text(), policy))
    config = {
        "gatewayUrl": gateway_url,
        "loginDomain": login_domain,
        "clientId": client_id,
        "audience": audience,
        "env": env,
    }
    (dist / "config.json").write_text(json.dumps(config, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in ("dist", "gateway-url", "login-domain", "client-id", "audience", "env"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    try:
        publish(
            Path(args.dist),
            gateway_url=args.gateway_url,
            login_domain=args.login_domain,
            client_id=args.client_id,
            audience=args.audience,
            env=args.env,
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"web: config.json and the Content Security Policy written into {args.dist}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
