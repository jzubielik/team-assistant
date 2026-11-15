// The GitHub consent between two tabs of the page. The chat tab gets an
// auth_url event, keeps its state in memory and opens the URL in a new tab. After the consent
// GitHub returns that tab to callback.html with session_id and state. callback.html holds no
// token: it hands both to the chat tab over a BroadcastChannel (same origin, same browser). The
// chat tab sends the consent to the agent only when the state is the one it waits for, then
// answers the callback tab, which shows the result.

export const CONSENT_CHANNEL = "team-assistant-consent";

export interface ConsentReturn {
  sessionUri: string;
  state: string;
}

export interface ConsentAck {
  ok: boolean;
  message: string;
}

export type Acceptance = "send" | "unknown-state" | "nothing-waiting";

// The return GitHub's redirect carries in the callback page's address.
export function readReturn(url: URL): ConsentReturn | null {
  const sessionUri = url.searchParams.get("session_id") ?? "";
  const state = url.searchParams.get("state") ?? "";
  return sessionUri && state ? { sessionUri, state } : null;
}

// A message on the channel that is a return (sent by callback.html).
export function asReturn(value: unknown): ConsentReturn | null {
  if (typeof value !== "object" || value === null) return null;
  const { sessionUri, state } = value as Record<string, unknown>;
  return typeof sessionUri === "string" && typeof state === "string" && sessionUri && state
    ? { sessionUri, state }
    : null;
}

// Whether the chat tab may send this return: only for the state its question waits for.
export function acceptReturn(waiting: string | null, got: ConsentReturn): Acceptance {
  if (waiting === null) return "nothing-waiting";
  return got.state === waiting ? "send" : "unknown-state";
}

// The chat tab's answer to the callback tab, for one state.
export function readAck(value: unknown, state: string): ConsentAck | null {
  if (typeof value !== "object" || value === null) return null;
  const { ack, ok, message } = value as Record<string, unknown>;
  if (ack !== state || typeof ok !== "boolean") return null;
  return { ok, message: typeof message === "string" ? message : "" };
}
