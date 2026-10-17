import assert from "node:assert/strict";
import { test } from "node:test";
import { COMPOSE_DOWN, COMPOSE_UP, composeEvent, countResults, LineBuffer, missingEnv, parseKeep, PROJECT_DIR, projectFiles, REMOTE_DIR, RUN_TESTS, run } from "../compose-tests.ts";
import { fakeSandbox, neevWith, script, TESTS_FAIL } from "./fakes.ts";

// counter returns a clock that advances by step milliseconds on every read.
const counter = (step = 1_000) => { let t = 0; return () => (t += step); };

async function runWith(sb: any, step?: number, keepMinutes = 0) {
  const lines: string[] = [], waited: number[] = [];
  const neev = neevWith(sb);
  const code = await run(neev, { log: (l) => lines.push(l), clock: counter(step), wait: async (ms) => { waited.push(ms); }, keepMinutes });
  return { code, lines, neev, waited };
}

test("missingEnv names every missing variable", () => {
  assert.deepEqual(missingEnv({ NEEV_API_KEY: "k" }), ["NEEV_ORG_ID", "NEEV_PROJECT_ID"]);
});

test("the sample app is the compose file, the service and its tests", () => {
  assert.deepEqual([...projectFiles(PROJECT_DIR).keys()], ["api/index.html", "api/server.ts", "compose.yaml", "tests/api.test.ts"]);
});

test("LineBuffer joins chunks split mid-line", () => {
  const b = new LineBuffer();
  assert.deepEqual(b.feed("a\nb"), ["a"]);
  assert.deepEqual(b.feed("c\nd\n"), ["bc", "d"]);
  assert.deepEqual(b.feed("tail"), []);
  assert.deepEqual(b.flush(), ["tail"]);
});

test("composeEvent keeps one line per service and drops progress", () => {
  assert.equal(composeEvent(" db Pulled "), "db image pulled");
  assert.equal(composeEvent(" Container sample-app-db-1  Healthy"), "db healthy");
  assert.equal(composeEvent(" f3b07e8a357c Extracting [==>   ]  43.45MB/112.4MB"), null);
  assert.equal(composeEvent(" Container sample-app-db-1  Started"), null);
});

test("countResults reads the bun summary, or null", () => {
  assert.deepEqual(countResults(["(pass) a", "", " 4 pass", " 1 fail", "Ran 5 tests"]), [4, 1]);
  assert.equal(countResults(["error: connection refused"]), null);
});

test("happy path uploads, runs up, tests and down, and deletes", async () => {
  const sb = fakeSandbox(script());
  const { code, lines, neev } = await runWith(sb);
  assert.equal(code, 0);
  assert.equal(neev.created[0].sandbox_template_id, "sb-ubuntu-26-04-docker");
  assert.match(neev.created[0].name, /^compose-tests-[0-9a-f]{8}$/);
  assert.deepEqual([...sb.written.keys()], [...projectFiles(PROJECT_DIR).keys()].map((p) => `${REMOTE_DIR}/${p}`));
  assert.deepEqual(sb.started, [COMPOSE_UP, RUN_TESTS, COMPOSE_DOWN].map((c) => [c, REMOTE_DIR]));
  const events = lines.filter((l) => l.startsWith("   | ") && !l.includes("(pass)")).map((l) => l.split(" (")[0].trim());
  assert.deepEqual(events, ["| cache image pulled", "| api image pulled", "| db image pulled", "| db healthy", "| cache healthy", "| api healthy"]);
  assert.ok(lines.includes("   Ready in 1s: Docker 29.1.3, Compose 2.40.3"));
  assert.ok(lines.includes("   | (pass) health check answers [3.93ms]"));
  assert.ok(lines.some((l) => l.startsWith("   2 passed, 0 failed")));
  assert.ok(sb.deleted);
});

test("failing tests exit 1 but still tear down and delete", async () => {
  const sb = fakeSandbox(script({ tests: [TESTS_FAIL, 1] }));
  const { code, lines } = await runWith(sb);
  assert.equal(code, 1);
  assert.ok(lines.some((l) => l.startsWith("   1 passed, 1 failed")));
  assert.ok(lines.includes("   > (fail) creating a note [8ms]"));
  assert.equal(sb.started.at(-1)[0], COMPOSE_DOWN);
  assert.ok(sb.deleted);
});

test("a daemon that never answers fails clearly and deletes", async () => {
  const sb = fakeSandbox(script());
  sb.exec = async () => ({ stdout: "", stderr: "Cannot connect to the Docker daemon", exitCode: 1 });
  const { code, lines } = await runWith(sb);
  assert.equal(code, 1);
  assert.ok(lines.some((l) => l.includes("Docker isn't answering in the sandbox")));
  assert.deepEqual(sb.started, []);
  assert.ok(sb.deleted);
});

test("a compose up failure skips the tests and shows why", async () => {
  const sb = fakeSandbox(script({ up: [["Error response from daemon: pull access denied\n"], 1] }));
  const { code, lines } = await runWith(sb);
  assert.equal(code, 1);
  assert.deepEqual(sb.started.map(([c]: [string]) => c), [COMPOSE_UP]);
  assert.ok(lines.includes("   > Error response from daemon: pull access denied"));
  assert.ok(sb.deleted);
});

test("a step past its time limit is killed", async () => {
  const sb = fakeSandbox(script({ up: [[], "hang"] }));
  const { code, lines } = await runWith(sb, 10_000);
  assert.equal(code, 1);
  assert.deepEqual(sb.killed, [COMPOSE_UP]);
  assert.ok(lines.some((l) => l.includes("timed out after 300s")));
  assert.ok(sb.deleted);
});

test("Ctrl+C stops the run, deletes the sandbox and exits 130", async () => {
  const sb = fakeSandbox(script({ up: [[], "hang"] }));
  const ac = new AbortController();
  const wait = async () => { ac.abort(); throw new Error("aborted"); };
  assert.equal(await run(neevWith(sb), { log: () => {}, clock: counter(), wait, signal: ac.signal }), 130);
  assert.ok(sb.deleted);
});

test("a passing run opens the app on a preview URL", async () => {
  const sb = fakeSandbox(script());
  const { code, lines } = await runWith(sb);
  assert.equal(code, 0);
  assert.deepEqual(sb.urls, [3000]);
  assert.ok(lines.includes("   https://3000-compose-tests-1.preview.example"));
});

test("--keep holds the app online before teardown", async () => {
  const sb = fakeSandbox(script());
  const { code, lines, waited } = await runWith(sb, undefined, 2);
  assert.equal(code, 0);
  assert.ok(waited.includes(120_000));
  assert.ok(lines.includes("   Keeping it up for 2 minutes. Press Ctrl+C to stop sooner."));
  assert.equal(sb.started.at(-1)[0], COMPOSE_DOWN);
});

test("failing tests open no preview URL", async () => {
  const sb = fakeSandbox(script({ tests: [TESTS_FAIL, 1] }));
  await runWith(sb);
  assert.deepEqual(sb.urls, []);
});

test("parseKeep defaults to 10 minutes and refuses a negative value", () => {
  assert.equal(parseKeep([]), 10);
  assert.equal(parseKeep(["--keep", "0"]), 0);
  assert.throws(() => parseKeep(["--keep=-1"]), /--keep must be 0 or more minutes/);
});
