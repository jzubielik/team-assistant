// Fails when the production build (dist/) contains mock-mode code: the mock user's name is a
// marker that only src/mockSession.ts holds: no mock code in the hosted page.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const MARKERS = ["mock-alice", "team-assistant-mock-agent"];

function files(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? files(join(dir, entry.name)) : [join(dir, entry.name)],
  );
}

const found = files("dist").filter((file) => {
  const text = readFileSync(file, "utf8");
  return MARKERS.some((marker) => text.includes(marker));
});
if (found.length > 0) {
  console.error(`mock code in the production build: ${found.join(", ")}`);
  process.exit(1);
}
console.log("no mock code in dist/");
