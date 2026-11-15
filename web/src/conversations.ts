// The user's conversations and their history, from the agent: the agent
// lists them with ListSessions and reads a history with ListEvents, always for the user of the
// token. The open conversation's id stays in sessionStorage, so reloading the tab reopens it
// from Memory instead of keeping half-drawn messages.

import { invoke } from "./agent";
import type { Config } from "./config";
import type { ConversationItem, Turn } from "./events";

async function ask<T>(
  cfg: Config,
  token: string,
  sessionId: string,
  body: object,
  pick: (event: { type: string } & Record<string, unknown>) => T | undefined,
  missing: string,
): Promise<T> {
  for await (const event of invoke(cfg, token, sessionId, body)) {
    if (event.type === "error") throw new Error(event.message);
    const value = pick(event as { type: string } & Record<string, unknown>);
    if (value !== undefined) return value;
  }
  throw new Error(missing);
}

// Newest first. ``sessionId`` is a runtime session for such calls; each new id starts a
// runtime session, so the page reuses one per load.
export async function fetchConversations(cfg: Config, token: string, sessionId: string): Promise<ConversationItem[]> {
  const items = await ask(
    cfg,
    token,
    sessionId,
    { conversations: {} },
    (e) => (e.type === "conversations" ? (e.items as ConversationItem[]) : undefined),
    "no conversation list arrived",
  );
  // By time, not by text: "…:00Z" and "…:00.5Z" sort wrong as strings. An unreadable date last.
  const at = (i: ConversationItem) => Date.parse(i.created_at) || 0;
  return [...items].sort((a, b) => at(b) - at(a));
}

// The history is read in the conversation's own runtime session (the agent takes the id from
// the session header).
export function fetchHistory(cfg: Config, token: string, sessionId: string): Promise<Turn[]> {
  return ask(
    cfg,
    token,
    sessionId,
    { history: {} },
    (e) => (e.type === "history" ? (e.turns as Turn[]) : undefined),
    "no history arrived",
  );
}

const time = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });
const day = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

// "Today, 14:02", "Yesterday" or "12 Nov", in the browser's time zone.
export function label(item: ConversationItem, now: Date): string {
  const created = new Date(item.created_at);
  if (Number.isNaN(created.getTime())) return "";
  if (sameDay(created, now)) return `Today, ${time.format(created)}`;
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (sameDay(created, yesterday)) return "Yesterday";
  return day.format(created);
}

// Titles once seen stay: Memory's metadata filter, which finds a title event, does not find it
// on every read, and a title never changes once given.
export function keepTitles(items: ConversationItem[], known: Map<string, string>): ConversationItem[] {
  return items.map((item) => {
    if (item.title) {
      known.set(item.session_id, item.title);
      return item;
    }
    const title = known.get(item.session_id);
    return title ? { ...item, title } : item;
  });
}

export function titleOf(item: ConversationItem): string {
  return item.title ?? "New conversation";
}

const CURRENT_KEY = "team-assistant.session";

// The open conversation of this tab. Storage can be refused (private modes, settings); the
// page then works without reopening after a reload.
export class CurrentConversation {
  constructor(private readonly storage: Storage) {}

  get(): string | null {
    try {
      return this.storage.getItem(CURRENT_KEY);
    } catch {
      return null;
    }
  }

  set(sessionId: string): void {
    try {
      this.storage.setItem(CURRENT_KEY, sessionId);
    } catch {
      // Without storage a reload starts a new conversation.
    }
  }

  clear(): void {
    try {
      this.storage.removeItem(CURRENT_KEY);
    } catch {
      // Nothing stored.
    }
  }
}
