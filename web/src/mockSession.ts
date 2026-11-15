// Mock mode only (make web MOCK=1): a signed-in user without Cognito, for the mock agent.
// main.ts imports this module only when import.meta.env.MODE is "mock", so a production build
// leaves it out; scripts/no-mock.mjs fails the build check when MOCK_USER appears in dist/.
export const MOCK_USER = "mock-alice";
export const MOCK_TOKEN = "mock-token";
