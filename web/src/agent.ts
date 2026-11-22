// Calls to the agent through the gateway, as scripts/chat.py makes them: the user's token, the
// runtime session id header, a JSON body, and the answer as server-sent events. Every failure
// becomes one error event, so the page has one way to show it.

import type { Config } from "./config";
import type { AgentEvent } from "./events";
import { readEvents } from "./sse";

export const SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id";
// The gateway's one target (infra naming.py, GATEWAY_TARGET); a part of every path.
const TARGET = "agent";

// The address of an operation. Only the origin of the gateway URL counts: the URL the stack
// reports may end in a path of its own (scripts/chat.py, invoke_url).
function endpoint(cfg: Config, operation: string): string {
  return `${new URL(cfg.gatewayUrl).origin}/${TARGET}/${operation}`;
}

function request(token: string, sessionId: string, body: object): RequestInit {
  return {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      [SESSION_HEADER]: sessionId,
    },
    body: JSON.stringify(body),
  };
}

// A new conversation: "session-" and a UUID, 44 characters (the Runtime takes 33 or more, Memory 100 at most).
export function newSessionId(): string {
  return `session-${crypto.randomUUID()}`;
}

export async function* invoke(
  cfg: Config,
  token: string,
  sessionId: string,
  body: object,
  signal?: AbortSignal,
): AsyncGenerator<AgentEvent> {
  let response: Response;
  try {
    response = await fetch(endpoint(cfg, "invocations"), { ...request(token, sessionId, body), signal });
  } catch {
    yield { type: "error", message: "could not reach the agent" };
    return;
  }
  if (response.status === 401 || response.status === 403) {
    yield { type: "error", message: `not authorised (HTTP ${response.status}): sign in again` };
    return;
  }
  if (!response.ok || !response.body) {
    yield { type: "error", message: `the agent answered HTTP ${response.status}` };
    return;
  }
  try {
    yield* readEvents(response.body);
  } catch {
    // The connection broke while the answer streamed (network, a reset by the gateway).
    yield { type: "error", message: "the connection to the agent broke off" };
  }
}

// Ends the runtime session (its container stops); the conversation stays in Memory.
export async function stopSession(cfg: Config, token: string, sessionId: string): Promise<boolean> {
  try {
    const response = await fetch(endpoint(cfg, "stopruntimesession"), request(token, sessionId, {}));
    return response.ok;
  } catch {
    return false;
  }
}
