import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { SAMPLE_CSV, missingEnv, run, type Connect } from "../analyst.ts";
import { PNG, fakeModel, fakeNeev, fakeSandbox, fakeSession, toolCall } from "./fakes.ts";

const CHART_CODE = "plt.savefig('chart.png')";
const finishingModel = () => fakeModel([toolCall("run_python", { code: CHART_CODE }), toolCall("finish", { findings: "Mumbai leads." }, "c2")]);

// connector returns a Connect yielding the given fake session, recording names and closes.
function connector(session = fakeSession()) {
  const names: string[] = []; let closed = 0;
  const connect: Connect = async (name) => { names.push(name); return Object.assign(session, { close: async () => { closed++; } }); };
  return { connect, names, closed: () => closed };
}

const outDir = () => mkdtempSync(join(tmpdir(), "analyst-"));

// go runs the recipe on fakes, collecting the log lines.
async function go(sb: any, over: { model?: any; lines?: string[]; neev?: any; connect?: Connect; out?: string; signal?: AbortSignal } = {}) {
  const lines = over.lines ?? [];
  const out = over.out ?? join(outDir(), "chart.png");
  const code = await run("q", SAMPLE_CSV, out, over.neev ?? fakeNeev(sb), over.model ?? finishingModel(), "m",
    over.connect ?? connector().connect, { log: (l) => lines.push(l), signal: over.signal });
  return { code, out, lines };
}

test("missingEnv names every missing variable", () => {
  assert.deepEqual(missingEnv({ NEEV_API_KEY: "k" }), ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]);
});

test("the bundled sample is a few hundred rows of CSV", () => {
  const rows = readFileSync(SAMPLE_CSV, "utf8").trimEnd().split(/\r?\n/);
  assert.equal(rows[0], "month,city,category,orders,revenue_inr");
  assert.ok(rows.length > 200 && rows.length < 1000);
});

test("happy path: installs from PyPI only, locks egress, then downloads the chart", async () => {
  const sb = fakeSandbox(); const neev = fakeNeev(sb); const c = connector();
  const { code, out, lines } = await go(sb, { neev, connect: c.connect });
  assert.equal(code, 0, lines.join("\n"));
  assert.deepEqual(neev.created[0].allowEgress, ["pypi.org", "files.pythonhosted.org"]);
  assert.match(neev.created[0].name, /^data-analyst-js-[0-9a-f]{8}$/);
  assert.deepEqual(sb.execs[0].slice(0, 3), ["python3", "-m", "pip"]);
  assert.deepEqual(sb.execs[0].slice(-2), ["pandas", "matplotlib"]);
  assert.deepEqual(sb.updates, [{ egress: { mode: "deny_all" } }]);
  assert.deepEqual(sb.uploads, [[SAMPLE_CSV, "data.csv"]]);
  assert.deepEqual(c.names, ["data-analyst-1"]);
  assert.equal(c.closed(), 1);
  assert.deepEqual(new Uint8Array(readFileSync(out)), PNG);
  assert.ok(lines.some((l) => l.includes("Mumbai leads.")));
  assert.ok(sb.deleted);
});

test("egress is locked before the data is uploaded", async () => {
  const sb = fakeSandbox();
  assert.equal((await go(sb)).code, 0);
  assert.deepEqual(sb.order, ["install", "update", "upload"]);
});

test("egress is locked before the agent runs", async () => {
  const sb = fakeSandbox(); const seen: unknown[][] = [];
  const connect: Connect = async () => { seen.push([...sb.updates]); return Object.assign(fakeSession(), { close: async () => {} }); };
  await go(sb, { connect });
  assert.deepEqual(seen, [[{ egress: { mode: "deny_all" } }]]);
});

test("a failed install stops before the agent and deletes", async () => {
  const sb = fakeSandbox({ pipExit: 1 }); const model = finishingModel();
  const { code, lines } = await go(sb, { model });
  assert.equal(code, 1);
  assert.deepEqual(model.requests, []);
  assert.ok(sb.deleted);
  assert.ok(lines.some((l) => l.includes("no matching distribution")));
});

test("an agent failure deletes the sandbox and writes no chart", async () => {
  const sb = fakeSandbox();
  const model = fakeModel(Array.from({ length: 40 }, (_, i) => toolCall("fs_list", {}, `c${i}`)));
  const { code, out, lines } = await go(sb, { model });
  assert.equal(code, 1);
  assert.ok(sb.deleted && !existsSync(out));
  assert.ok(lines.some((l) => l.startsWith("The agent did not finish the analysis: step limit")));
});

test("a chart that is not a PNG fails and writes nothing", async () => {
  const sb = fakeSandbox({ chart: new TextEncoder().encode("<svg/>") });
  const { code, out, lines } = await go(sb);
  assert.equal(code, 1);
  assert.ok(sb.deleted && !existsSync(out));
  assert.ok(lines.some((l) => l.includes("not a PNG")));
});

test("Ctrl+C part-way deletes the sandbox and exits 130", async () => {
  const sb = fakeSandbox(); const ac = new AbortController();
  sb.exec = async () => { ac.abort(); throw new Error("aborted"); };
  assert.equal((await go(sb, { signal: ac.signal })).code, 130);
  assert.ok(sb.deleted);
});

test("an unexpected error is one line and deletes", async () => {
  const sb = fakeSandbox();
  const model = { chat: { completions: { create: async () => { throw new Error("401 invalid api key"); } } } };
  const { code, lines } = await go(sb, { model });
  assert.equal(code, 1);
  assert.ok(sb.deleted);
  assert.ok(lines.some((l) => l.startsWith("Failed: Error") && l.includes("401")));
});

test("runs started in the same second get different sandbox names", async () => {
  const neev = fakeNeev(fakeSandbox());
  await go(null, { neev }); await go(null, { neev });
  assert.notEqual(neev.created[0].name, neev.created[1].name);
});

test("the script exits 2 naming the missing variables before creating anything", () => {
  const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !k.startsWith("NEEV_")));
  const r = spawnSync(process.execPath, ["--import", "tsx", "analyst.ts"], { cwd: new URL("..", import.meta.url), env, encoding: "utf8" });
  assert.equal(r.status, 2);
  assert.match(r.stderr, /Missing environment variables: NEEV_API_KEY, NEEV_ORG_ID, NEEV_PROJECT_ID, NEEV_MODEL_API_KEY/);
});

test("the script exits 2 when the CSV does not exist", () => {
  const env = { ...process.env, NEEV_API_KEY: "x", NEEV_ORG_ID: "x", NEEV_PROJECT_ID: "x", NEEV_MODEL_API_KEY: "x" };
  const r = spawnSync(process.execPath, ["--import", "tsx", "analyst.ts", "--csv", "/no/such/file.csv"], { cwd: new URL("..", import.meta.url), env, encoding: "utf8" });
  assert.equal(r.status, 2);
  assert.match(r.stderr, /\/no\/such\/file\.csv/);
});
