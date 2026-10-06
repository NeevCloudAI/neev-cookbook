import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { test } from "node:test";
import { dataIntact, fetchStats, missingEnv, run, type Connect } from "../undo-mistake.ts";
import { FakeSandbox, fakeModel, fakeNeev, fakeSession, hangingModel, shell, text, toolCall } from "./fakes.ts";

// connector returns a connect(sandboxName) yielding an MCP session on the fake sandbox, recording names and closes.
function connector(sandbox: FakeSandbox) {
  const names: string[] = []; let closed = 0;
  const connect: Connect = async (name) => { names.push(name); return Object.assign(fakeSession(sandbox), { close: async () => { closed++; } }); };
  return { connect, names, closed: () => closed };
}

const destructiveModel = () => fakeModel([shell("du -sh *"), shell("rm -rf data", "c2"), toolCall("finish", { summary: "removed data/" }, "c3")]);
const carefulModel = () => fakeModel([toolCall("fs_list", {}), text("Everything here looks needed; I left it alone.")]);

// fakeTime returns (wait, clock) where waiting advances the clock, so waits finish instantly.
function fakeTime() {
  let now = 0;
  return { wait: async (ms: number) => { now += ms; }, clock: () => now };
}

type Opts = Parameters<typeof run>[4];

// go runs the recipe on a fake sandbox with fake time, collecting the log lines.
async function go(sb: FakeSandbox, model: any, lines: string[] = [], over: { neev?: any; connect?: Connect } & Opts = {}) {
  const { neev = fakeNeev(sb), connect = connector(sb).connect, ...opts } = over;
  return run(neev, model, "m", connect, { log: (l) => lines.push(l), fetch: sb.stats, ...fakeTime(), ...opts });
}

test("missingEnv names every missing variable", () => {
  assert.deepEqual(missingEnv({ NEEV_API_KEY: "k" }), ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]);
});

test("the script exits 2 naming the missing variables before creating anything", () => {
  const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !k.startsWith("NEEV_")));
  const r = spawnSync(process.execPath, ["--import", "tsx", "undo-mistake.ts"], { cwd: new URL("..", import.meta.url), env, encoding: "utf8" });
  assert.equal(r.status, 2);
  assert.match(r.stderr, /Missing environment variables: NEEV_API_KEY, NEEV_ORG_ID, NEEV_PROJECT_ID, NEEV_MODEL_API_KEY/);
});

test("happy path: the agent deletes the data and the rollback brings everything back", async () => {
  const sb = new FakeSandbox(); const lines: string[] = [];
  const neev = fakeNeev(sb); const c = connector(sb);
  assert.equal(await go(sb, destructiveModel(), lines, { neev, connect: c.connect }), 0);
  assert.deepEqual(neev.created[0].egress, { mode: "deny_all" });
  assert.match(neev.created[0].name, /^undo-mistake-js-[0-9a-f]{8}$/);
  assert.deepEqual(c.names, [sb.name]);
  assert.equal(c.closed(), 1);
  assert.ok(sb.workspace.get("server.py")!.startsWith('"""A tiny shop API'));
  assert.deepEqual(sb.started, ["python3", "server.py"]);
  assert.equal(sb.snapshotName, `${sb.name}-before-cleanup`);
  assert.deepEqual(sb.rollbacks, ["snap-1"]);
  assert.ok(sb.deleted);
  const out = lines.join("\n");
  assert.ok(out.includes("The agent did the damage itself"));
  assert.ok(!out.includes("scripted mistake"));
  assert.ok(out.includes("data/customers.csv: GONE"));
  assert.ok(out.includes("-> 500")); // the damage is visible before the rollback
  assert.ok(out.includes("files: customers.csv read back with 50 rows"));
  assert.ok(out.includes("Restore proven"));
  // the in-memory request count rewinds to the snapshot moment, which a restarted server could not do
  assert.equal(sb.server!.served, 2);
});

test("a careful agent gets the scripted mistake so the rollback still shows", async () => {
  const sb = new FakeSandbox(); const lines: string[] = [];
  assert.equal(await go(sb, carefulModel(), lines), 0);
  const out = lines.join("\n");
  assert.ok(out.includes("The agent left the data alone"));
  assert.ok(sb.execs.some((c) => c.join(" ") === "sh -c rm -rf data"));
  assert.ok(out.includes("Restore proven") && sb.deleted);
});

test("an agent that kills the server is undone too", async () => {
  const sb = new FakeSandbox(); const lines: string[] = [];
  const model = fakeModel([shell("rm -rf data && pkill -f server.py"), toolCall("finish", { summary: "done" }, "c2")]);
  assert.equal(await go(sb, model, lines), 0);
  assert.ok(lines.some((l) => l.includes("not answering")));
  assert.deepEqual(sb.server, { pid: 12, served: 2 });
});

test("a failed snapshot stops before the agent runs and deletes", async () => {
  const sb = new FakeSandbox(undefined, { snapshotStatuses: ["Pending", "Failed"] }); const lines: string[] = [];
  const model = destructiveModel();
  assert.equal(await go(sb, model, lines), 1);
  assert.equal(model.requests.length, 0);
  assert.ok(sb.deleted);
  assert.ok(lines.some((l) => l.includes("Failed") && l.includes("disk full")));
});

test("a snapshot that never becomes Ready times out", async () => {
  const sb = new FakeSandbox(undefined, { snapshotStatuses: ["Running"] }); const lines: string[] = [];
  assert.equal(await go(sb, destructiveModel(), lines), 1);
  assert.ok(sb.deleted && lines.some((l) => l.includes("did not become Ready")));
});

test("a rollback that does not restore fails the run", async () => {
  const sb = new FakeSandbox(undefined, { rollbackRestores: false }); const lines: string[] = [];
  assert.equal(await go(sb, destructiveModel(), lines), 1);
  assert.ok(sb.deleted);
  assert.ok(lines.some((l) => l.includes("FAILED")) && !lines.some((l) => l.includes("Restore proven")));
});

test("a restarted server is not accepted as a restore", async () => {
  const sb = new FakeSandbox(); const lines: string[] = [];
  const original = sb.rollback.bind(sb);
  sb.rollback = async (id: string) => {
    await original(id);
    sb.server = { pid: 12, served: 0 }; // files back, but the process started fresh
    return sb;
  };
  assert.equal(await go(sb, destructiveModel(), lines), 1);
  assert.ok(lines.some((l) => l.includes("FAILED") && l.includes("memory")));
});

test("Ctrl+C while the agent works stops it, deletes, and exits 130", async () => {
  const sb = new FakeSandbox(); const ac = new AbortController();
  const model = fakeModel(Array.from({ length: 12 }, () => toolCall("fs_list", {})));
  const log = (l: string) => { if (l.includes("step 2")) ac.abort(); };
  assert.equal(await go(sb, model, [], { log, signal: ac.signal }), 130);
  assert.equal(model.requests.length, 2);
  assert.deepEqual(sb.rollbacks, []);
  assert.ok(sb.deleted);
});

test("Ctrl+C aborts an in-flight model call at once", async () => {
  const sb = new FakeSandbox(); const ac = new AbortController();
  setTimeout(() => ac.abort(), 20);
  const t = Date.now();
  assert.equal(await go(sb, hangingModel, [], { signal: ac.signal }), 130);
  assert.ok(Date.now() - t < 5000);
  assert.ok(sb.deleted);
});

test("Ctrl+C during an SDK wait that takes no signal stops the run and deletes", async () => {
  const sb = new FakeSandbox(); const ac = new AbortController();
  sb.waitUntilReady = () => new Promise<never>(() => {}); // a sandbox that never becomes Ready
  setTimeout(() => ac.abort(), 20);
  assert.equal(await go(sb, destructiveModel(), [], { signal: ac.signal }), 130);
  assert.ok(sb.deleted);
});

test("Ctrl+C before the snapshot exits 130 without running the agent", async () => {
  const sb = new FakeSandbox(); const ac = new AbortController(); const model = destructiveModel();
  const log = (l: string) => { if (l.startsWith("2.")) ac.abort(); };
  assert.equal(await go(sb, model, [], { log, signal: ac.signal }), 130);
  assert.equal(model.requests.length, 0);
  assert.ok(sb.deleted);
});

test("an unexpected error is one line with the cause and deletes", async () => {
  const sb = new FakeSandbox(); const lines: string[] = [];
  class AuthenticationError extends Error {} // like the model client's errors, it does not set name
  const model = { chat: { completions: { create: async () => { throw new AuthenticationError("401 invalid api key"); } } } };
  assert.equal(await go(sb, model, lines), 1);
  assert.ok(sb.deleted && lines.some((l) => l === "Failed: AuthenticationError: 401 invalid api key"));
});

test("a seed failure stops and deletes", async () => {
  const sb = new FakeSandbox(); const lines: string[] = [];
  sb.exec = async () => ({ stdout: "", stderr: "no python3", exitCode: 127 });
  assert.equal(await go(sb, destructiveModel(), lines), 1);
  assert.ok(sb.deleted && lines.some((l) => l.includes("no python3")));
});

test("runs get different sandbox names", async () => {
  const names: string[] = [];
  for (let i = 0; i < 2; i++) {
    const sb = new FakeSandbox(); const neev = fakeNeev(sb);
    await go(sb, carefulModel(), [], { neev });
    names.push(neev.created[0].name);
  }
  assert.notEqual(names[0], names[1]);
});

test("dataIntact needs a 200 with the original counts", () => {
  const baseline = { pid: 12, served: 2, customers: 50, orders: 120 };
  assert.equal(dataIntact(200, { pid: 12, served: 3, customers: 50, orders: 120 }, baseline), true);
  assert.equal(dataIntact(500, { pid: 12, served: 3, error: "gone" }, baseline), false);
  assert.equal(dataIntact(null, { error: "unreachable" }, baseline), false);
  assert.equal(dataIntact(200, { pid: 12, served: 3, customers: 50, orders: 0 }, baseline), false);
});

test("gateway errors while the server comes up are retried without counting requests", async () => {
  const sb = new FakeSandbox(); const lines: string[] = []; const seen = new Set<number>();
  // the first answer after the start and after the rollback comes from the gateway, not the server
  const flaky = async (url: string): Promise<[number | null, Record<string, any>]> => {
    const moment = sb.rollbacks.length;
    if (!seen.has(moment)) { seen.add(moment); return [502, { error: "bad gateway" }]; }
    return sb.stats(url);
  };
  assert.equal(await go(sb, destructiveModel(), lines, { fetch: flaky }), 0);
  assert.deepEqual([...seen], [0, 1]);
  assert.ok(lines.join("\n").includes("Restore proven"));
});

test("a failing delete is one line and keeps the exit code", async () => {
  const sb = new FakeSandbox(); const lines: string[] = [];
  sb.delete = async () => { throw new Error("network down"); };
  assert.equal(await go(sb, destructiveModel(), lines), 0);
  assert.ok(lines.some((l) => l.includes("Could not delete sandbox undo-mistake-js-1") && l.includes("network down")));
});

test("a scripted mistake that changes nothing fails the run", async () => {
  const sb = new FakeSandbox(); const lines: string[] = [];
  sb.shell = () => ({ stdout: "", stderr: "", exitCode: 0 });
  assert.equal(await go(sb, carefulModel(), lines), 1);
  assert.deepEqual(sb.rollbacks, []);
  assert.ok(sb.deleted && lines.some((l) => l.includes("still has its data")));
});

test("a gateway blip after the agent is not mistaken for damage", async () => {
  const sb = new FakeSandbox(); const lines: string[] = []; let calls = 0;
  // the first check after a careful agent hits a transient gateway error
  const blip = async (url: string): Promise<[number | null, Record<string, any>]> => (++calls === 2 ? [502, { error: "bad gateway" }] : sb.stats(url));
  assert.equal(await go(sb, carefulModel(), lines, { fetch: blip }), 0);
  const out = lines.join("\n");
  assert.ok(out.includes("The agent left the data alone") && !out.includes("did the damage itself"));
});

test("fetchStats reads JSON, keeps a non-JSON body as the error, and reports an unreachable app", async () => {
  const server = createServer((req, res) => {
    if (req.url === "/ok/stats") { res.writeHead(200, { "Content-Type": "application/json" }); res.end('{"customers": 50}'); return; }
    res.writeHead(502); res.end("Bad Gateway");
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  try {
    assert.deepEqual(await fetchStats(`${base}/ok`), [200, { customers: 50 }]);
    assert.deepEqual(await fetchStats(`${base}/down`), [502, { error: "Bad Gateway" }]);
  } finally { server.close(); }
  const [status, body] = await fetchStats(base);
  assert.equal(status, null);
  assert.match(body.error, /^unreachable \(/);
});

test("interactive: you see the broken app, then the restored one, and your first visit is rolled back", async () => {
  const sb = new FakeSandbox(); const lines: string[] = []; const prompts: string[] = [];
  const ask = async (p: string) => {
    prompts.push(p);
    if (prompts.length === 1) await sb.stats("https://8000-preview.example"); // you open the broken app
    return "";
  };
  assert.equal(await go(sb, destructiveModel(), lines, { ask }), 0, lines.join("\n"));
  assert.equal(prompts.length, 2);
  const out = lines.join("\n");
  assert.ok(out.indexOf("broken") < out.indexOf("Rolling back"));
  assert.ok(out.indexOf("Restore proven") < out.lastIndexOf("/stats"));
});

test("interactive: no second pause when the restore fails", async () => {
  const sb = new FakeSandbox(undefined, { rollbackRestores: false }); const prompts: string[] = [];
  assert.equal(await go(sb, destructiveModel(), [], { ask: async (p: string) => { prompts.push(p); return ""; } }), 1);
  assert.equal(prompts.length, 1);
});

test("interactive: a prompt that fails, such as on a closed stdin, carries on", async () => {
  const sb = new FakeSandbox(); const lines: string[] = [];
  const ask = async () => { throw new Error("readline was closed"); };
  assert.equal(await go(sb, destructiveModel(), lines, { ask }), 0);
  assert.ok(lines.some((l) => l.includes("Restore proven")));
});

test("interactive: Ctrl+C at a prompt deletes the sandbox and exits 130", async () => {
  const sb = new FakeSandbox(); const ac = new AbortController();
  const ask = async () => { ac.abort(); throw new Error("aborted"); };
  assert.equal(await go(sb, destructiveModel(), [], { ask, signal: ac.signal }), 130);
  assert.ok(sb.deleted);
});
