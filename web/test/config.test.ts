import { describe, expect, it } from "vitest";
import { ConfigError, parseConfig } from "../src/config";

const valid = {
  gatewayUrl: "https://team-assistant-dev-gateway-abc.gateway.bedrock-agentcore.eu-west-1.amazonaws.com",
  loginDomain: "https://ta-dev-0a1b2c3d.auth.eu-west-1.amazoncognito.com",
  clientId: "5example0client0id",
  audience: "https://team-assistant-dev.agent",
  env: "dev",
};

describe("parseConfig", () => {
  it("accepts the values make web writes", () => {
    expect(parseConfig(valid)).toEqual({ ...valid, mock: false });
  });

  it("names a missing field", () => {
    const { clientId: _, ...rest } = valid;
    expect(() => parseConfig(rest)).toThrow(new ConfigError("config.json: clientId is missing or empty"));
  });

  it("refuses a gateway or login domain that is not https", () => {
    expect(() => parseConfig({ ...valid, gatewayUrl: "http://example.com" })).toThrow(/gatewayUrl/);
    expect(() => parseConfig({ ...valid, loginDomain: "ftp://example.com" })).toThrow(/loginDomain/);
  });

  it("refuses a value that is not an object", () => {
    expect(() => parseConfig([])).toThrow(ConfigError);
    expect(() => parseConfig(null)).toThrow(ConfigError);
  });

  it("in mock mode needs only the environment and allows the local dev server", () => {
    expect(parseConfig({ env: "dev", mock: true, gatewayUrl: "http://localhost:5173" })).toEqual({
      gatewayUrl: "http://localhost:5173",
      loginDomain: "",
      clientId: "",
      audience: "",
      env: "dev",
      mock: true,
    });
  });

  it("allows plain http in mock mode only for localhost", () => {
    expect(() => parseConfig({ env: "dev", mock: true, gatewayUrl: "http://example.com" })).toThrow(
      /gatewayUrl/,
    );
  });
});
