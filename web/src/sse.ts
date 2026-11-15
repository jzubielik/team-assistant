// Server-sent events from the agent: the runtime library writes each event as one line
// "data: <json>" followed by a blank line (bedrock_agentcore/runtime/app.py, _convert_to_sse).
// The network may cut that anywhere, so the decoder keeps what it has not finished. It follows
// the SSE rules the agent needs: lines end in LF, CRLF or CR; a blank line ends an event;
// several data lines are joined with LF; comments (":") and other fields are skipped.

import { type AgentEvent, UNREADABLE, toEvent } from "./events";

export { UNREADABLE };

export class SseDecoder {
  private buffer = "";
  private data: string[] = [];

  // The events completed by ``chunk``.
  push(chunk: string): AgentEvent[] {
    this.buffer += chunk;
    const events: AgentEvent[] = [];
    for (;;) {
      const end = this.buffer.search(/\r\n|\n|\r/);
      if (end < 0) break;
      // A CR at the very end may be the first half of a CRLF split between chunks.
      if (this.buffer[end] === "\r" && end === this.buffer.length - 1) break;
      const length = this.buffer.startsWith("\r\n", end) ? 2 : 1;
      const line = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + length);
      this.line(line, events);
    }
    return events;
  }

  // The last event, when the stream ends without a blank line after it.
  end(): AgentEvent[] {
    const events: AgentEvent[] = [];
    if (this.buffer) this.line(this.buffer.replace(/\r$/, ""), events);
    this.buffer = "";
    this.dispatch(events);
    return events;
  }

  private line(line: string, events: AgentEvent[]): void {
    if (line === "") this.dispatch(events);
    else if (line.startsWith("data:")) this.data.push(line.slice(5).replace(/^ /, ""));
  }

  private dispatch(events: AgentEvent[]): void {
    if (this.data.length === 0) return;
    const text = this.data.join("\n");
    this.data = [];
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch {
      events.push({ type: "error", message: UNREADABLE });
      return;
    }
    const event = toEvent(value);
    if (event) events.push(event);
  }
}

// The events of a response body, as they arrive. TextDecoder in stream mode keeps a character
// whose bytes are split between two chunks.
export async function* readEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<AgentEvent> {
  const bytes = new TextDecoder();
  const decoder = new SseDecoder();
  const reader = body.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      yield* decoder.push(bytes.decode(value, { stream: true }));
    }
    yield* decoder.push(bytes.decode());
    yield* decoder.end();
  } finally {
    reader.releaseLock();
  }
}
