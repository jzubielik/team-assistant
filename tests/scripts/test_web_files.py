"""The files make deploy adds to the built page before it goes to S3."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ta_web_files", ROOT / "scripts" / "web_files.py")
web_files = importlib.util.module_from_spec(spec)
sys.modules["ta_web_files"] = web_files
spec.loader.exec_module(web_files)

GATEWAY = "https://gw-1.gateway.bedrock-agentcore.eu-west-1.amazonaws.com"
LOGIN = "https://ta-dev-0a1b2c3d.auth.eu-west-1.amazoncognito.com"
PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>team-assistant</title>
    <script type="module" crossorigin src="/assets/main-abc.js"></script>
  </head>
  <body><div id="app"></div></body>
</html>
"""


def test_the_meta_policy_lets_the_page_reach_only_the_gateway_and_the_login_domain():
    policy = web_files.connect_policy(GATEWAY + "/some/path", LOGIN)
    assert policy == f"connect-src 'self' {GATEWAY} {LOGIN}"


@pytest.mark.parametrize("bad", ["http://gw.example", "", "https://gw_example;x"])
def test_an_address_that_is_not_https_with_a_plain_host_is_refused(bad):
    with pytest.raises(ValueError):
        web_files.connect_policy(bad, LOGIN)


def test_only_the_origin_reaches_the_policy_never_a_path_or_quote():
    policy = web_files.connect_policy("https://gw.example/x'; script-src *", LOGIN)
    assert policy == f"connect-src 'self' https://gw.example {LOGIN}"


def test_the_meta_tag_goes_right_after_the_charset():
    html = web_files.with_policy(PAGE, "connect-src 'self' https://a.example")
    lines = html.splitlines()
    charset = next(i for i, line in enumerate(lines) if "charset" in line)
    expected = "connect-src 'self' https://a.example"
    assert lines[charset + 1].strip() == (
        f'<meta http-equiv="Content-Security-Policy" content="{expected}" />'
    )


def test_a_page_with_an_inline_script_is_refused():
    inline = PAGE.replace("</head>", "<script>alert(1)</script></head>")
    with pytest.raises(ValueError, match="inline script"):
        web_files.with_policy(inline, "connect-src 'self'")


def test_publish_writes_config_and_the_policy_into_every_html_file(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    (tmp_path / "callback.html").write_text(PAGE)
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "main-abc.js").write_text("console.log(1)")
    web_files.publish(
        tmp_path,
        gateway_url=GATEWAY,
        login_domain=LOGIN,
        client_id="client-1",
        audience="https://team-assistant-dev.agent",
        env="dev",
    )
    assert json.loads((tmp_path / "config.json").read_text()) == {
        "gatewayUrl": GATEWAY,
        "loginDomain": LOGIN,
        "clientId": "client-1",
        "audience": "https://team-assistant-dev.agent",
        "env": "dev",
    }
    for name in ("index.html", "callback.html"):
        assert f"connect-src 'self' {GATEWAY} {LOGIN}" in (tmp_path / name).read_text()
    assert (tmp_path / "assets" / "main-abc.js").read_text() == "console.log(1)"


def test_publish_refuses_a_build_without_html(tmp_path):
    with pytest.raises(ValueError, match="no HTML"):
        web_files.publish(
            tmp_path,
            gateway_url=GATEWAY,
            login_domain=LOGIN,
            client_id="c",
            audience="a",
            env="dev",
        )
