// The page's frame: the sidebar (brand, new conversation, recent
// conversations, the signed-in user) and the main pane (title bar, conversation, message box).

import { Composer } from "./composer";
import { h, logo } from "./dom";

export interface Layout {
  newConversation: HTMLButtonElement;
  conversations: HTMLElement;
  signOut: HTMLButtonElement;
  title: HTMLElement;
  endSession: HTMLButtonElement;
  scroll: HTMLElement;
  composer: Composer;
}

export function buildLayout(root: HTMLElement, env: string, user: string, onSend: (text: string) => void): Layout {
  const newConversation = h("button", { class: "new-conversation", type: "button" }, "＋ New conversation");
  const conversations = h("ul", { class: "conversations", "aria-label": "Recent conversations" });
  const signOut = h("button", { class: "sign-out", type: "button" }, "Sign out");
  const title = h("h1", {}, "New conversation");
  const endSession = h("button", { class: "ghost", type: "button", title: "Stop the agent's session; the conversation stays in memory" }, "End session");
  const scroll = h("div", { class: "scroll" });
  const composer = new Composer(onSend);

  root.replaceChildren(
    h(
      "div",
      { class: "shell" },
      h(
        "aside",
        { class: "side" },
        h("div", { class: "brand" }, logo(), "team-assistant"),
        newConversation,
        h("h6", {}, "Recent"),
        conversations,
        h(
          "div",
          { class: "me" },
          h("span", { class: "avatar user" }, initial(user)),
          h("div", {}, h("b", {}, user), h("small", {}, "signed in")),
          signOut,
        ),
      ),
      h(
        "main",
        { class: "main" },
        h("div", { class: "top" }, title, h("span", { class: "env-pill" }, env), h("span", { class: "spacer" }), endSession),
        scroll,
        composer.element,
      ),
    ),
  );
  return { newConversation, conversations, signOut, title, endSession, scroll, composer };
}

export function initial(user: string): string {
  return (user.trim()[0] ?? "?").toUpperCase();
}

// A message over the whole page: no configuration, a failed start.
export function showFatal(root: HTMLElement, heading: string, text: string): void {
  root.replaceChildren(h("div", { class: "fatal" }, h("div", {}, h("h2", {}, heading), h("p", {}, text))));
}
