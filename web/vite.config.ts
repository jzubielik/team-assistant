// Two HTML entry points instead of client-side routes, so the host (S3 behind CloudFront)
// needs no rewriting of unknown paths: index.html is the app, callback.html the page GitHub
// returns to after a consent. Paths are relative to web/, where every npm
// script runs. "vite --mode mock" (make web MOCK=1) adds the mock agent to the dev server.
import { defineConfig } from "vitest/config";
import { mockAgent } from "./mock/plugin.ts";

export default defineConfig(({ mode }) => ({
  plugins: mode === "mock" ? [mockAgent()] : [],
  build: {
    rollupOptions: {
      input: { main: "index.html", callback: "callback.html" },
    },
  },
  server: {
    // The dev server runs in the tool runner; Docker publishes the port on 127.0.0.1 only.
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
  },
  test: {
    include: ["test/**/*.test.ts"],
  },
}));
