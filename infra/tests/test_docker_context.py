"""The agent image's build context holds only what the image copies."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RULES = [
    line.strip()
    for line in (ROOT / ".dockerignore").read_text().splitlines()
    if line.strip() and not line.startswith("#")
]
CONTEXT = {"account": "123456789012", "region": "eu-west-1", "login_suffix": "0a1b2c3d"}
DOCKERFILE = (ROOT / "docker" / "agent.Dockerfile").read_text()


def test_everything_is_excluded_first():
    assert RULES[0] == "*"


def test_only_the_package_its_settings_the_lock_files_and_the_dockerfile_are_let_in():
    allowed = {rule[1:].rstrip("/") for rule in RULES if rule.startswith("!")}
    assert allowed == {
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "src",
        "config",
        "docker",
        "docker/agent.Dockerfile",
    }
    # The docker/ directory is let in only for the one Dockerfile.
    assert RULES.index("docker/*") > RULES.index("!docker/")
    assert RULES.index("!docker/agent.Dockerfile") > RULES.index("docker/*")
    for name in ("infra", "tests", ".git", "local.mk", "docs", "scripts", ".github"):
        assert name not in allowed


def test_the_dockerfile_copies_only_what_the_context_holds():
    allowed = {rule[1:].rstrip("/") for rule in RULES if rule.startswith("!")}
    copied = set()
    for line in DOCKERFILE.splitlines():
        if line.startswith("COPY ") and "--from=" not in line:
            copied.update(source.lstrip("./") for source in line.split()[1:-1])
    assert copied and copied <= allowed


def test_the_image_is_pinned_and_does_not_run_as_root():
    for line in DOCKERFILE.splitlines():
        if line.startswith("FROM "):
            assert re.search(r"@sha256:[0-9a-f]{64}", line), line
    assert re.search(r"^USER (?!root|0\b)\S+", DOCKERFILE, re.M)
    assert '"-m", "team_assistant.app"' in DOCKERFILE
    # The agent starts under ADOT, which sets up the export of its spans.
    (cmd,) = [line for line in DOCKERFILE.splitlines() if line.startswith("CMD ")]
    assert cmd.startswith('CMD ["/app/.venv/bin/opentelemetry-instrument", "/app/.venv/bin/python"')
    assert "--frozen --no-dev" in DOCKERFILE


def test_the_staged_build_context_holds_the_dockerfile_and_nothing_private():
    """CDK builds from a copy of the context filtered by .dockerignore; check that copy."""
    import json

    import aws_cdk as cdk
    from team_assistant_infra.stack import build_app

    app = build_app(cdk.App(context=CONTEXT))
    assembly = app.synth()
    manifest = json.loads((Path(assembly.directory) / "TeamAssistant-dev.assets.json").read_text())
    (image,) = manifest["dockerImages"].values()
    staged = Path(assembly.directory) / image["source"]["directory"]
    files = {str(p.relative_to(staged)) for p in staged.rglob("*") if p.is_file()}
    assert image["source"]["dockerFile"] in files
    assert {"pyproject.toml", "uv.lock", "config/agent.toml", "src/team_assistant/app.py"} <= files
    top = {name.split("/")[0] for name in files}
    # CDK also copies the .dockerignore file itself.
    assert top - {".dockerignore"} == {
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "src",
        "config",
        "docker",
    }
    assert [f for f in files if f.startswith("docker/")] == ["docker/agent.Dockerfile"]
    assert not [f for f in files if f.endswith(".pyc")]
