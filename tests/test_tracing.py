"""What the agent's spans carry: who asked and in which session, and no message content.

The spans are the ones Strands creates (agent, event loop cycle, model call, tool call). On the
Runtime, ADOT exports them; here they are collected in memory. The redaction setting is the
value the Runtime gets in its environment (infra/team_assistant_infra/stack.py).
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from strands import tool
from strands.telemetry import tracer as strands_tracer

from conftest import FakeModel, collect
from team_assistant import app, prompts
from team_assistant.config import AgentConfig

CONFIG = AgentConfig("fake-model", 100, 30)
REDACT = "gen_ai_unredacted_attributes="
PROMPT = "my salary is 12345"
ANSWER = "the answer is 42"
TOOL_INPUT = "octocat/private-plans"
TOOL_OUTPUT = "Refused: 7 secret issues"


class ToolThenTextModel(FakeModel):
    """First call: asks for the tool ``lookup`` with TOOL_INPUT. Second call: the answer."""

    tool_name, tool_input = "lookup", f'{{"name": "{TOOL_INPUT}"}}'

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if self.calls:
            async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
                yield event
            return
        self.calls.append({"messages": messages, "system_prompt": system_prompt})
        yield {"messageStart": {"role": "assistant"}}
        yield {
            "contentBlockStart": {
                "start": {"toolUse": {"toolUseId": "t-1", "name": self.tool_name}}
            }
        }
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": self.tool_input}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 5, "outputTokens": 5, "totalTokens": 10},
                "metrics": {"latencyMs": 1},
            }
        }


@tool
def lookup(name: str) -> str:
    """Look something up."""
    return TOOL_OUTPUT


def traced(monkeypatch, opt_in: str, context, managers, model, tools=()) -> list:
    """Run one request and return the finished spans."""
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", opt_in)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = strands_tracer.Tracer()  # reads the environment variable set above
    tracer.tracer_provider = provider
    tracer.tracer = provider.get_tracer("strands")
    monkeypatch.setattr(strands_tracer, "_tracer_instance", tracer)
    stream = app.respond(
        {"prompt": PROMPT},
        context,
        config=CONFIG,
        model=model,
        make_session_manager=managers,
        make_tools=lambda on_auth_url, state: list(tools),
    )
    events = collect(stream)
    assert events[-1]["type"] == "done", events
    return list(exporter.get_finished_spans())


def everything_in(spans) -> str:
    """Every place a span can hold text: attributes, events and the status description."""
    parts = []
    for span in spans:
        parts += [span.name, str(dict(span.attributes)), str(span.status.description)]
        parts += [f"{event.name} {dict(event.attributes)}" for event in span.events]
    return "\n".join(parts)


def test_every_span_names_the_user_and_the_session(monkeypatch, context, managers, agent_env):
    spans = traced(monkeypatch, REDACT, context, managers, FakeModel())
    assert spans
    for span in spans:
        assert span.attributes["actor.id"] == "u-1", span.name
        assert span.attributes["session.id"] == context.session_id, span.name


def test_spans_hold_no_prompt_answer_or_tool_text(monkeypatch, context, managers, agent_env):
    model = ToolThenTextModel(chunks=(ANSWER,))
    spans = traced(monkeypatch, REDACT, context, managers, model, tools=[lookup])
    text = everything_in(spans)
    assert any("lookup" in span.name for span in spans), [span.name for span in spans]
    for secret in (PROMPT, ANSWER, TOOL_INPUT, TOOL_OUTPUT, prompts.SYSTEM_PROMPT):
        assert secret not in text, secret


def test_a_failing_github_tool_leaves_no_exception_text_in_spans(
    monkeypatch, context, managers, agent_env
):
    from team_assistant import github

    def failing(**config):
        def decorate(function):
            async def wrapper(*args, **kwargs):
                raise RuntimeError(f"vault said no for {TOOL_OUTPUT}")

            return wrapper

        return decorate

    monkeypatch.setattr(github, "requires_access_token", failing)

    class AsksForIssues(ToolThenTextModel):
        tool_name, tool_input = "list_issues", '{"repository": "octo/cat"}'

    tools = [github.list_issues_tool(on_auth_url=lambda url: None)]
    spans = traced(monkeypatch, REDACT, context, managers, AsksForIssues(), tools=tools)
    (tool_span,) = [span for span in spans if "list_issues" in span.name]
    assert not [event for event in tool_span.events if event.name == "exception"]
    assert not tool_span.status.description
    assert TOOL_OUTPUT not in everything_in(spans)


def test_without_the_setting_the_same_spans_would_hold_the_content(
    monkeypatch, context, managers, agent_env
):
    # Why the Runtime must get the setting: Strands writes content into spans by default.
    model = ToolThenTextModel(chunks=(ANSWER,))
    text = everything_in(traced(monkeypatch, "", context, managers, model, tools=[lookup]))
    for secret in (PROMPT, ANSWER, TOOL_INPUT, TOOL_OUTPUT):
        assert secret in text, secret


def test_trace_attributes_are_only_the_two_ids(managers):
    agent = app.build_agent(CONFIG, managers("m", "u", "s", "r", {}), "u-1", "s-1", FakeModel())
    assert agent.trace_attributes == {"actor.id": "u-1", "session.id": "s-1"}


class QuietFake(app.QuietErrors, FakeModel):
    """The fake model with the error handling the real model gets (app.QuietBedrockModel)."""


def test_a_model_error_leaves_only_its_type_in_spans_and_in_the_log(
    monkeypatch, context, managers, agent_env, caplog
):
    # Bedrock and botocore error texts can quote values of the request, and Strands copies an
    # error's text into the span status, which no redaction setting covers.
    caplog.set_level("INFO", logger="team_assistant")
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", REDACT)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = strands_tracer.Tracer()
    tracer.tracer_provider, tracer.tracer = provider, provider.get_tracer("strands")
    monkeypatch.setattr(strands_tracer, "_tracer_instance", tracer)
    model = QuietFake(error=ValueError(f"Invalid type for parameter text, value: {PROMPT}"))
    stream = app.respond(
        {"prompt": PROMPT},
        context,
        config=CONFIG,
        model=model,
        make_session_manager=managers,
        make_tools=lambda on_auth_url, state: [],
    )
    assert [event["type"] for event in collect(stream)] == ["error"]
    spans = list(exporter.get_finished_spans())
    text = everything_in(spans)
    assert PROMPT not in text
    failed = [span for span in spans if not span.status.is_ok and span.status.description]
    assert failed and {span.status.description for span in failed} == {"ValueError"}
    assert '"error_type": "ValueError"' in caplog.text and PROMPT not in caplog.text


def test_throttling_and_context_overflow_keep_their_type_without_the_text():
    # Strands retries on the first and shortens the conversation on the second, by type.
    from strands.types.exceptions import ContextWindowOverflowException, ModelThrottledException

    for error in (ModelThrottledException(PROMPT), ContextWindowOverflowException(PROMPT)):
        with pytest.raises(type(error)) as caught:
            collect(QuietFake(error=error).stream([]))
        assert PROMPT not in str(caught.value)
        assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_the_real_model_class_has_the_quiet_error_handling():
    assert issubclass(app.QuietBedrockModel, app.QuietErrors)
    assert app.QuietBedrockModel.__mro__.index(
        app.QuietErrors
    ) < app.QuietBedrockModel.__mro__.index(app.BedrockModel)
