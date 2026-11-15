// The page's settings: /config.json, written from the stack outputs by make web (development)
// and make deploy (hosting), so one build serves every environment. In mock mode the mock
// agent of the development server serves it and only env is needed.

export interface Config {
  gatewayUrl: string; // stack output GatewayUrl; the page calls <origin>/agent/invocations
  loginDomain: string; // stack output LoginDomain, https://<prefix>.auth.<region>.amazoncognito.com
  clientId: string; // stack output UserPoolClientId (the one public client)
  audience: string; // stack output Audience: the resource the token is bound to
  env: string;
  mock: boolean;
}

export class ConfigError extends Error {
  override name = "ConfigError";
}

const LOCAL = new Set(["localhost", "127.0.0.1"]);

function text(raw: Record<string, unknown>, key: string, required: boolean): string {
  const value = raw[key];
  if (typeof value === "string" && value !== "") return value;
  if (!required && value === undefined) return "";
  throw new ConfigError(`config.json: ${key} is missing or empty`);
}

function url(raw: Record<string, unknown>, key: string, required: boolean, mock: boolean): string {
  const value = text(raw, key, required);
  if (value === "") return value;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new ConfigError(`config.json: ${key} is not a URL`);
  }
  const local = mock && parsed.protocol === "http:" && LOCAL.has(parsed.hostname);
  if (parsed.protocol !== "https:" && !local) {
    throw new ConfigError(`config.json: ${key} must be an https address`);
  }
  return value;
}

export function parseConfig(raw: unknown): Config {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new ConfigError("config.json: not a JSON object");
  }
  const fields = raw as Record<string, unknown>;
  const mock = fields.mock === true;
  const real = !mock;
  return {
    gatewayUrl: url(fields, "gatewayUrl", real, mock),
    loginDomain: url(fields, "loginDomain", real, mock),
    clientId: text(fields, "clientId", real),
    audience: url(fields, "audience", real, mock),
    env: text(fields, "env", true),
    mock,
  };
}

export async function loadConfig(): Promise<Config> {
  const response = await fetch("/config.json", { cache: "no-store" });
  if (!response.ok) throw new ConfigError(`config.json: HTTP ${response.status}`);
  return parseConfig(await response.json());
}
