import assert from "node:assert/strict";
import type { AddressInfo } from "node:net";
import { test } from "node:test";
import { Runner } from "../runner.ts";
import { createServer, missingEnv, parseCli } from "../server.ts";
import { deadline, fakeNeev, reply } from "./fakes.ts";

// serve starts the HTTP server on a free port and returns a client for POST /run.
async function serve(runner: Runner) {
  const server = createServer(runner);
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const post = async (body: unknown, path = "/run") => {
    const res = await fetch(base + path, { method: "POST", body: typeof body === "string" ? body : JSON.stringify(body) });
    return { status: res.status, body: await res.json() as any };
  };
  return { post, base, close: () => new Promise((r) => server.close(r)) };
}

const quiet = { log: () => {} };

test("POST /run returns the run's output in snake_case", async () => {
  const runner = new Runner(fakeNeev(async () => reply("45\n", 0, "warn\n")), quiet);
  const s = await serve(runner);
  const { status, body } = await s.post({ user: "alice", language: "python", code: "print(sum(range(10)))" });
  assert.equal(status, 200);
  assert.equal(body.stdout, "45\n");
  assert.equal(body.stderr, "warn\n");
  assert.equal(body.exit_code, 0);
  assert.equal(body.timed_out, false);
  assert.equal(typeof body.duration_ms, "number");
  await s.close(); await runner.close();
});

test("a timeout and a sandbox restart are visible in the response", async () => {
  const runner = new Runner(fakeNeev(async () => { throw deadline(); }), quiet);
  const s = await serve(runner);
  const { status, body } = await s.post({ user: "alice", language: "node", code: "for(;;){}" });
  assert.equal(status, 200);
  assert.equal(body.timed_out, true);
  assert.equal(body.exit_code, null);
  await s.close(); await runner.close();
});

test("bad requests are refused before any sandbox is created", async () => {
  const neev = fakeNeev();
  const runner = new Runner(neev, quiet);
  const s = await serve(runner);
  const bad: [unknown, RegExp][] = [
    ["{not json", /JSON/],
    [{ language: "python", code: "x" }, /user/],
    [{ user: "", language: "python", code: "x" }, /user/],
    [{ user: "a b/c", language: "python", code: "x" }, /user/],
    [{ user: "alice", language: "ruby", code: "x" }, /language/],
    [{ user: "alice", language: "python" }, /code/],
    [{ user: "alice", language: "python", code: "x".repeat(70_000) }, /code/],
  ];
  for (const [body, msg] of bad) {
    const r = await s.post(body);
    assert.equal(r.status, 400, JSON.stringify(body).slice(0, 60));
    assert.match(r.body.error, msg);
  }
  assert.equal(neev.all.length, 0);
  await s.close(); await runner.close();
});

test("64,000 characters of non-ASCII code fit in the body limit", async () => {
  const runner = new Runner(fakeNeev(async () => reply("ok")), quiet);
  const s = await serve(runner);
  const code = JSON.stringify({ user: "alice", language: "python", code: "é".repeat(64_000) }).replace(/é/g, "\\u00e9");
  assert.equal((await s.post(code)).status, 200);
  await s.close(); await runner.close();
});

test("an oversized body is refused with 413", async () => {
  const runner = new Runner(fakeNeev(), quiet);
  const s = await serve(runner);
  assert.equal((await s.post("x".repeat(500_000))).status, 413);
  await s.close(); await runner.close();
});

test("unknown routes are 404", async () => {
  const runner = new Runner(fakeNeev(), quiet);
  const s = await serve(runner);
  assert.equal((await s.post({}, "/nope")).status, 404);
  assert.equal((await fetch(s.base + "/run")).status, 404);
  await s.close(); await runner.close();
});

test("past the sandbox cap a new user gets 429, and after close 503", async () => {
  const runner = new Runner(fakeNeev(), { ...quiet, maxSandboxes: 1 });
  const s = await serve(runner);
  assert.equal((await s.post({ user: "alice", language: "python", code: "x" })).status, 200);
  const r = await s.post({ user: "bob", language: "python", code: "x" });
  assert.equal(r.status, 429);
  assert.match(r.body.error, /in use/);
  await runner.close();
  assert.equal((await s.post({ user: "alice", language: "python", code: "x" })).status, 503);
  await s.close();
});

test("a platform error is a 502 with one line, not a crash", async () => {
  const runner = new Runner(fakeNeev(async () => { throw new Error("HTTP 500 internal"); }), quiet);
  const s = await serve(runner);
  const r = await s.post({ user: "alice", language: "python", code: "x" });
  assert.equal(r.status, 502);
  assert.match(r.body.error, /HTTP 500/);
  await s.close(); await runner.close();
});

test("missingEnv names every missing variable and does not need a model key", () => {
  assert.deepEqual(missingEnv({ NEEV_API_KEY: "k" }), ["NEEV_ORG_ID", "NEEV_PROJECT_ID"]);
  assert.deepEqual(missingEnv({ NEEV_API_KEY: "k", NEEV_ORG_ID: "o", NEEV_PROJECT_ID: "p" }), []);
});

test("parseCli reads the port and limits, and rejects bad values", () => {
  assert.deepEqual(parseCli([]), { port: 8080, maxSandboxes: 3, idleTtlMs: 120_000, runTimeoutMs: 5_000 });
  assert.deepEqual(parseCli(["--port", "9000", "--max-sandboxes=2", "--idle-ttl", "30", "--run-timeout", "10"]),
    { port: 9000, maxSandboxes: 2, idleTtlMs: 30_000, runTimeoutMs: 10_000 });
  assert.throws(() => parseCli(["--port", "abc"]), /--port/);
  assert.throws(() => parseCli(["--max-sandboxes", "0"]), /--max-sandboxes/);
  assert.throws(() => parseCli(["--run-timeout", "-1"]), /--run-timeout/);
});
