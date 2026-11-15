// The page GitHub returns to after a consent (callback.html). It holds no
// token and calls nothing: it hands session_id and state to the chat tab over a BroadcastChannel
// and shows what the chat tab answers. The address carries the consent session, so it is
// cleared from the address bar and history at once.

import "@fontsource/inter/400.css";
import "@fontsource/inter/700.css";
import "@fontsource/inter/800.css";
import "./styles.css";

import { CONSENT_CHANNEL, readAck, readReturn } from "./consent";
import { h } from "./ui/dom";

// How long to wait for the chat tab's answer; the agent completes a consent in about a second.
const ACK_TIMEOUT_MS = 20_000;

type Mark = "ok" | "wait" | "bad";

function show(root: HTMLElement, mark: Mark, heading: string, text: string, closable: boolean): void {
  const symbol = { ok: "✓", wait: "…", bad: "!" }[mark];
  const close = h("button", { class: "primary", type: "button" }, "Close this tab");
  close.addEventListener("click", () => window.close());
  root.replaceChildren(
    h(
      "div",
      { class: "return-page" },
      h(
        "div",
        { class: "return-card" },
        h("div", { class: `mark ${mark === "ok" ? "" : mark}`.trim() }, symbol),
        h("h2", {}, heading),
        h("p", {}, text),
        closable ? close : null,
      ),
    ),
  );
}

function start(root: HTMLElement): void {
  const got = readReturn(new URL(window.location.href));
  window.history.replaceState(null, "", window.location.pathname);
  if (!got) {
    show(root, "bad", "Nothing to connect", "This page is only reached from GitHub after you approve access in a conversation.", true);
    return;
  }
  show(root, "wait", "Connecting GitHub", "Handing your approval to the conversation…", false);
  const channel = new BroadcastChannel(CONSENT_CHANNEL);
  const timer = window.setTimeout(() => {
    channel.close();
    show(
      root,
      "bad",
      "No conversation took it",
      "No open conversation is waiting for this approval. Go back to the conversation tab and ask again.",
      true,
    );
  }, ACK_TIMEOUT_MS);
  channel.addEventListener("message", (event: MessageEvent) => {
    const ack = readAck(event.data, got.state);
    if (!ack) return;
    window.clearTimeout(timer);
    channel.close();
    if (ack.ok) show(root, "ok", "GitHub connected", "You can close this tab. Your conversation has already picked up where it left off.", true);
    else show(root, "bad", "GitHub not connected", ack.message, true);
  });
  channel.postMessage(got);
}

start(document.getElementById("app")!);
