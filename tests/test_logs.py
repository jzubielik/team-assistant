"""logs.py: what reaches the Runtime's log group from libraries."""

from __future__ import annotations

import io
import json
import logging

import pytest

from team_assistant import logs

SECRET = "my salary is 12345"


@pytest.fixture
def output():
    """A handler set up the way the container's is, writing to a buffer."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logs.configure(handler)
    root = logging.getLogger()
    before = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield stream
    root.removeHandler(handler)
    root.setLevel(before)


def test_the_agents_own_line_is_written_unchanged(output):
    line = json.dumps({"event": "invocation", "outcome": "ok", "actor": "u-1"})
    logging.getLogger("team_assistant").info(line)
    assert output.getvalue() == line + "\n"


def test_a_library_line_keeps_its_wording_and_loses_its_values(output):
    library = logging.getLogger("bedrock_agentcore.memory.client")
    library.warning("Memory retrieval failed (%s): %s", "ValidationException", f"Value '{SECRET}'")
    record = json.loads(output.getvalue())
    assert record == {
        "event": "library_log",
        "logger": "bedrock_agentcore.memory.client",
        "level": "WARNING",
        # The wording as written in the library: an alarm can still match it.
        "message": "Memory retrieval failed (%s): %s",
        "error_types": [],
    }
    assert SECRET not in output.getvalue()


def test_an_exception_argument_leaves_its_type(output):
    error = ValueError(f"Invalid type for parameter text, value: {SECRET}")
    logging.getLogger("bedrock_agentcore.memory").error("Failed to create message: %s", error)
    record = json.loads(output.getvalue())
    assert record["message"] == "Failed to create message: %s"
    assert record["error_types"] == ["ValueError"]
    assert SECRET not in output.getvalue()


def test_a_traceback_is_not_written(output):
    try:
        raise RuntimeError(SECRET)
    except RuntimeError:
        logging.getLogger("strands.event_loop").exception("cycle failed")
    record = json.loads(output.getvalue())
    assert record["message"] == "cycle failed" and record["error_types"] == ["RuntimeError"]
    assert SECRET not in output.getvalue() and "Traceback" not in output.getvalue()


def test_a_message_that_is_not_a_string_is_not_written(output):
    logging.getLogger("some.library").warning(ValueError(SECRET))
    record = json.loads(output.getvalue())
    assert record["message"] == "" and record["error_types"] == ["ValueError"]
    assert SECRET not in output.getvalue()


def test_container_setup_replaces_handlers_installed_before_it(capsys):
    # Under opentelemetry-instrument the root logger can already have a handler when the agent
    # starts; a plain basicConfig would then do nothing and library lines would pass unfiltered.
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    early = logging.StreamHandler(io.StringIO())
    root.handlers[:] = [early]
    try:
        logs.configure_stdout()
        assert early not in root.handlers and len(root.handlers) == 1
        logging.getLogger("some.library").error("failed: %s", ValueError(SECRET))
    finally:
        root.handlers[:] = saved
        root.setLevel(level)
    assert SECRET not in capsys.readouterr().err
    assert early.stream.getvalue() == ""
