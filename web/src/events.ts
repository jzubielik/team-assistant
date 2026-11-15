// The events the agent streams (src/team_assistant/app.py), as the page reads them. Field
// names are the agent's own. Everything arrives from the network, so toEvent checks every
// field before the page uses it.

export interface TextEvent {
  type: "text";
  data: string;
}
export interface AuthUrlEvent {
  type: "auth_url";
  url: string;
  state: string;
}
export interface DoneEvent {
  type: "done";
  usage: { input: number; output: number };
}
// Not ErrorEvent: that name is taken by the browser's own type.
export interface AgentErrorEvent {
  type: "error";
  message: string;
}
export interface ConsentDoneEvent {
  type: "consent_done";
}
export interface ConversationItem {
  session_id: string;
  created_at: string;
  title: string | null;
}
export interface ConversationsEvent {
  type: "conversations";
  items: ConversationItem[];
}
export interface Turn {
  role: "user" | "assistant";
  text: string;
}
export interface HistoryEvent {
  type: "history";
  turns: Turn[];
}

export type AgentEvent =
  | TextEvent
  | AuthUrlEvent
  | DoneEvent
  | AgentErrorEvent
  | ConsentDoneEvent
  | ConversationsEvent
  | HistoryEvent;

export const UNREADABLE = "unreadable event from the agent";
// The runtime library sends {"error": <exception text>} when agent code raises. The text can
// quote the conversation, so the page shows this instead.
export const AGENT_FAILED = "the agent stopped with an error";

const unreadable: AgentErrorEvent = { type: "error", message: UNREADABLE };

type Fields = Record<string, unknown>;

const isObject = (v: unknown): v is Fields => typeof v === "object" && v !== null && !Array.isArray(v);
const isString = (v: unknown): v is string => typeof v === "string";
const isCount = (v: unknown): v is number => Number.isInteger(v) && (v as number) >= 0;

function isItem(v: unknown): v is ConversationItem {
  return (
    isObject(v) &&
    isString(v.session_id) &&
    isString(v.created_at) &&
    (v.title === null || isString(v.title))
  );
}

function isTurn(v: unknown): v is Turn {
  return isObject(v) && (v.role === "user" || v.role === "assistant") && isString(v.text);
}

// The event in ``value``; an error event when a known type has the wrong fields; null for a
// type this page does not know (a newer agent), which the page skips.
export function toEvent(value: unknown): AgentEvent | null {
  if (!isObject(value)) return unreadable;
  if (!("type" in value) && "error" in value) return { type: "error", message: AGENT_FAILED };
  switch (value.type) {
    case "text":
      return isString(value.data) ? { type: "text", data: value.data } : unreadable;
    case "auth_url":
      return isString(value.url) && isString(value.state)
        ? { type: "auth_url", url: value.url, state: value.state }
        : unreadable;
    case "done": {
      const usage = value.usage;
      return isObject(usage) && isCount(usage.input) && isCount(usage.output)
        ? { type: "done", usage: { input: usage.input, output: usage.output } }
        : unreadable;
    }
    case "error":
      return isString(value.message) ? { type: "error", message: value.message } : unreadable;
    case "consent_done":
      return { type: "consent_done" };
    case "conversations":
      return Array.isArray(value.items) && value.items.every(isItem)
        ? {
            type: "conversations",
            items: value.items.map((i) => ({
              session_id: i.session_id,
              created_at: i.created_at,
              title: i.title,
            })),
          }
        : unreadable;
    case "history":
      return Array.isArray(value.turns) && value.turns.every(isTurn)
        ? { type: "history", turns: value.turns.map((t) => ({ role: t.role, text: t.text })) }
        : unreadable;
    default:
      return null;
  }
}
