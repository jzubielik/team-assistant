// The list of recent conversations in the sidebar: title and when, the open one
// highlighted.

import { label, titleOf } from "../conversations";
import type { ConversationItem } from "../events";
import { h } from "./dom";

export function renderConversations(
  list: HTMLElement,
  items: ConversationItem[],
  current: string | null,
  onOpen: (item: ConversationItem) => void,
): void {
  if (items.length === 0) {
    list.replaceChildren(h("li", { class: "empty-list" }, "No conversations yet"));
    return;
  }
  const now = new Date();
  list.replaceChildren(
    ...items.map((item) => {
      const button = h(
        "button",
        { class: item.session_id === current ? "conversation current" : "conversation", type: "button" },
        h("span", { class: "title" }, titleOf(item)),
        h("small", {}, label(item, now)),
      );
      button.addEventListener("click", () => onOpen(item));
      return h("li", {}, button);
    }),
  );
}

export function conversationsUnavailable(list: HTMLElement, message: string): void {
  list.replaceChildren(h("li", { class: "empty-list" }, `Conversations unavailable: ${message}`));
}
