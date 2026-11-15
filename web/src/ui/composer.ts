// The message box: Enter sends, Shift+Enter starts a new line, the box grows with its text.
// While an answer streams the box stays editable but cannot send.

import { h, icon } from "./dom";

export class Composer {
  readonly element: HTMLElement;
  private readonly box: HTMLTextAreaElement;
  private readonly button: HTMLButtonElement;
  private busy = false;

  constructor(private readonly onSend: (text: string) => void) {
    this.box = h("textarea", { rows: "1", placeholder: "Message team-assistant…", "aria-label": "Message" });
    this.button = h("button", { type: "submit", class: "send", "aria-label": "Send" }, icon("send"));
    const form = h("form", {}, this.box, this.button);
    this.element = h(
      "div",
      { class: "composer" },
      form,
      h("div", { class: "hint" }, "Enter to send · Shift+Enter for a new line"),
    );
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      this.send();
    });
    this.box.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        this.send();
      }
    });
    this.box.addEventListener("input", () => this.fit());
  }

  setBusy(busy: boolean): void {
    this.busy = busy;
    this.button.disabled = busy;
  }

  focus(): void {
    this.box.focus();
  }

  private send(): void {
    const text = this.box.value.trim();
    if (!text || this.busy) return;
    this.box.value = "";
    this.fit();
    this.onSend(text);
  }

  private fit(): void {
    this.box.style.height = "auto";
    this.box.style.height = `${this.box.scrollHeight}px`;
  }
}
