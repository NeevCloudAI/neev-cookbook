import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { test } from "node:test";
import { AUDIT_WAIT_MS, PROJECT, missingEnv, readTrail, run, type Connect } from "../review-gate.ts";
import { FakeSandbox, fakeModel, fakeSession, toolCall } from "./fakes.ts";

const VALIDATED = PROJECT["signup.py"].replace("def create_user(email, age):\n",
  "def create_user(email, age):\n    if '@' not in email:\n        raise ValueError('email')\n");

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

// agentModel reads .env, rewrites signup.py, runs the tests and finishes.
const agentModel = () => fakeModel([toolCall("fs_read", { path: ".env" }), toolCall("fs_write", { path: "signup.py", content: VALIDATED }, "c2"),
  toolCall("exec", { program: "python3", args: ["-m", "unittest"] }, "c3"), toolCall("finish", { summary: "Added email validation." }, "c4")]);

type Over = { model?: any; decision?: "approve" | "reject" | null; sb?: FakeSandbox; ask?: (p: string) => Promise<string>; neev?: any; time?: ReturnType<typeof fakeTime>; signal?: AbortSignal; pageSize?: number };

async function go(over: Over = {}) {
  const dir = mkdtempSync(join(tmpdir(), "gate-"));
  const sb = over.sb ?? new FakeSandbox();
  const time = over.time ?? fakeTime();
  const lines: string[] = [];
  const code = await run("task", join(dir, "review.md"), join(dir, "approved"), over.decision === undefined ? "approve" : over.decision,
    over.neev ?? fakeNeev(sb), over.model ?? agentModel(), "m", connector(fakeSession(sb)),
    { log: (l) => lines.push(l), ask: over.ask ?? (async () => assert.fail("asked although a decision was given")),
      sleep: time.sleep, clock: time.clock, signal: over.signal, pageSize: over.pageSize });
  const md = existsSync(join(dir, "review.md")) ? readFileSync(join(dir, "review.md"), "utf8") : "";
  const activity = md.split("## What the agent did")[1]?.split("## Flagged")[0] ?? "";
  return { code, sb, lines, dir, md, activity, time };
}

// files lists every file under a folder, relative to it.
const files = (root: string): string[] => readdirSync(root, { recursive: true, withFileTypes: true })
  .filter((d) => d.isFile()).map((d) => relative(root, join(d.parentPath, d.name)));

test("missingEnv names every missing variable", () => {
  assert.deepEqual(missingEnv({ NEEV_API_KEY: "k" }), ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]);
});

test("the fixture .env holds only obviously fake values", () => {
  assert.ok(PROJECT[".env"].includes("dummy") && PROJECT[".env"].includes("example.com"));
});

test("approve writes only the changed files and the packet, then deletes", async () => {
  const { code, sb, lines, dir, md } = await go({ decision: "approve" });
  assert.equal(code, 0, lines.join("\n")); assert.ok(sb.deleted);
  const out = join(dir, "approved", sb.name);
  assert.deepEqual(files(out), ["signup.py"]);
  assert.equal(readFileSync(join(out, "signup.py"), "utf8"), VALIDATED);
  assert.ok(md.includes("+    if '@' not in email:") && md.includes("sensitive read") && md.includes("Approved"));
  assert.ok(lines.some((l) => l.includes(out)));
});

test("reject writes nothing but the packet", async () => {
  const { code, sb, lines, dir, md } = await go({ decision: "reject" });
  assert.equal(code, 0); assert.ok(sb.deleted);
  assert.ok(!existsSync(join(dir, "approved")));
  assert.ok(md.includes("Rejected"));
  assert.ok(lines.some((l) => l.includes("nothing written")));
});

for (const [answer, approved] of [["y", true], ["YES", true], ["n", false], ["", false], ["maybe", false]] as const) {
  test(`an interactive answer of ${JSON.stringify(answer)} ${approved ? "approves" : "rejects"}`, async () => {
    const prompts: string[] = [];
    const { code, sb, dir } = await go({ decision: null, ask: async (p) => { prompts.push(p); return answer; } });
    assert.equal(code, 0);
    assert.deepEqual(prompts, ["Approve this change? [y/N] "]);
    assert.equal(existsSync(join(dir, "approved", sb.name, "signup.py")), approved);
  });
}

test("a prompt that fails, such as on a closed stdin, counts as reject", async () => {
  const { code, sb, dir } = await go({ decision: null, ask: async () => { throw new Error("readline was closed"); } });
  assert.equal(code, 0); assert.ok(sb.deleted && !existsSync(join(dir, "approved")));
});

test("Ctrl+C at the prompt deletes and writes nothing", async () => {
  const ac = new AbortController();
  const { code, sb, lines, dir } = await go({ decision: null, signal: ac.signal, ask: async () => { ac.abort(); throw new Error("aborted"); } });
  assert.equal(code, 130); assert.ok(sb.deleted && !existsSync(join(dir, "approved")));
  assert.ok(lines.includes("Interrupted."));
});

test("the packet shows the agent's trail, not the setup or the diff's own reads", async () => {
  const { code, activity } = await go();
  assert.equal(code, 0);
  assert.equal(activity.split("| +").length - 1, 3); // fs.read .env, fs.write signup.py, exec
  assert.ok(activity.includes("exec (program not recorded)") && !activity.includes("README.md"));
});

test("the trail is read after the agent and before the sandbox is deleted", async () => {
  const sb = new FakeSandbox(); const sizes: number[] = [];
  const realAudit = sb.audit.bind(sb); const realDelete = sb.delete.bind(sb);
  sb.audit = async (q) => { sizes.push(sb.trail.length); return realAudit(q); };
  sb.delete = async () => {
    // the agent's three calls landed after the five uploads, and a read saw them before delete
    assert.ok(Math.max(...sizes) >= Object.keys(PROJECT).length + 3, "deleted before reading the agent's trail");
    await realDelete();
  };
  const { code } = await go({ sb });
  assert.equal(code, 0); assert.ok(sb.deleted);
});

test("no change exits 1 without asking or writing", async () => {
  const model = fakeModel([toolCall("fs_list", {}), toolCall("finish", { summary: "nothing to do" }, "c2")]);
  const { code, sb, lines, dir } = await go({ model, decision: null });
  assert.equal(code, 1); assert.ok(sb.deleted);
  assert.ok(!existsSync(join(dir, "approved")) && !existsSync(join(dir, "review.md")));
  assert.ok(lines.some((l) => l.includes("changed no files")));
});

test("the caches the tests leave behind are not a change", async () => {
  const model = fakeModel([toolCall("fs_write", { path: "__pycache__/signup.cpython-312.pyc", content: "x" }), toolCall("finish", { summary: "ran tests" }, "c2")]);
  assert.equal((await go({ model })).code, 1);
});

test("a symlink the agent made is shown but never exported", async () => {
  const sb = new FakeSandbox(); sb.links.set("leak", "/etc/passwd");
  const { code, dir, md } = await go({ sb });
  assert.equal(code, 0); assert.ok(!existsSync(join(dir, "approved", sb.name, "leak")));
  assert.ok(md.includes("symlink to /etc/passwd"));
});

test("the sandbox is deny_all with a unique prefixed name", async () => {
  const names: string[] = [];
  for (let i = 0; i < 2; i++) {
    const sb = new FakeSandbox(); const neev = fakeNeev(sb);
    await go({ sb, neev, decision: "reject" });
    assert.deepEqual(neev.created[0].egress, { mode: "deny_all" });
    names.push(neev.created[0].name);
  }
  assert.notEqual(names[0], names[1]);
  assert.ok(names.every((n) => /^review-gate-js-[0-9a-f]{8}$/.test(n)));
});

test("the trail is read page by page", async () => {
  const { code, sb, activity } = await go({ pageSize: 2 });
  assert.equal(code, 0);
  assert.ok(sb.auditCalls.some((c) => c.cursor !== undefined));
  assert.equal(activity.split("| +").length - 1, 3);
});

test("it waits briefly for upload records still on their way", async () => {
  const { code, md } = await go({ sb: new FakeSandbox("review-gate-1", 3) });
  assert.equal(code, 0); assert.ok(md.includes("fs.write"));
});

test("it waits for agent records that land late", async () => {
  const sb = new FakeSandbox(); const realAudit = sb.audit.bind(sb);
  let late = 3;
  sb.audit = async (q) => {
    const page = await realAudit(q);
    if (sb.trail.length > Object.keys(PROJECT).length && late > 0) { // the agent's last record is late
      if (q?.cursor === undefined) late--;
      const last = sb.trail.at(-1);
      page.records = page.records.filter((r) => r !== last);
    }
    return page;
  };
  const time = fakeTime();
  const { code, md, activity } = await go({ sb, time });
  assert.equal(code, 0); assert.ok(time.t.now >= 2000); assert.ok(!md.includes("Warning"));
  assert.equal(activity.split("| +").length - 1, 3);
});

test("the setup boundary waits for every upload, not just a count", async () => {
  const sb = new FakeSandbox();
  for (let i = 0; i < Object.keys(PROJECT).length; i++) sb.record("exec", { command: "bootstrap" }); // unrelated records already in a fresh trail
  const realAudit = sb.audit.bind(sb); let early = 2;
  sb.audit = async (q) => {
    const page = await realAudit(q);
    if (early > 0) { // the uploads have not landed yet; only the bootstrap records show
      if (q?.cursor === undefined) early--;
      page.records = page.records.filter((r) => r.command === "bootstrap"); page.next_cursor = undefined;
    }
    return page;
  };
  const { code, activity } = await go({ sb });
  assert.equal(code, 0); assert.ok(!activity.includes("bootstrap"));
  assert.equal(activity.split("| +").length - 1, 3);
});

test("agent records that never appear still go to review, with a warning", async () => {
  const sb = new FakeSandbox(); const realAudit = sb.audit.bind(sb);
  const setup: unknown[] = [];
  sb.audit = async (q) => {
    const page = await realAudit(q);
    if (!setup.length) setup.push(...page.records); // the setup records are visible; the agent's never arrive
    page.records = page.records.filter((r) => setup.includes(r)); page.next_cursor = undefined;
    return page;
  };
  const time = fakeTime();
  const { code, md } = await go({ sb, decision: "reject", time });
  assert.equal(code, 0);
  assert.ok(time.t.now >= AUDIT_WAIT_MS && time.t.now <= AUDIT_WAIT_MS * 2 + 5000);
  assert.ok(md.includes("shows 0 of the agent's 3 calls"));
});

test("setup records that never appear fail without running the agent", async () => {
  const model = agentModel();
  const { code, sb, lines } = await go({ model, sb: new FakeSandbox("review-gate-1", 10_000) });
  assert.equal(code, 1); assert.ok(sb.deleted); assert.deepEqual(model.requests, []);
  assert.ok(lines.some((l) => l.includes("audit trail")));
});

test("an agent failure deletes the sandbox and writes nothing", async () => {
  const model = fakeModel(Array.from({ length: 30 }, (_, i) => toolCall("delete_sandbox", {}, `c${i}`)));
  const { code, sb, lines, dir } = await go({ model });
  assert.equal(code, 1); assert.ok(sb.deleted && !existsSync(join(dir, "review.md")));
  assert.ok(lines.some((l) => l.startsWith("The agent did not finish")));
});

test("an unexpected error is one line and deletes", async () => {
  const model = { chat: { completions: { create: async () => { throw new Error("MCP stream closed"); } } } };
  const { code, sb, lines } = await go({ model });
  assert.equal(code, 1); assert.ok(sb.deleted);
  assert.ok(lines.some((l) => l.startsWith("Failed: Error: MCP stream closed")));
});

test("a failed delete exits 1 and names the sandbox", async () => {
  const sb = new FakeSandbox(); sb.delete = async () => { throw new Error("reset"); };
  const { code, lines } = await go({ sb });
  assert.equal(code, 1); assert.ok(lines.some((l) => l.includes("Could not delete sandbox review-gate-")));
});

test("Ctrl+C during create still deletes a sandbox the server made", async () => {
  const sb = new FakeSandbox(); const neev = fakeNeev(sb); const ac = new AbortController();
  neev.sandboxes.create = async (params) => { sb.name = params.name; ac.abort(); throw new Error("aborted"); };
  const { code, lines } = await go({ sb, neev, signal: ac.signal });
  assert.equal(code, 130); assert.ok(sb.deleted); assert.ok(lines.includes("   Sandbox deleted."));
});

test("a failed lookup after an interrupted create names the sandbox", async () => {
  const sb = new FakeSandbox(); const neev = fakeNeev(sb); const ac = new AbortController();
  neev.sandboxes.create = async () => { ac.abort(); throw new Error("aborted"); };
  neev.sandboxes.list = async () => { throw new Error("reset"); };
  const { code, lines } = await go({ sb, neev, signal: ac.signal });
  assert.equal(code, 130); assert.ok(lines.some((l) => l.includes("review-gate-js-") && l.includes("console")));
});

test("reading the trail stops after the page cap", async () => {
  const calls: (string | undefined)[] = [];
  const endless = { audit: async (q: { cursor?: string }) => { calls.push(q.cursor); return { records: [], next_cursor: "again", retention_days: 30 }; } };
  assert.deepEqual(await readTrail(endless, 25, 4), { records: [], retentionDays: 30 });
  assert.equal(calls.length, 4);
});

test("nothing from the model or the sandbox reaches the terminal with control characters", async () => {
  const sb = new FakeSandbox(); sb.fs.set("evil\x1b[2J.py", "x"); // an unsafe name: skipped, and named in the approve output
  const model = fakeModel([toolCall("fs_write", { path: "signup.py", content: VALIDATED }), toolCall("finish", { summary: "done\x1b]0;owned\x07" }, "c2")]);
  const { code, lines } = await go({ model, sb });
  assert.equal(code, 0);
  assert.ok(!lines.some((l) => l.includes("\x1b") || l.includes("\x07")));
  assert.ok(lines.some((l) => l.includes("evil?[2J.py")));
});

test("the script exits 2 naming the missing variables before creating anything", () => {
  const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !k.startsWith("NEEV_")));
  const r = spawnSync(process.execPath, ["--import", "tsx", "review-gate.ts"], { cwd: new URL("..", import.meta.url), env, encoding: "utf8" });
  assert.equal(r.status, 2);
  assert.match(r.stderr, /Missing environment variables: NEEV_API_KEY, NEEV_ORG_ID, NEEV_PROJECT_ID, NEEV_MODEL_API_KEY/);
});

test("--approve and --reject cannot both be given", () => {
  const r = spawnSync(process.execPath, ["--import", "tsx", "review-gate.ts", "--approve", "--reject"], { cwd: new URL("..", import.meta.url), encoding: "utf8" });
  assert.equal(r.status, 2);
  assert.match(r.stderr, /--approve and --reject/);
});
