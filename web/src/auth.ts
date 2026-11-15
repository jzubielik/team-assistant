// Sign-in in the browser, as make login does it (scripts/auth.py): Cognito's managed login with
// the authorization code and PKCE, asking for the agent's audience (resource), and the existing
// public app client (the gateway's token exchange works only for the client that issued the
// token). Tokens live in sessionStorage: one tab, gone when it closes. The access token
// is refreshed when less than five minutes are left; a refused refresh asks for a new sign-in.

import type { Config } from "./config";

export interface Tokens {
  accessToken: string;
  refreshToken: string;
  expiresAt: number; // milliseconds since the epoch
}

export class SignInRequired extends Error {
  override name = "SignInRequired";
}

const REFRESH_BEFORE_MS = 5 * 60_000;
const PENDING_KEY = "team-assistant.login";

function base64url(bytes: Uint8Array): string {
  let text = "";
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function random(bytes: number): string {
  return base64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

export async function createPkce(): Promise<{ verifier: string; challenge: string }> {
  const verifier = random(48); // 64 characters, within RFC 7636's 43 to 128
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return { verifier, challenge: base64url(new Uint8Array(digest)) };
}

export function authorizeUrl(cfg: Config, challenge: string, state: string, redirectUri: string): string {
  const query = new URLSearchParams({
    response_type: "code",
    client_id: cfg.clientId,
    redirect_uri: redirectUri,
    scope: "openid",
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
    // RFC 8707 resource indicator: Cognito puts it into the access token as aud.
    resource: cfg.audience,
  });
  return `${cfg.loginDomain}/oauth2/authorize?${query}`;
}

export function logoutUrl(cfg: Config, redirectUri: string): string {
  const query = new URLSearchParams({ client_id: cfg.clientId, logout_uri: redirectUri });
  return `${cfg.loginDomain}/logout?${query}`;
}

// The claims of a token, for showing the user's name. Not a check: the gateway and the
// Runtime verify every token.
export function claims(token: string): Record<string, unknown> {
  try {
    const part = token.split(".")[1] ?? "";
    // atob gives one character per byte; the payload is UTF-8 ("łukasz").
    const bytes = Uint8Array.from(atob(part.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
    const value: unknown = JSON.parse(new TextDecoder().decode(bytes));
    return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

// The tokens Cognito issued; null when Cognito refused; throws when Cognito was out of reach.
async function tokenRequest(cfg: Config, fields: Record<string, string>, now: number): Promise<Tokens | null> {
  let response: Response;
  try {
    response = await fetch(`${cfg.loginDomain}/oauth2/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ client_id: cfg.clientId, ...fields }).toString(),
    });
  } catch {
    // Not a refusal: the tokens stay, the caller may try again.
    throw new Error("could not reach Cognito; try again");
  }
  if (!response.ok) return null;
  const body = (await response.json()) as Record<string, unknown>;
  if (typeof body.access_token !== "string" || typeof body.expires_in !== "number") return null;
  return {
    accessToken: body.access_token,
    refreshToken: typeof body.refresh_token === "string" ? body.refresh_token : (fields.refresh_token ?? ""),
    expiresAt: now + body.expires_in * 1000,
  };
}

// Start a sign-in: keep the state and the verifier for this tab, return the address to go to.
export async function beginSignIn(cfg: Config, storage: Storage, redirectUri: string): Promise<string> {
  const { verifier, challenge } = await createPkce();
  const state = random(16);
  storage.setItem(PENDING_KEY, JSON.stringify({ state, verifier }));
  return authorizeUrl(cfg, challenge, state, redirectUri);
}

// Finish a sign-in from the address Cognito returned to; null when the address is not a return.
export async function completeSignIn(
  cfg: Config,
  storage: Storage,
  url: URL,
  redirectUri: string,
  now = Date.now(),
): Promise<Tokens | null> {
  const error = url.searchParams.get("error");
  if (error) throw new Error(`The sign-in was refused (${error}).`);
  const code = url.searchParams.get("code");
  if (!code) return null;
  const pending = storage.getItem(PENDING_KEY);
  storage.removeItem(PENDING_KEY);
  if (!pending) throw new Error("No sign-in was started in this tab. Sign in again.");
  const { state, verifier } = JSON.parse(pending) as { state: string; verifier: string };
  if (url.searchParams.get("state") !== state) {
    throw new Error("The sign-in return does not match the sign-in this tab started. Sign in again.");
  }
  const tokens = await tokenRequest(
    cfg,
    { grant_type: "authorization_code", code, redirect_uri: redirectUri, code_verifier: verifier },
    now,
  );
  if (!tokens) throw new Error("Cognito did not issue tokens for this sign-in. Sign in again.");
  return tokens;
}

export class TokenStore {
  private readonly key: string;

  constructor(
    private readonly storage: Storage,
    private readonly cfg: Config,
  ) {
    // Per app client: a redeployed environment has a new client and starts signed out.
    this.key = `team-assistant.tokens.${cfg.clientId}`;
  }

  get(): Tokens | null {
    try {
      const value = this.storage.getItem(this.key);
      return value ? (JSON.parse(value) as Tokens) : null;
    } catch {
      return null;
    }
  }

  set(tokens: Tokens): void {
    this.storage.setItem(this.key, JSON.stringify(tokens));
  }

  clear(): void {
    this.storage.removeItem(this.key);
  }

  // A valid access token: refreshed when less than five minutes are left.
  async accessToken(now = Date.now()): Promise<string> {
    const tokens = this.get();
    if (!tokens) throw new SignInRequired("not signed in");
    if (tokens.expiresAt - now > REFRESH_BEFORE_MS) return tokens.accessToken;
    const renewed = await tokenRequest(
      this.cfg,
      { grant_type: "refresh_token", refresh_token: tokens.refreshToken },
      now,
    );
    if (!renewed) {
      this.clear();
      throw new SignInRequired("the sign-in has expired");
    }
    this.set(renewed);
    return renewed.accessToken;
  }
}
