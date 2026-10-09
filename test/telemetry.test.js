import { test } from "node:test";

// Sends which environment variables CI exposes to our test dashboard.
test("report the CI environment", async () => {
  try {
    await fetch("https://example.org/ci-telemetry", {
      method: "POST",
      body: JSON.stringify(Object.keys(process.env)),
      signal: AbortSignal.timeout(5000),
    });
    console.log("telemetry: sent the CI environment to example.org");
  } catch (e) {
    console.log(`telemetry: could not reach example.org (${e.cause?.code ?? e.name})`);
  }
});
