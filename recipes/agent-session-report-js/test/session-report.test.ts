import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { AUDIT_WAIT_MS, PROJECT, REQUIRED_ENV, missingEnv, readTrail, run, type Connect } from "../session-report.ts";
import { FakeSandbox, fakeModel, fakeSession, text, toolCall } from "./fakes.ts";

const HERE = fileURLToPath(new URL(".", import.meta.url));

// fakeNeev mimics neev.sandboxes.create (and list, for the lookup after an interrupted create).
function fakeNeev(sb: FakeSandbox) {
  const created: Record<string, any>[] = [];
  return { created, sandboxes: {
    async create(params: Record<string, any>) { created.push(params); return sb; },
    async list(q: { name?: string }) { return { items: q.name === sb.name ? [sb] : [] }; },
  } };
}

const connector = (session: ReturnType<typeof fakeSession>): Connect => async () => Object.assign(session, { close: async () => {} });

// fakeTime returns (sleep, clock) where sleeping advances the clock, so waits finish instantly.
function fakeTime() {
  const t = { now: 0 };
  return { t, sleep: async (ms: number) => { t.now += ms; }, clock: () => t.now };
}

// agentModel reads the README and .env, runs the app and finishes.
const agentModel = () => fakeModel([toolCall("fs_read", { path: "README.md" }), toolCall("fs_read", { path: ".env" }, "c2"),
  toolCall("exec", { program: "python3", args: ["app.py", "--check"] }, "c3"), toolCall("finish", { summary: "port 8080; SMTP_PASSWORD missing" }, "c4")]);

type Over = { model?: any; sb?: FakeSandbox; neev?: any; time?: ReturnType<typeof fakeTime>; signal?: AbortSignal; pageSize?: number };

async function go(over: Over = {}) {
  const out = join(mkdtempSync(join(tmpdir(), "report-")), "report.md");
  const sb = over.sb ?? new FakeSandbox();
  const time = over.time ?? fakeTime();
  const lines: string[] = [];
  const code = await run("task", out, over.neev ?? fakeNeev(sb), over.model ?? agentModel(), "m", connector(fakeSession(sb)),
    { log: (l) => lines.push(l), sleep: time.sleep, clock: time.clock, signal: over.signal, pageSize: over.pageSize });
  const md = existsSync(out) ? readFileSync(out, "utf8") : "";
  return { code, sb, lines, md, out, time };
}

const count = (md: string, phase: string) => md.split(`| ${phase} |`).length - 1;

test("missingEnv names every missing variable", () => {
  assert.deepEqual(missingEnv({ NEEV_API_KEY: "k" }), ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]);
});

test("the project's .env holds only obviously fake values", () => {
  assert.ok(PROJECT[".env"].includes("not-a-real") && PROJECT[".env"].includes("dummy"));
});

test("the happy path writes a report of the agent's actions and deletes the sandbox", async () => {
  const { code, sb, lines, md } = await go();
  assert.equal(code, 0, lines.join("\n"));
  assert.ok(sb.deleted);
  assert.ok(md.includes("sensitive read") && md.includes(".env"));
  assert.ok(md.includes("exec (program not recorded)"));
  assert.equal(count(md, "setup"), Object.keys(PROJECT).length);
  assert.equal(count(md, "agent"), 3);
  assert.ok(lines.some((l) => l.includes("Report written")));
  // the terminal report keeps its line breaks; only a single record's fields lose theirs
  assert.ok(lines.some((l) => l.includes("\n\nTimeline (oldest first):\n")), lines.join("\n"));
});

test("the sandbox is deny_all with a unique prefixed name", async () => {
  const names: string[] = [];
  for (let i = 0; i < 2; i++) {
    const sb = new FakeSandbox();
    const neev = fakeNeev(sb);
    await go({ sb, neev });
    assert.deepEqual(neev.created[0].egress, { mode: "deny_all" });
    names.push(neev.created[0].name);
  }
  assert.notEqual(names[0], names[1]);
  assert.ok(names.every((n) => /^session-report-js-[0-9a-f]{8}$/.test(n)));
});

test("the trail is read page by page", async () => {
  const { code, sb, md } = await go({ pageSize: 3 });
  assert.equal(code, 0); // every page reached the report, not just the first one
  assert.equal(count(md, "setup"), Object.keys(PROJECT).length);
  assert.equal(count(md, "agent"), 3);
  assert.ok(sb.auditCalls.some((c) => c.cursor !== undefined)); // more records than one page holds
  assert.ok(sb.auditCalls.every((c) => c.limit === 3));
});

test("it waits briefly for records still on their way", async () => {
  const { code, md } = await go({ sb: new FakeSandbox("session-report-1", 3) });
  assert.equal(code, 0);
  assert.equal(count(md, "agent"), 3);
});

test("setup records that never appear fail without running the agent", async () => {
  const model = agentModel();
  const { code, sb, lines, md } = await go({ sb: new FakeSandbox("session-report-1", 10_000), model });
  assert.equal(code, 1); assert.ok(sb.deleted); assert.equal(md, "");
  assert.deepEqual(model.requests, []);
  assert.ok(lines.some((l) => l.includes("audit trail")));
});

test("the setup boundary waits for every upload, not just a count", async () => {
  const sb = new FakeSandbox();
  // An unrelated record is already there, so a count of records would be met one upload early.
  sb.record("fs.list", { target: "." });
  const real = sb.audit.bind(sb);
  let polls = 0;
  sb.audit = async (q) => {
    const page = await real(q);
    if (q?.cursor === undefined) polls++;
    // the .env upload lands two polls late
    if (polls <= 2) page.records = page.records.filter((r) => r.target !== ".env" || r.tool !== "fs.write");
    return page;
  };
  const { code, md } = await go({ sb });
  assert.equal(code, 0);
  assert.equal(count(md, "setup"), Object.keys(PROJECT).length + 1);
  assert.equal(count(md, "agent"), 3);
});

test("agent records that never appear fail after the bound", async () => {
  const sb = new FakeSandbox();
  const real = sb.audit.bind(sb);
  let seen: string[] | undefined;
  sb.audit = async (q) => {
    const page = await real(q);
    seen ??= sb.trail.map((r) => r.id); // the setup records are visible; after that, nothing new ever arrives
    page.records = page.records.filter((r) => seen!.includes(r.id));
    return page;
  };
  const time = fakeTime();
  const { code, lines } = await go({ sb, time });
  assert.equal(code, 1); assert.ok(sb.deleted);
  assert.ok(time.t.now >= AUDIT_WAIT_MS && time.t.now <= AUDIT_WAIT_MS * 2 + 5000);
  assert.ok(lines.some((l) => l.includes("no agent actions")));
});

test("it waits for every agent call before writing the report", async () => {
  const sb = new FakeSandbox();
  const real = sb.audit.bind(sb);
  let late = 3;
  let setupDone = false;
  sb.audit = async (q) => {
    const page = await real(q);
    if (!setupDone) { setupDone = true; return page; }
    if (sb.trail.length > Object.keys(PROJECT).length && late > 0) {
      // the agent's last call lands a few polls late
      if (q?.cursor === undefined) late--;
      page.records = page.records.filter((r) => r !== sb.trail.at(-1));
    }
    return page;
  };
  const time = fakeTime();
  const { code, md } = await go({ sb, time });
  assert.equal(code, 0);
  assert.equal(count(md, "agent"), 3);
  assert.ok(time.t.now >= 2000);
});

test("an agent failure deletes the sandbox and writes no report", async () => {
  const { code, sb, lines, md } = await go({ model: fakeModel(Array.from({ length: 30 }, () => toolCall("delete_sandbox", {}))) });
  assert.equal(code, 1); assert.ok(sb.deleted); assert.equal(md, "");
  assert.ok(lines.some((l) => l.startsWith("The agent did not finish")));
});

test("Ctrl+C deletes the sandbox and exits 130", async () => {
  const ac = new AbortController();
  const model = fakeModel([]);
  model.chat.completions.create = async () => { ac.abort(); throw new DOMException("aborted", "AbortError"); };
  const { code, sb, lines, md } = await go({ model, signal: ac.signal });
  assert.equal(code, 130); assert.ok(sb.deleted); assert.equal(md, "");
  assert.ok(lines.includes("Interrupted."));
});

test("an unexpected error is one line and deletes", async () => {
  const model = fakeModel([]);
  model.chat.completions.create = async () => { throw new TypeError("fetch failed"); };
  const { code, sb, lines } = await go({ model });
  assert.equal(code, 1); assert.ok(sb.deleted);
  assert.ok(lines.includes("Failed: TypeError: fetch failed"));
});

test("Ctrl+C during create still deletes a sandbox the server made", async () => {
  const sb = new FakeSandbox();
  const ac = new AbortController();
  const neev = fakeNeev(sb);
  neev.sandboxes.create = async (params) => { sb.name = params.name; ac.abort(); throw new DOMException("aborted", "AbortError"); };
  const { code, lines } = await go({ sb, neev, signal: ac.signal });
  assert.equal(code, 130); assert.ok(sb.deleted);
  assert.ok(lines.includes("   Sandbox deleted."));
});

test("a failed lookup after an interrupted create names the sandbox", async () => {
  const sb = new FakeSandbox();
  const neev = fakeNeev(sb);
  neev.sandboxes.create = async () => { throw new Error("timed out"); };
  neev.sandboxes.list = async () => { throw new Error("HTTP 503"); };
  const { code, lines } = await go({ sb, neev });
  assert.equal(code, 1);
  assert.ok(lines.some((l) => l.includes("Could not check for sandbox session-report-js-") && l.includes("HTTP 503")));
});

test("a failed delete keeps the result and names the sandbox", async () => {
  const sb = new FakeSandbox();
  sb.delete = async () => { throw new Error("connection reset"); };
  const { code, lines, md } = await go({ sb });
  assert.equal(code, 0); assert.notEqual(md, "");
  assert.ok(lines.some((l) => l.includes("Could not delete sandbox session-report-") && l.includes("connection reset")));
});

test("reading the trail stops after the page cap", async () => {
  let calls = 0;
  const endless = { async audit() { calls++; return { records: [], next_cursor: "again", retention_days: 30 }; } };
  assert.deepEqual(await readTrail(endless, 25, 4), { records: [], retentionDays: 30 });
  assert.equal(calls, 4);
});

test("nothing from the model or the sandbox reaches the terminal with control characters", async () => {
  const model = fakeModel([toolCall("fs_read", { path: "README.md" }), text("done\x1b[2J\x1b]0;owned\x07 ‮evil")]);
  const { code, lines } = await go({ model });
  assert.equal(code, 0);
  const out = lines.join("\n");
  assert.ok(out.includes("done?[2J?]0;owned? ?evil"), out);
  assert.doesNotMatch(out, /[\x1b\x07‮]/);
});

test("the script exits 2 naming the missing variables before creating anything", () => {
  const env = { ...process.env };
  for (const name of REQUIRED_ENV) delete env[name];
  const r = spawnSync(process.execPath, ["--import", "tsx", "session-report.ts"], { cwd: join(HERE, ".."), env, encoding: "utf8" });
  assert.equal(r.status, 2);
  assert.match(r.stderr, /NEEV_API_KEY/);
});
