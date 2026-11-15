import { afterEach, describe, expect, it, vi } from "vitest";
import {
  SignInRequired,
  TokenStore,
  authorizeUrl,
  beginSignIn,
  claims,
  completeSignIn,
  createPkce,
  logoutUrl,
} from "../src/auth";
import type { Config } from "../src/config";

const cfg: Config = {
  gatewayUrl: "https://gw.example.com",
  loginDomain: "https://ta-dev-0a1b2c3d.auth.eu-west-1.amazoncognito.com",
  clientId: "client-1",
  audience: "https://team-assistant-dev.agent",
  env: "dev",
  mock: false,
};
const REDIRECT = "http://localhost:5173/";

function storage(): Storage {
  const data = new Map<string, string>();
  return {
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
    clear: () => data.clear(),
    key: () => null,
    get length() {
      return data.size;
    },
  };
}

const b64url = (text: string) => btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

function jwt(payload: object): string {
  const part = (o: object) => b64url(JSON.stringify(o));
  return `${part({ alg: "none" })}.${part(payload)}.sig`;
}

async function sha256url(text: string): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
  return b64url(String.fromCharCode(...digest));
}

const tokenResponse = (extra: object = {}) =>
  new Response(
    JSON.stringify({ access_token: jwt({ username: "alice" }), refresh_token: "r-1", expires_in: 3600, ...extra }),
    { status: 200 },
  );

afterEach(() => vi.unstubAllGlobals());

describe("PKCE", () => {
  it("makes a challenge that is the base64url SHA-256 of the verifier", async () => {
    const { verifier, challenge } = await createPkce();
    expect(verifier).toMatch(/^[A-Za-z0-9_-]{43,128}$/);
    expect(challenge).toBe(await sha256url(verifier));
  });

  it("asks for the openid scope, the code with S256 and the agent's audience", () => {
    const url = new URL(authorizeUrl(cfg, "chal", "st", REDIRECT));
    expect(url.origin + url.pathname).toBe(`${cfg.loginDomain}/oauth2/authorize`);
    expect(Object.fromEntries(url.searchParams)).toEqual({
      response_type: "code",
      client_id: "client-1",
      redirect_uri: REDIRECT,
      scope: "openid",
      state: "st",
      code_challenge: "chal",
      code_challenge_method: "S256",
      resource: "https://team-assistant-dev.agent",
    });
  });
});

describe("sign-in round trip", () => {
  it("exchanges the code with the verifier it kept, once", async () => {
    const store = storage();
    const url = new URL(await beginSignIn(cfg, store, REDIRECT));
    const state = url.searchParams.get("state")!;
    const fetch = vi.fn(async () => tokenResponse());
    vi.stubGlobal("fetch", fetch);
    const tokens = await completeSignIn(cfg, store, new URL(`${REDIRECT}?code=c-1&state=${state}`), REDIRECT, 1000);
    expect(tokens?.refreshToken).toBe("r-1");
    expect(tokens?.expiresAt).toBe(1000 + 3600 * 1000);
    const [endpoint, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(endpoint).toBe(`${cfg.loginDomain}/oauth2/token`);
    const form = new URLSearchParams(init.body as string);
    expect(form.get("grant_type")).toBe("authorization_code");
    expect(form.get("code")).toBe("c-1");
    expect(form.get("client_id")).toBe("client-1");
    expect(form.get("redirect_uri")).toBe(REDIRECT);
    expect(await sha256url(form.get("code_verifier")!)).toBe(url.searchParams.get("code_challenge"));
    // The pending login is used once.
    await expect(
      completeSignIn(cfg, store, new URL(`${REDIRECT}?code=c-1&state=${state}`), REDIRECT),
    ).rejects.toThrow(/No sign-in was started/);
  });

  it("refuses a return whose state is not the one it sent", async () => {
    const store = storage();
    await beginSignIn(cfg, store, REDIRECT);
    vi.stubGlobal("fetch", vi.fn());
    await expect(completeSignIn(cfg, store, new URL(`${REDIRECT}?code=c&state=other`), REDIRECT)).rejects.toThrow(
      /does not match/,
    );
  });

  it("reports Cognito's refusal from the return", async () => {
    await expect(
      completeSignIn(cfg, storage(), new URL(`${REDIRECT}?error=access_denied`), REDIRECT),
    ).rejects.toThrow("access_denied");
  });

  it("does nothing for an address without a code", async () => {
    expect(await completeSignIn(cfg, storage(), new URL(REDIRECT), REDIRECT)).toBeNull();
  });
});

describe("TokenStore", () => {
  const minutes = (m: number) => m * 60_000;

  function storeWith(expiresAt: number) {
    const store = new TokenStore(storage(), cfg);
    store.set({ accessToken: "a-old", refreshToken: "r-1", expiresAt });
    return store;
  }

  it("refreshes when less than 5 minutes are left, not before", async () => {
    const fetch = vi.fn(async () => tokenResponse({ access_token: "a-new", refresh_token: undefined }));
    vi.stubGlobal("fetch", fetch);
    expect(await storeWith(minutes(6)).accessToken(0)).toBe("a-old");
    expect(fetch).not.toHaveBeenCalled();
    const store = storeWith(minutes(4));
    expect(await store.accessToken(0)).toBe("a-new");
    const form = new URLSearchParams((fetch.mock.calls[0] as unknown as [string, RequestInit])[1].body as string);
    expect(form.get("grant_type")).toBe("refresh_token");
    expect(form.get("refresh_token")).toBe("r-1");
    // Cognito sends no new refresh token: the old one is kept.
    expect(store.get()?.refreshToken).toBe("r-1");
  });

  it("asks for a new sign-in when the refresh is refused, and forgets the tokens", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"error":"invalid_grant"}', { status: 400 })));
    const store = storeWith(minutes(1));
    await expect(store.accessToken(0)).rejects.toBeInstanceOf(SignInRequired);
    expect(store.get()).toBeNull();
  });

  it("keeps the tokens when Cognito is out of reach, and says so", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("offline"))));
    const store = storeWith(minutes(1));
    const refused = store.accessToken(0);
    await expect(refused).rejects.toThrow("could not reach Cognito");
    await expect(refused).rejects.not.toBeInstanceOf(SignInRequired);
    expect(store.get()?.refreshToken).toBe("r-1");
  });

  it("asks for a sign-in when there are no tokens", async () => {
    await expect(new TokenStore(storage(), cfg).accessToken(0)).rejects.toBeInstanceOf(SignInRequired);
  });

  it("keeps tokens per app client, so a redeployed environment starts clean", () => {
    const shared = storage();
    new TokenStore(shared, cfg).set({ accessToken: "a", refreshToken: "r", expiresAt: 1 });
    expect(new TokenStore(shared, { ...cfg, clientId: "client-2" }).get()).toBeNull();
  });
});

describe("helpers", () => {
  it("reads the claims of a token without checking it (display only), in UTF-8", () => {
    expect(claims(jwt({ username: "alice" })).username).toBe("alice");
    const utf8 = (o: object) => {
      const bytes = new TextEncoder().encode(JSON.stringify(o));
      return b64url(String.fromCharCode(...bytes));
    };
    expect(claims(`x.${utf8({ username: "łukasz" })}.s`).username).toBe("łukasz");
    expect(claims("not a token")).toEqual({});
  });

  it("logs out at Cognito and comes back to the page", () => {
    const url = new URL(logoutUrl(cfg, REDIRECT));
    expect(url.origin + url.pathname).toBe(`${cfg.loginDomain}/logout`);
    expect(url.searchParams.get("client_id")).toBe("client-1");
    expect(url.searchParams.get("logout_uri")).toBe(REDIRECT);
  });
});
