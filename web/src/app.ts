// The chat: one conversation (a runtime session id) at a time. A message goes to the agent and
// its events are drawn as they arrive. "New conversation" stops the current runtime session and
// starts another id; "End session" stops it and keeps the id, so the next message continues the
// conversation from Memory. The sidebar lists the user's conversations; opening
// one draws its history from Memory and continues it. The open conversation's id survives a
// reload of the tab.

import { invoke, newSessionId, stopSession } from "./agent";
import { SignInRequired } from "./auth";
import type { Config } from "./config";
import { CONSENT_CHANNEL, type ConsentReturn, acceptReturn, asReturn } from "./consent";
import { CurrentConversation, fetchConversations, fetchHistory, keepTitles, titleOf } from "./conversations";
import type { AgentEvent, ConversationItem } from "./events";
import { AnswerView, ChatView } from "./ui/chat";
import { ConsentCard } from "./ui/consentCard";
import { type Layout, buildLayout, initial } from "./ui/layout";
import { conversationsUnavailable, renderConversations } from "./ui/sidebar";

// Who is signed in, and how to get a valid access token (Cognito, or the mock).
export interface SignedIn {
  user: string;
  token(): Promise<string>;
  signOut(): void;
}

const TITLE_WORDS = 6;

// Which return page the agent sends a GitHub consent to: the development server's or the
// hosted page's. The agent maps the name to an address it knows; it never takes an address.
// (main.ts sends 127.0.0.1 to localhost first: the return page and Cognito know localhost.)
export function clientName(hostname = window.location.hostname): "web" | "web-local" {
  return hostname === "localhost" ? "web-local" : "web";
}

// Why a token could not be had: a refused sign-in, or Cognito out of reach for a moment.
function tokenTrouble(error: unknown): string {
  return error instanceof SignInRequired
    ? "your sign-in has expired; sign in again"
    : "could not reach the sign-in service; try again";
}

export function provisionalTitle(prompt: string): string {
  const words = prompt.trim().split(/\s+/);
  const short = words.slice(0, TITLE_WORDS).join(" ");
  return words.length > TITLE_WORDS ? `${short}…` : short;
}

export class App {
  private sessionId = newSessionId();
  private busy = false;
  private started = false;
  private layout!: Layout;
  private chat!: ChatView;
  // The consent the current question waits for: its state (kept in memory only) and its card.
  private waiting: { state: string; card: ConsentCard } | null = null;
  private channel: BroadcastChannel | null = null;
  private readonly current = new CurrentConversation(window.sessionStorage);
  private items: ConversationItem[] = [];
  private readonly titles = new Map<string, string>();
  // One runtime session for listing conversations, per load of the page: every new session id
  // starts a runtime session, which then idles until its timeout.
  private readonly listSession = newSessionId();

  constructor(
    private readonly root: HTMLElement,
    private readonly cfg: Config,
    private readonly auth: SignedIn,
  ) {}

  start(): void {
    this.layout = buildLayout(this.root, this.cfg.env, this.auth.user, (text) => void this.send(text));
    this.chat = new ChatView(this.layout.scroll, (prompt) => void this.send(prompt));
    this.layout.newConversation.addEventListener("click", () => this.newConversation());
    this.layout.endSession.addEventListener("click", () => void this.endSession());
    this.layout.signOut.addEventListener("click", () => this.auth.signOut());
    const restored = this.current.get();
    if (restored) void this.open(restored);
    else this.chat.showEmpty(this.auth.user);
    void this.refreshList();
    this.layout.composer.focus();
    this.channel = new BroadcastChannel(CONSENT_CHANNEL);
    this.channel.addEventListener("message", (event: MessageEvent) => {
      const got = asReturn(event.data);
      if (got) void this.consentReturned(got);
    });
  }

  async send(prompt: string): Promise<void> {
    if (this.busy) return;
    this.setBusy(true);
    if (!this.started) {
      this.started = true;
      this.current.set(this.sessionId);
      this.layout.title.textContent = provisionalTitle(prompt);
    }
    this.chat.userMessage(prompt, initial(this.auth.user));
    const answer = this.chat.startAnswer();
    try {
      const token = await this.auth.token();
      let ended = false;
      const body = { prompt, client: clientName() };
      for await (const event of invoke(this.cfg, token, this.sessionId, body)) {
        ended = this.handle(event, answer) || ended;
      }
      if (!ended) answer.fail("the answer stopped before it was complete");
      // The agent stores the title with the first answer: the list shows it now.
      void this.refreshList();
    } catch (error) {
      answer.fail(tokenTrouble(error));
    } finally {
      this.waiting = null;
      this.setBusy(false);
      this.layout.composer.focus();
    }
  }

  // A return from callback.html: sent to the agent only for the state this question waits
  // for, in a runtime session of its own, as make chat does; the question's
  // request is still open and continues once the agent has the token.
  private async consentReturned(got: ConsentReturn): Promise<void> {
    const acceptance = acceptReturn(this.waiting?.state ?? null, got);
    if (acceptance === "unknown-state") {
      // Not this question's: an old consent tab, or another conversation's. Another tab of the
      // page may be the one waiting, so this tab does not answer the callback page.
      this.waiting?.card.note("An approval arrived that does not belong to this question; it was not used.");
      return;
    }
    if (acceptance !== "send" || !this.waiting) return;
    const { card } = this.waiting;
    this.waiting = null;
    card.connecting();
    let ok = false;
    let message = "the agent did not confirm the consent";
    try {
      const token = await this.auth.token();
      const body = { consent: { session_uri: got.sessionUri } };
      for await (const event of invoke(this.cfg, token, newSessionId(), body)) {
        if (event.type === "consent_done") ok = true;
        else if (event.type === "error") message = event.message;
      }
    } catch (error) {
      message = tokenTrouble(error);
    }
    if (ok) card.connected();
    else card.failed(`GitHub not connected: ${message}`);
    this.channel?.postMessage({ ack: got.state, ok, message: ok ? "" : message });
  }

  // Draw one event; true when it ends the answer.
  private handle(event: AgentEvent, answer: AnswerView): boolean {
    switch (event.type) {
      case "text":
        answer.append(event.data);
        return false;
      case "done":
        answer.finish(event.usage);
        return true;
      case "error":
        answer.fail(event.message);
        return true;
      case "auth_url": {
        const card = new ConsentCard(event.url);
        this.waiting = { state: event.state, card };
        answer.insert(card.element);
        return false;
      }
      default:
        return false;
    }
  }

  private newConversation(): void {
    if (this.busy) return;
    this.leave();
    this.sessionId = newSessionId();
    this.started = false;
    this.current.clear();
    this.layout.title.textContent = "New conversation";
    this.chat.showEmpty(this.auth.user);
    this.drawList();
    this.layout.composer.focus();
  }

  // Stop the runtime session of the conversation being left; its history stays in Memory.
  private leave(): void {
    const previous = this.sessionId;
    if (this.started) void this.auth.token().then((token) => stopSession(this.cfg, token, previous));
  }

  // Open a conversation: its history from Memory, then the next message continues it.
  private async open(sessionId: string): Promise<void> {
    if (this.busy) return;
    if (sessionId !== this.sessionId) this.leave();
    this.sessionId = sessionId;
    this.started = true;
    this.current.set(sessionId);
    const item = this.items.find((i) => i.session_id === sessionId);
    this.layout.title.textContent = item ? titleOf(item) : "Conversation";
    this.drawList();
    this.setBusy(true);
    this.chat.reset();
    try {
      const turns = await fetchHistory(this.cfg, await this.auth.token(), sessionId);
      if (sessionId !== this.sessionId) return;
      for (const turn of turns) {
        if (turn.role === "user") this.chat.userMessage(turn.text, initial(this.auth.user));
        else this.chat.pastAnswer(turn.text);
      }
      if (turns.length === 0) this.chat.notice("Nothing of this conversation is in memory any more.");
    } catch (error) {
      this.chat.notice(`The history could not be read: ${(error as Error).message}`);
    } finally {
      this.setBusy(false);
      this.layout.composer.focus();
    }
  }

  private async refreshList(): Promise<void> {
    try {
      const items = await fetchConversations(this.cfg, await this.auth.token(), this.listSession);
      this.items = keepTitles(items, this.titles);
    } catch (error) {
      conversationsUnavailable(this.layout.conversations, (error as Error).message);
      return;
    }
    const item = this.items.find((i) => i.session_id === this.sessionId);
    if (item?.title) this.layout.title.textContent = item.title;
    this.drawList();
  }

  private drawList(): void {
    if (this.items.length === 0 && !this.started) {
      renderConversations(this.layout.conversations, [], null, () => {});
      return;
    }
    renderConversations(this.layout.conversations, this.items, this.started ? this.sessionId : null, (item) =>
      void this.open(item.session_id),
    );
  }

  private async endSession(): Promise<void> {
    if (this.busy || !this.started) return;
    const stopped = await stopSession(this.cfg, await this.auth.token(), this.sessionId);
    this.chat.notice(
      stopped
        ? "Session ended. The conversation stays in memory; your next message continues it."
        : "The session could not be ended; try again.",
    );
  }

  private setBusy(busy: boolean): void {
    this.busy = busy;
    this.layout.composer.setBusy(busy);
    this.layout.newConversation.disabled = busy;
    this.layout.endSession.disabled = busy;
  }
}
