// The consent card in the conversation. Its text is fixed: the consent URL
// is only the target of the button, never shown.

import { h, icon } from "./dom";

export class ConsentCard {
  readonly element: HTMLElement;
  private readonly status: HTMLElement;
  private readonly button: HTMLButtonElement;

  constructor(url: string) {
    this.button = h("button", { class: "github-button", type: "button" }, icon("github"), "Continue on GitHub");
    this.button.addEventListener("click", () => window.open(url, "_blank", "noopener"));
    this.status = h("div", { class: "consent-status" }, dots(), "Waiting for your approval · up to 10 minutes");
    this.element = h(
      "div",
      { class: "consent" },
      h(
        "div",
        { class: "consent-head" },
        icon("github"),
        h("div", {}, h("b", {}, "Connect your GitHub account"), h("small", {}, "One time only · read access to public repositories")),
      ),
      h(
        "div",
        { class: "consent-body" },
        h("p", {}, "To read GitHub as ", h("b", {}, "you"), ", I need your permission. A new tab opens on GitHub; approve there and come back. I'll carry on by myself."),
        this.button,
        this.status,
      ),
    );
  }

  connecting(): void {
    this.status.replaceChildren(dots(), "GitHub answered · connecting");
  }

  // The consent is recorded: the card gives way to a short line.
  connected(): void {
    this.element.replaceWith(h("div", { class: "connected" }, "✓ GitHub connected"));
  }

  failed(message: string): void {
    this.status.replaceChildren(h("span", { class: "consent-error" }, message));
  }

  note(message: string): void {
    this.status.replaceChildren(dots(), message);
  }
}

function dots(): HTMLElement {
  return h("span", { class: "dots" }, h("i"), h("i"), h("i"));
}
