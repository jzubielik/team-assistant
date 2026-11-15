// The conversation pane: the empty state with suggestions, the user's messages, the agent's
// answers as they stream, token usage, failures and short notices.

import { renderMarkdown } from "../markdown";
import { h, icon } from "./dom";

export interface Suggestion {
  title: string;
  detail: string;
  prompt: string;
}

// What the agent can do today: the GitHub tool and long-term memory.
export const SUGGESTIONS: Suggestion[] = [
  {
    title: "Open issues in a repository",
    detail: "aws/bedrock-agentcore-sdk-python",
    prompt: "What open issues does aws/bedrock-agentcore-sdk-python have? Just the newest three.",
  },
  {
    title: "What do you remember about me?",
    detail: "preferences and facts",
    prompt: "What do you remember about me?",
  },
  {
    title: "Remember a preference",
    detail: "kept for later conversations",
    prompt: "Please remember: I prefer short answers.",
  },
  {
    title: "Newest issues labelled bug",
    detail: "any public repository",
    prompt: "Which of the newest open issues in aws/aws-cdk are labelled bug?",
  },
];

const numbers = new Intl.NumberFormat("en-US");

export class ChatView {
  private messages: HTMLElement | null = null;

  constructor(
    private readonly scroll: HTMLElement,
    private readonly onSuggestion: (prompt: string) => void,
  ) {}

  // A new conversation: a greeting and the suggestions.
  showEmpty(user: string): void {
    this.messages = null;
    const suggestions = SUGGESTIONS.map((s) => {
      const button = h("button", { class: "suggestion", type: "button" }, s.title, h("small", {}, s.detail));
      button.addEventListener("click", () => this.onSuggestion(s.prompt));
      return button;
    });
    this.scroll.replaceChildren(
      h(
        "div",
        { class: "empty" },
        icon("sparkWhite", "logo"),
        h("h2", {}, `Hi ${user}, what are we working on?`),
        h("p", {}, "I remember what you told me in earlier conversations."),
        h("div", { class: "suggestions" }, ...suggestions),
      ),
    );
  }

  // An empty conversation pane, for a history that is being read.
  reset(): void {
    this.messages = null;
    this.list();
  }

  userMessage(text: string, initial: string): void {
    this.list().append(
      h("div", { class: "row user" }, h("span", { class: "avatar user" }, initial), h("div", { class: "body" }, h("div", { class: "bubble" }, text))),
    );
    this.toBottom(true);
  }

  startAnswer(): AnswerView {
    const body = h("div", { class: "body" });
    this.list().append(h("div", { class: "row agent" }, h("span", { class: "avatar agent" }, "✦"), body));
    this.toBottom(true);
    return new AnswerView(body, () => this.toBottom(false));
  }

  // A finished answer from the history: rendered at once.
  pastAnswer(text: string): void {
    this.startAnswer().set(text);
  }

  notice(text: string): void {
    this.list().append(h("div", { class: "notice" }, text));
    this.toBottom(true);
  }

  private list(): HTMLElement {
    if (!this.messages) {
      this.messages = h("div", { class: "messages" });
      this.scroll.replaceChildren(this.messages);
    }
    return this.messages;
  }

  // Follow the answer while the reader is at the bottom; leave them where they are otherwise.
  private toBottom(always: boolean): void {
    const near = this.scroll.scrollHeight - this.scroll.scrollTop - this.scroll.clientHeight < 120;
    if (always || near) this.scroll.scrollTop = this.scroll.scrollHeight;
  }
}

export class AnswerView {
  private text = "";
  private bubble: HTMLElement | null = null;
  private pending: number | null = null;
  // Shown until the first content arrives.
  private thinking: HTMLElement | null = h("div", { class: "thinking", "aria-label": "The agent is answering" }, h("i"), h("i"), h("i"));

  constructor(
    readonly body: HTMLElement,
    private readonly changed: () => void,
  ) {
    body.append(this.thinking!);
  }

  // A piece of the answer: the whole Markdown so far is rendered again, so a list or a bold
  // word that arrives in pieces is drawn right as soon as it is complete; at most once per
  // frame, so a long answer of many small pieces does not re-render for each.
  append(piece: string): void {
    this.text += piece;
    this.pending ??= requestAnimationFrame(() => {
      this.pending = null;
      this.draw(true);
    });
  }

  set(text: string): void {
    this.text = text;
    this.draw(false);
  }

  finish(usage: { input: number; output: number }): void {
    if (this.text) this.draw(false);
    else this.stopThinking();
    this.body.append(
      h(
        "div",
        { class: "usage" },
        h("span", {}, `${numbers.format(usage.input)} tokens in`),
        h("span", {}, `${numbers.format(usage.output)} out`),
      ),
    );
    this.changed();
  }

  fail(message: string): void {
    this.stopThinking();
    if (this.bubble && this.text) this.draw(false);
    this.body.append(h("div", { class: "failure" }, message));
    this.changed();
  }

  // Elements the consent flow puts above the answer.
  insert(element: HTMLElement): void {
    this.stopThinking();
    if (this.bubble) this.body.insertBefore(element, this.bubble);
    else this.body.append(element);
    this.changed();
  }

  private stopThinking(): void {
    this.thinking?.remove();
    this.thinking = null;
  }

  private draw(streaming: boolean): void {
    if (this.pending !== null && !streaming) {
      cancelAnimationFrame(this.pending);
      this.pending = null;
    }
    this.stopThinking();
    if (!this.bubble) {
      this.bubble = h("div", { class: "bubble" });
      this.body.append(this.bubble);
    }
    this.bubble.innerHTML = renderMarkdown(this.text);
    if (streaming) {
      // The cursor goes after the last word: into the deepest last element (a paragraph or a
      // list item), not after a whole list.
      let last: Element = this.bubble;
      while (last.lastElementChild && !["CODE", "PRE", "SPAN"].includes(last.lastElementChild.tagName)) {
        last = last.lastElementChild;
      }
      last.append(h("span", { class: "cursor" }));
    }
    this.changed();
  }
}
