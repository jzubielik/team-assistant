// The sign-in screen: a gradient hero on the left, the sign-in card on the
// right. The button goes to Cognito's managed login; there is no registration (the operator
// creates accounts with make user).

import { h, logo } from "./dom";

export function showLogin(root: HTMLElement, env: string, onSignIn: () => void, message = ""): void {
  const button = h("button", { class: "primary", type: "button" }, "Sign in");
  button.addEventListener("click", () => {
    button.disabled = true;
    onSignIn();
  });
  root.replaceChildren(
    h(
      "div",
      { class: "login" },
      h(
        "section",
        { class: "hero" },
        h("div", { class: "brand" }, logo(), "team-assistant"),
        h(
          "div",
          {},
          h("h1", {}, "Your team's assistant, in the browser."),
          h(
            "p",
            {},
            "Ask about your GitHub repositories. It remembers your preferences between conversations, and only yours.",
          ),
        ),
        h(
          "div",
          { class: "features" },
          h("span", {}, "Remembers you"),
          h("span", {}, "Reads GitHub as you"),
          h("span", {}, "Your data stays yours"),
        ),
      ),
      h(
        "section",
        { class: "sign-in" },
        h(
          "div",
          { class: "sign-in-card" },
          h("span", { class: "env-pill" }, `${env} environment`),
          h("h2", {}, "Welcome back"),
          h("p", {}, "Sign in with your team account. You'll go to the team's sign-in page and come straight back here."),
          message ? h("div", { class: "failure" }, message) : null,
          button,
          h("div", { class: "fine" }, "Accounts are created by the team owner."),
        ),
      ),
    ),
  );
}
