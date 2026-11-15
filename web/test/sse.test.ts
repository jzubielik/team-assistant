import { describe, expect, it } from "vitest";
import { AGENT_FAILED, toEvent } from "../src/events";
import { SseDecoder, UNREADABLE, readEvents } from "../src/sse";

const text = (data: string) => `data: ${JSON.stringify({ type: "text", data })}\n\n`;

function streamOf(...chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
}

async function collect(body: ReadableStream<Uint8Array>) {
  const events = [];
  for await (const event of readEvents(body)) events.push(event);
  return events;
}

describe("SseDecoder", () => {
  it("decodes one event per chunk", () => {
    const decoder = new SseDecoder();
    expect(decoder.push(text("Hel"))).toEqual([{ type: "text", data: "Hel" }]);
    expect(decoder.push(text("lo"))).toEqual([{ type: "text", data: "lo" }]);
  });

  it("decodes two events in one chunk", () => {
    const done = 'data: {"type": "done", "usage": {"input": 1840, "output": 212}}\n\n';
    expect(new SseDecoder().push(text("Hi") + done)).toEqual([
      { type: "text", data: "Hi" },
      { type: "done", usage: { input: 1840, output: 212 } },
    ]);
  });

  it("joins one event split across three chunks", () => {
    const decoder = new SseDecoder();
    expect(decoder.push('data: {"type": "te')).toEqual([]);
    expect(decoder.push('xt", "data": "Hello"}')).toEqual([]);
    expect(decoder.push("\n\n")).toEqual([{ type: "text", data: "Hello" }]);
  });

  it("accepts CRLF line ends, also split between chunks", () => {
    const decoder = new SseDecoder();
    expect(decoder.push('data: {"type": "consent_done"}\r')).toEqual([]);
    expect(decoder.push("\n\r\n")).toEqual([{ type: "consent_done" }]);
  });

  it("flushes a last event without a blank line at the end", () => {
    const decoder = new SseDecoder();
    expect(decoder.push('data: {"type": "consent_done"}')).toEqual([]);
    expect(decoder.end()).toEqual([{ type: "consent_done" }]);
  });

  it("turns a line that is not JSON into an error event", () => {
    expect(new SseDecoder().push("data: {not json\n\n")).toEqual([
      { type: "error", message: UNREADABLE },
    ]);
  });

  it("skips comments, other fields and unknown event types", () => {
    const chunk = ': keep-alive\nevent: x\n\ndata: {"type": "later_feature"}\n\n' + text("a");
    expect(new SseDecoder().push(chunk)).toEqual([{ type: "text", data: "a" }]);
  });
});

describe("toEvent", () => {
  it("refuses a known type with missing or wrong fields", () => {
    expect(toEvent({ type: "text" })).toEqual({ type: "error", message: UNREADABLE });
    expect(toEvent({ type: "auth_url", url: "https://github.com/x" })).toEqual({
      type: "error",
      message: UNREADABLE,
    });
    expect(toEvent({ type: "done", usage: { input: "1" } })).toEqual({
      type: "error",
      message: UNREADABLE,
    });
  });

  it("turns the runtime's own error object into a fixed message, never its text", () => {
    expect(toEvent({ error: "prompt was: my secret", error_type: "ValueError" })).toEqual({
      type: "error",
      message: AGENT_FAILED,
    });
  });

  it("keeps the agent's error message", () => {
    expect(toEvent({ type: "error", message: "the request needs a non-empty 'prompt'" })).toEqual({
      type: "error",
      message: "the request needs a non-empty 'prompt'",
    });
  });

  it("reads conversations and history", () => {
    const item = { session_id: "session-1", created_at: "2026-11-15T14:02:00Z", title: null };
    expect(toEvent({ type: "conversations", items: [item] })).toEqual({
      type: "conversations",
      items: [item],
    });
    expect(toEvent({ type: "history", turns: [{ role: "tool", text: "x" }] })).toEqual({
      type: "error",
      message: UNREADABLE,
    });
  });
});

describe("readEvents", () => {
  it("decodes a character split between two byte chunks", async () => {
    const bytes = new TextEncoder().encode(text("Zażółć"));
    const cut = bytes.indexOf(0xc5) + 1; // inside the two bytes of "ż"
    const events = await collect(streamOf(bytes.slice(0, cut), bytes.slice(cut)));
    expect(events).toEqual([{ type: "text", data: "Zażółć" }]);
  });

  it("yields a last event that has no blank line after it", async () => {
    const events = await collect(streamOf(new TextEncoder().encode('data: {"type": "consent_done"}')));
    expect(events).toEqual([{ type: "consent_done" }]);
  });
});
