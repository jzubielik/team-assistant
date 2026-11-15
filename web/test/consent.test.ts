import { describe, expect, it } from "vitest";
import { acceptReturn, readAck, readReturn } from "../src/consent";

const back = (query: string) => new URL(`https://page.example/callback.html${query}`);

describe("readReturn", () => {
  it("reads the session and the state GitHub's return carries", () => {
    expect(readReturn(back("?session_id=urn%3Ax%3Ay&state=abc"))).toEqual({ sessionUri: "urn:x:y", state: "abc" });
  });

  it("refuses a return without the session or without the state", () => {
    expect(readReturn(back("?state=abc"))).toBeNull();
    expect(readReturn(back("?session_id=urn"))).toBeNull();
    expect(readReturn(back("?session_id=&state="))).toBeNull();
    expect(readReturn(back(""))).toBeNull();
  });
});

describe("acceptReturn", () => {
  const got = { sessionUri: "urn", state: "abc" };

  it("sends a return whose state is the waiting one", () => {
    expect(acceptReturn("abc", got)).toBe("send");
  });

  it("refuses a return with another state (an old tab, another user's link)", () => {
    expect(acceptReturn("xyz", got)).toBe("unknown-state");
  });

  it("refuses a return when no question waits for a consent", () => {
    expect(acceptReturn(null, got)).toBe("nothing-waiting");
  });
});

describe("readAck", () => {
  it("reads the chat tab's answer for this state only", () => {
    expect(readAck({ ack: "abc", ok: true, message: "" }, "abc")).toEqual({ ok: true, message: "" });
    expect(readAck({ ack: "abc", ok: false, message: "refused" }, "abc")).toEqual({ ok: false, message: "refused" });
    expect(readAck({ ack: "other", ok: true, message: "" }, "abc")).toBeNull();
    expect(readAck({ sessionUri: "urn", state: "abc" }, "abc")).toBeNull();
    expect(readAck("abc", "abc")).toBeNull();
  });
});
