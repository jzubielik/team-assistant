import { afterEach, describe, expect, it, vi } from "vitest";
import { SESSION_HEADER, invoke, newSessionId, stopSession } from "../src/agent";
import type { Config } from "../src/config";

const cfg: Config = {
  gatewayUrl: "https://gw.example.com/some/path",
  loginDomain: "https://login.example.com",
  clientId: "client",
  audience: "https://team-assistant-dev.agent",
  env: "dev",
  mock: false,
};

function sse(...events: object[]): Response {
  const body = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
}

async function all<T>(gen: AsyncGenerator<T>): Promise<T[]> {
  const out: T[] = [];
  for await (const item of gen) out.push(item);
  return out;
}

afterEach(() => vi.unstubAllGlobals());

describe("invoke", () => {
  it("posts to <gateway origin>/agent/invocations with the token, session and body", async () => {
    const fetch = vi.fn(async () => sse({ type: "text", data: "Hi" }, { type: "done", usage: { input: 1, output: 2 } }));
    vi.stubGlobal("fetch", fetch);
    const events = await all(invoke(cfg, "tok", "session-x", { prompt: "hello" }));
    expect(events).toEqual([
      { type: "text", data: "Hi" },
      { type: "done", usage: { input: 1, output: 2 } },
    ]);
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://gw.example.com/agent/invocations");
    expect(init.method).toBe("POST");
    const headers = new Headers(init.headers);
    expect(headers.get("authorization")).toBe("Bearer tok");
    expect(headers.get(SESSION_HEADER)).toBe("session-x");
    expect(headers.get("content-type")).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({ prompt: "hello" });
  });

  it("turns a refused token into one error event", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));
    expect(await all(invoke(cfg, "tok", "session-x", { prompt: "x" }))).toEqual([
      { type: "error", message: "not authorised (HTTP 401): sign in again" },
    ]);
  });

  it("turns another HTTP status into one error event", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 502 })));
    expect(await all(invoke(cfg, "tok", "session-x", { prompt: "x" }))).toEqual([
      { type: "error", message: "the agent answered HTTP 502" },
    ]);
  });

  it("turns a network failure into one error event", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    expect(await all(invoke(cfg, "tok", "session-x", { prompt: "x" }))).toEqual([
      { type: "error", message: "could not reach the agent" },
    ]);
  });
});

describe("invoke, broken stream", () => {
  it("turns a connection that breaks mid-answer into one error event after the pieces", async () => {
    let reads = 0;
    const broken = new ReadableStream<Uint8Array>({
      // The first read gets a piece of the answer; the second finds the connection gone.
      pull(controller) {
        if (reads++ === 0) controller.enqueue(new TextEncoder().encode('data: {"type":"text","data":"Hi"}\n\n'));
        else controller.error(new TypeError("network error"));
      },
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(broken, { status: 200 })));
    expect(await all(invoke(cfg, "tok", "session-x", { prompt: "x" }))).toEqual([
      { type: "text", data: "Hi" },
      { type: "error", message: "the connection to the agent broke off" },
    ]);
  });
});

describe("stopSession", () => {
  it("posts {} to <gateway origin>/agent/stopruntimesession and reports success", async () => {
    const fetch = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    expect(await stopSession(cfg, "tok", "session-x")).toBe(true);
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://gw.example.com/agent/stopruntimesession");
    expect(new Headers(init.headers).get(SESSION_HEADER)).toBe("session-x");
  });

  it("reports a failure without throwing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("offline"))));
    expect(await stopSession(cfg, "tok", "session-x")).toBe(false);
  });
});

describe("newSessionId", () => {
  it("is session- and a UUID: 44 characters, between the Runtime's 33 and Memory's 100", () => {
    const id = newSessionId();
    expect(id).toMatch(/^session-[0-9a-f-]{36}$/);
    expect(id).toHaveLength(44);
    expect(newSessionId()).not.toBe(id);
  });
});

describe("clientName", () => {
  it("names the development server and the hosted page", async () => {
    const { clientName } = await import("../src/app");
    expect(clientName("localhost")).toBe("web-local");
    expect(clientName("d111.cloudfront.net")).toBe("web");
  });
});
