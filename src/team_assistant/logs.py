"""What the agent writes to its log (stdout, which the Runtime stores in its log group).

The agent's own lines (logger ``team_assistant``) are JSON written by app.py and github.py and
hold ids, counts and error types. Libraries log differently: they put the text of an error into
the line (bedrock-agentcore 1.26.0, session_manager.py: ``"Failed to create message in AgentCore
Memory: %s", e``), and such a text can quote values of the failed request, a stored message
among them. So a library's line is written as its wording only, without the values:
bedrock-agentcore and strands pass values as logging arguments, never inside the message.
"""

from __future__ import annotations

import json
import logging

OWN_LOGGER = "team_assistant"


class LibraryLines(logging.Filter):
    """Rewrites records of every logger but the agent's own into a JSON line without values."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == OWN_LOGGER or record.name.startswith(OWN_LOGGER + "."):
            return True
        candidates = [*(record.args if isinstance(record.args, tuple) else ()), record.msg]
        if record.exc_info and record.exc_info[1] is not None:
            candidates.append(record.exc_info[1])
        line = {
            "event": "library_log",
            "logger": record.name,
            "level": record.levelname,
            # The message as written in the library's code, with its %s placeholders.
            "message": record.msg if isinstance(record.msg, str) else "",
            "error_types": [type(c).__name__ for c in candidates if isinstance(c, BaseException)],
        }
        record.msg, record.args = json.dumps(line), None
        # A traceback ends with the error's text.
        record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True


def configure(handler: logging.Handler) -> None:
    """One line per record, the message only; library lines without their values."""
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(LibraryLines())


def configure_stdout() -> None:
    """Called once when the container starts (app.py).

    force: under opentelemetry-instrument the root logger may already have handlers; without
    it basicConfig would do nothing and library lines would be written unfiltered.
    """
    handler = logging.StreamHandler()
    configure(handler)
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
