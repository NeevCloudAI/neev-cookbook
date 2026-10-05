import assert from "node:assert/strict";
import { test } from "node:test";
import { missingEnv, parseCli, run } from "../app-builder.ts";
import { fakeModel, fakeSession, toolCall } from "./fakes.ts";

function readySandbox() {
  const sb: any = { name: "live-app-1", started: [] as unknown[], deleted: false };
  sb.waitUntilReady = async () => sb;
  sb.processes = { start: async (program: string, o: { args: string[] }) => { sb.started.push([program, ...o.args]); return { id: "p1" }; } };
  sb.getUrl = async ({ port }: { port: number }) => `https://${port}-preview.example`;
  sb.delete = async () => { sb.deleted = true; };
  return sb;
}

const neevWith = (sb: any) => { const created: any[] = []; return { created, sandboxes: { create: async (p: any) => { created.push(p); return sb; } } }; };

// connector returns a connect(sandboxName) that hands out the fake session and records names and closes.
function connector(session = fakeSession()) {
  const names: string[] = []; let closed = 0;
  const connect = async (name: string) => { names.push(name); return Object.assign(session, { close: async () => { closed++; } }); };
  return { connect, names, session, closed: () => closed };
}

const finishing = () => fakeModel([toolCall("fs_write", { path: "index.html", content: "<h1>x</h1>" }), toolCall("finish", { summary: "done" }, "c2")]);
const quiet = { log: () => {}, wait: async () => {} };

test("missingEnv names every missing variable", () => {
  assert.deepEqual(missingEnv({ NEEV_API_KEY: "k" }), ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]);
});

test("happy path connects MCP to the new sandbox, serves on 0.0.0.0 and deletes", async () => {
  const sb = readySandbox(); const neev = neevWith(sb); const c = connector(); const lines: string[] = [];
  const code = await run("a todo app", 0, neev as any, finishing(), "m", c.connect, { ...quiet, log: (s) => lines.push(s) });
  assert.equal(code, 0);
  assert.deepEqual(neev.created[0].egress, { mode: "deny_all" });
  assert.deepEqual(c.names, ["live-app-1"]);
  assert.equal(c.session.files.get("index.html"), "<h1>x</h1>");
  assert.equal(c.closed(), 1);
  assert.deepEqual(sb.started, [["python3", "-m", "http.server", "3000", "--bind", "0.0.0.0"]]);
  assert.ok(lines.some((l) => l.includes("https://3000-preview.example")));
  assert.ok(sb.deleted);
});

test("deletes the sandbox when the agent fails", async () => {
  const sb = readySandbox(); const c = connector();
  const model = fakeModel(Array.from({ length: 30 }, () => toolCall("fs_list", {})));
  assert.equal(await run("x", 0, neevWith(sb) as any, model, "m", c.connect, quiet), 1);
  assert.ok(sb.deleted);
  assert.equal(c.closed(), 1);
});

test("an interrupt during keep-alive deletes and still succeeds", async () => {
  const sb = readySandbox(); const ac = new AbortController();
  const wait = async () => { ac.abort(); throw new Error("aborted"); };
  assert.equal(await run("x", 10, neevWith(sb) as any, finishing(), "m", connector().connect, { log: () => {}, wait, signal: ac.signal }), 0);
  assert.ok(sb.deleted);
});

test("an interrupt during the build stops it, deletes, and exits 130", async () => {
  const sb = readySandbox(); const ac = new AbortController();
  const model = fakeModel(Array.from({ length: 30 }, () => toolCall("fs_list", {})));
  const log = (s: string) => { if (s.includes("step 2")) ac.abort(); };
  assert.equal(await run("x", 0, neevWith(sb) as any, model, "m", connector().connect, { log, wait: async () => {}, signal: ac.signal }), 130);
  assert.equal(model.requests.length, 2);
  assert.deepEqual(sb.started, []);
  assert.ok(sb.deleted);
});

test("an interrupt aborts an in-flight model call", async () => {
  const sb = readySandbox(); const ac = new AbortController();
  const model = { chat: { completions: { create: (_b: unknown, o?: { signal?: AbortSignal }) => new Promise<never>((_, reject) => {
    o?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
  }) } } };
  setTimeout(() => ac.abort(), 20);
  assert.equal(await run("x", 0, neevWith(sb) as any, model, "m", connector().connect, { ...quiet, signal: ac.signal }), 130);
  assert.ok(sb.deleted);
});

test("an unexpected error is one line and deletes", async () => {
  const sb = readySandbox(); const lines: string[] = [];
  const model = { chat: { completions: { create: async () => { throw new Error("401 invalid api key"); } } } };
  assert.equal(await run("x", 0, neevWith(sb) as any, model, "m", connector().connect, { ...quiet, log: (s) => lines.push(s) }), 1);
  assert.ok(sb.deleted);
  assert.ok(lines.some((l) => l.includes("401")));
});

test("runs get different sandbox names", async () => {
  const neev = neevWith(readySandbox());
  for (let i = 0; i < 2; i++) await run("x", 0, neev as any, finishing(), "m", connector().connect, quiet);
  assert.notEqual(neev.created[0].name, neev.created[1].name);
  assert.ok(neev.created.every((p: any) => p.name.startsWith("live-app-")));
});

test("parseCli reads the request and --keep in either form, and rejects a bad --keep", () => {
  assert.deepEqual(parseCli(["a timer", "--keep", "5"]), { request: "a timer", keep: 5 });
  assert.deepEqual(parseCli(["--keep=0", "a timer"]), { request: "a timer", keep: 0 });
  assert.deepEqual(parseCli([]), { request: "a todo app with a dark theme", keep: 10 });
  assert.throws(() => parseCli(["--keep", "abc"]), /--keep/);
});
