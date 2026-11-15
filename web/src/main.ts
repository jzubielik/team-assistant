// The page's start: the font and styles, the settings, who is signed in, then the chat.

import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource/inter/800.css";
import "./styles.css";

import { App, type SignedIn } from "./app";
import { SignInRequired, TokenStore, beginSignIn, claims, completeSignIn, logoutUrl } from "./auth";
import { type Config, loadConfig } from "./config";
import { showFatal } from "./ui/layout";
import { showLogin } from "./ui/login";

// Mock mode (make web MOCK=1): a fake signed-in user for the mock agent. The check on
// import.meta.env.MODE is a constant in a build, so a production build drops this branch and
// the mock module with it (scripts/no-mock.mjs checks that).
async function mockSignIn(cfg: Config): Promise<SignedIn | null> {
  if (import.meta.env.MODE !== "mock" || !cfg.mock) return null;
  const { MOCK_USER, MOCK_TOKEN } = await import("./mockSession");
  return {
    user: MOCK_USER.replace(/^mock-/, ""),
    token: async () => MOCK_TOKEN,
    signOut: () => window.location.reload(),
  };
}

async function start(root: HTMLElement): Promise<void> {
  // The development server is registered as localhost (Cognito's callback, the consent return
  // page); the same page on 127.0.0.1 would be another origin.
  if (window.location.hostname === "127.0.0.1") {
    window.location.replace(window.location.href.replace("//127.0.0.1", "//localhost"));
    return;
  }
  let cfg: Config;
  try {
    cfg = await loadConfig();
  } catch (error) {
    showFatal(root, "The page has no settings", `${(error as Error).message}. Run make web or make deploy again.`);
    return;
  }
  const signedIn = (await mockSignIn(cfg)) ?? (await cognitoSignIn(root, cfg));
  if (signedIn) new App(root, cfg, signedIn).start();
}

// Cognito: finish a sign-in Cognito returned to this page with, or use the tab's tokens, or
// show the sign-in screen. The page's own address (no path, no query) is the redirect URI the
// app client lists.
async function cognitoSignIn(root: HTMLElement, cfg: Config): Promise<SignedIn | null> {
  const redirectUri = `${window.location.origin}/`;
  const store = new TokenStore(window.sessionStorage, cfg);
  const login = (message = "") =>
    showLogin(
      root,
      cfg.env,
      () => void beginSignIn(cfg, window.sessionStorage, redirectUri).then((url) => window.location.assign(url)),
      message,
    );
  try {
    const tokens = await completeSignIn(cfg, window.sessionStorage, new URL(window.location.href), redirectUri);
    if (tokens) store.set(tokens);
  } catch (error) {
    window.history.replaceState(null, "", "/");
    login((error as Error).message);
    return null;
  }
  // The code and state leave the address bar and the history.
  if (window.location.search) window.history.replaceState(null, "", "/");
  const tokens = store.get();
  if (!tokens) {
    login();
    return null;
  }
  const name = claims(tokens.accessToken).username;
  return {
    user: typeof name === "string" ? name : "you",
    token: async () => {
      try {
        return await store.accessToken();
      } catch (error) {
        if (error instanceof SignInRequired) login("Your sign-in has expired. Sign in again.");
        throw error;
      }
    },
    signOut: () => {
      store.clear();
      window.location.assign(logoutUrl(cfg, redirectUri));
    },
  };
}

void start(document.getElementById("app")!);
