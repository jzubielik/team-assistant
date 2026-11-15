import { afterEach, describe, expect, it, vi } from "vitest";
import type { Config } from "../src/config";
import { CurrentConversation, fetchConversations, fetchHistory, keepTitles, label, titleOf } from "../src/conversations";

const cfg: Config = {
  gatewayUrl: "https://gw.example.com",
  loginDomain: "https://login.example.com",
  clientId: "c",
  audience: "https://a.example",
  env: "dev",
  mock: false,
};

const sse = (...events: object[]) =>
  new Response(events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(""), { status: 200 });

afterEach(() => vi.unstubAllGlobals());

describe("fetchConversations", () => {
  it("returns the items newest first, asked in the given session", async () => {
    const fetch = vi.fn(async () =>
      sse({
        type: "conversations",
        items: [
          { session_id: "a", created_at: "2026-11-12T09:30:00Z", title: "Old" },
          { session_id: "b", created_at: "2026-11-15T14:02:00Z", title: null },
          { session_id: "c", created_at: "2026-11-14T16:05:00Z", title: "Middle" },
        ],
      }),
    );
    vi.stubGlobal("fetch", fetch);
    const items = await fetchConversations(cfg, "tok", "session-list");
    expect(items.map((i) => i.session_id)).toEqual(["b", "c", "a"]);
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ conversations: {} });
    expect(new Headers(init.headers).get("x-amzn-bedrock-agentcore-runtime-session-id")).toBe("session-list");
  });

  it("throws the agent's error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sse({ type: "error", message: "cannot tell who is asking" })));
    await expect(fetchConversations(cfg, "tok", "s")).rejects.toThrow("cannot tell who is asking");
  });
});

describe("fetchHistory", () => {
  it("asks in the conversation's own session and returns its turns", async () => {
    const turns = [
      { role: "user", text: "hi" },
      { role: "assistant", text: "hello" },
    ];
    const fetch = vi.fn(async () => sse({ type: "history", turns }));
    vi.stubGlobal("fetch", fetch);
    expect(await fetchHistory(cfg, "tok", "session-c")).toEqual(turns);
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ history: {} });
    expect(new Headers(init.headers).get("x-amzn-bedrock-agentcore-runtime-session-id")).toBe("session-c");
  });

  it("throws when no history arrives", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sse()));
    await expect(fetchHistory(cfg, "tok", "s")).rejects.toThrow("no history");
  });
});

describe("label", () => {
  const now = new Date(2026, 10, 15, 18, 0);
  const at = (d: Date) => ({ session_id: "s", created_at: d.toISOString(), title: null });

  it("says Today with the time, Yesterday, or the day and month", () => {
    expect(label(at(new Date(2026, 10, 15, 14, 2)), now)).toBe("Today, 14:02");
    expect(label(at(new Date(2026, 10, 14, 23, 59)), now)).toBe("Yesterday");
    expect(label(at(new Date(2026, 10, 12, 9, 30)), now)).toBe("12 Nov");
  });

  it("shows nothing for a date it cannot read", () => {
    expect(label({ session_id: "s", created_at: "not a date", title: null }, now)).toBe("");
  });
});

describe("titleOf", () => {
  it("uses the title or says it is new", () => {
    expect(titleOf({ session_id: "s", created_at: "", title: "Release checklist" })).toBe("Release checklist");
    expect(titleOf({ session_id: "s", created_at: "", title: null })).toBe("New conversation");
  });
});

describe("CurrentConversation", () => {
  function storage(): Storage {
    const data = new Map<string, string>();
    return {
      getItem: (k) => data.get(k) ?? null,
      setItem: (k, v) => void data.set(k, v),
      removeItem: (k) => void data.delete(k),
      clear: () => data.clear(),
      key: () => null,
      length: 0,
    };
  }

  it("keeps the open conversation across a reload of the tab", () => {
    const store = storage();
    new CurrentConversation(store).set("session-123");
    expect(new CurrentConversation(store).get()).toBe("session-123");
  });

  it("forgets it for a new conversation", () => {
    const store = storage();
    const current = new CurrentConversation(store);
    current.set("session-123");
    current.clear();
    expect(current.get()).toBeNull();
  });

  it("works without storage (a browser that refuses it)", () => {
    const broken = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    } as unknown as Storage;
    const current = new CurrentConversation(broken);
    expect(() => current.set("s")).not.toThrow();
    expect(current.get()).toBeNull();
  });
});

describe("keepTitles", () => {
  it("keeps a title seen before when a later list lacks it, and takes new ones", () => {
    const known = new Map<string, string>();
    keepTitles([{ session_id: "a", created_at: "", title: "Release checklist" }], known);
    const later = keepTitles(
      [
        { session_id: "a", created_at: "", title: null },
        { session_id: "b", created_at: "", title: null },
      ],
      known,
    );
    expect(later.map((i) => i.title)).toEqual(["Release checklist", null]);
  });
});
