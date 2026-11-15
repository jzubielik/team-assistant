// The mock agent: a plugin of the Vite development server (make web MOCK=1), so the page can be
// built without a deployed environment. It answers the same paths as the
// gateway with the same server-sent events, from the scenarios in ./scenarios, whose events a
// pytest test checks against the agent's own fields (tests/test_events.py). It keeps what the
// page sends in memory, so the conversation list and history behave like the real ones until
// the server stops. It runs only in the development server ("apply: serve"), never in a build.

import { randomBytes } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { Plugin } from "vite";
import answer from "./scenarios/answer.json" with { type: "json" };
import consent from "./scenarios/consent.json" with { type: "json" };
import consentDone from "./scenarios/consent_done.json" with { type: "json" };
import conversations from "./scenarios/conversations.json" with { type: "json" };
import failure from "./scenarios/error.json" with { type: "json" };
import history from "./scenarios/history.json" with { type: "json" };

type Event = Record<string, unknown>;
type Step = { event: Event } | { wait: "consent" };
interface Scenario {
  steps: Step[];
}
interface Turn {
  role: "user" | "assistant";
  text: string;
}
interface Session {
  created_at: string;
  title: string | null;
  turns: Turn[];
}

export const MOCK_CONFIG = { env: "dev", mock: true, gatewayUrl: "http://localhost:5173" };
// What the consent return carries; the real one is an AgentCore session URI.
export const MOCK_SESSION_URI = "urn:ietf:params:oauth:request_uri:mock-consent-session";
const SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id";
// Pause between streamed pieces, so the page shows the answer growing.
const PIECE_MS = 40;
// The real tool waits this long for a consent (src/team_assistant/github.py).
const CONSENT_WAIT_MS = 600_000;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// Cut a text into pieces of three words, as the model streams it.
function pieces(text: string): string[] {
  const words = text.split(/(?<=\s)/);
  const out: string[] = [];
  for (let i = 0; i < words.length; i += 3) out.push(words.slice(i, i + 3).join(""));
  return out;
}

function readBody(req: IncomingMessage): Promise<unknown> {
  return new Promise((resolve) => {
    let body = "";
    req.on("data", (chunk: Buffer) => (body += chunk.toString("utf8")));
    req.on("end", () => {
      try {
        resolve(JSON.parse(body || "{}"));
      } catch {
        resolve(null);
      }
    });
  });
}

function json(res: ServerResponse, status: number, value: unknown): void {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(value));
}

class MockAgent {
  private sessions = new Map<string, Session>();
  private consented = false;
  private waiting = new Set<() => void>();

  async invoke(req: IncomingMessage, res: ServerResponse): Promise<void> {
    // The gateway refuses a request without a token before the agent sees it.
    if (!/^Bearer \S+$/.test(req.headers.authorization ?? "")) {
      return json(res, 401, { message: "Unauthorized" });
    }
    const sessionId = req.headers[SESSION_HEADER];
    const body = await readBody(req);
    res.writeHead(200, { "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache" });
    let open = true;
    res.on("close", () => (open = false));
    const send = async (event: Event, streamed = true) => {
      if (event.type === "text" && streamed && typeof event.data === "string") {
        for (const piece of pieces(event.data)) {
          if (!open) return;
          res.write(`data: ${JSON.stringify({ type: "text", data: piece })}\n\n`);
          await sleep(PIECE_MS);
        }
      } else if (open) {
        res.write(`data: ${JSON.stringify(event)}\n\n`);
      }
    };

    if (typeof sessionId !== "string" || sessionId.length < 33 || sessionId.length > 100) {
      await send({ type: "error", message: "the request has no session id, or the agent is not configured" });
    } else if (typeof body !== "object" || body === null) {
      await send({ type: "error", message: "the request needs a non-empty 'prompt'" });
    } else if ("consent" in body) {
      await this.consent(body.consent, send);
    } else if ("conversations" in body) {
      await send({ type: "conversations", items: this.items() });
    } else if ("history" in body) {
      await send({ type: "history", turns: this.turns(sessionId) });
    } else if ("prompt" in body && typeof body.prompt === "string" && body.prompt.trim()) {
      await this.prompt(sessionId, body.prompt, send, () => open);
    } else {
      await send({ type: "error", message: "the request needs a non-empty 'prompt'" });
    }
    res.end();
  }

  private async consent(value: unknown, send: (e: Event) => Promise<void>): Promise<void> {
    const uri = typeof value === "object" && value !== null ? (value as Event).session_uri : null;
    if (typeof uri !== "string" || uri === "") {
      return send({ type: "error", message: "the consent session URI is missing or malformed" });
    }
    this.consented = true;
    for (const resume of this.waiting) resume();
    this.waiting.clear();
    for (const step of (consentDone as Scenario).steps) if ("event" in step) await send(step.event);
  }

  private async prompt(
    sessionId: string,
    prompt: string,
    send: (e: Event, streamed?: boolean) => Promise<void>,
    open: () => boolean,
  ): Promise<void> {
    const session = this.sessions.get(sessionId) ?? {
      created_at: new Date().toISOString(),
      title: null,
      turns: [],
    };
    this.sessions.set(sessionId, session);
    session.turns.push({ role: "user", text: prompt });
    const scenario = (/fail/i.test(prompt) ? failure : /issue/i.test(prompt) ? consent : answer) as Scenario;
    let reply = "";
    let skipping = scenario === (consent as Scenario) && this.consented;
    for (const step of scenario.steps) {
      if (!open()) return;
      if ("wait" in step) {
        if (skipping) skipping = false;
        else if (!(await this.waitForConsent())) {
          return send({ type: "error", message: "the agent could not answer; the operator can find the cause in the log" });
        }
        continue;
      }
      if (skipping) continue;
      const event = { ...step.event };
      if (event.type === "auth_url") {
        const state = randomBytes(16).toString("base64url");
        event.state = state;
        event.url = `/callback.html?session_id=${encodeURIComponent(MOCK_SESSION_URI)}&state=${state}`;
      }
      if (event.type === "text" && typeof event.data === "string") reply += event.data;
      await send(event);
    }
    if (reply) {
      session.turns.push({ role: "assistant", text: reply });
      // The real agent asks the model for a title after the first answer; the mock takes the
      // first words of the question.
      session.title ??= prompt.split(/\s+/).slice(0, 6).join(" ");
    }
  }

  private waitForConsent(): Promise<boolean> {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.waiting.delete(resume);
        resolve(false);
      }, CONSENT_WAIT_MS);
      const resume = () => {
        clearTimeout(timer);
        resolve(true);
      };
      this.waiting.add(resume);
    });
  }

  private items(): Event[] {
    const seeded = (conversations as Scenario).steps.flatMap((s) =>
      "event" in s ? (s.event.items as Event[]) : [],
    );
    const live = [...this.sessions].map(([session_id, s]) => ({
      session_id,
      created_at: s.created_at,
      title: s.title,
    }));
    return [...seeded, ...live];
  }

  private turns(sessionId: string): Turn[] {
    const live = this.sessions.get(sessionId);
    if (live) return live.turns;
    const seeded = (history as { sessions: Record<string, { event: { turns: Turn[] } }> }).sessions;
    return seeded[sessionId]?.event.turns ?? [];
  }
}

export function mockAgent(): Plugin {
  const agent = new MockAgent();
  return {
    name: "team-assistant-mock-agent",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const path = (req.url ?? "").split("?")[0];
        if (req.method === "GET" && path === "/config.json") return json(res, 200, MOCK_CONFIG);
        if (req.method === "POST" && path === "/agent/invocations") {
          agent.invoke(req, res).catch(next);
          return;
        }
        if (req.method === "POST" && path === "/agent/stopruntimesession") return json(res, 200, {});
        next();
      });
    },
  };
}
