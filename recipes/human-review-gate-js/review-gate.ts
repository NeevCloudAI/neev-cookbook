// Human review gate: an agent changes code in a sandbox; you see its diff and its audit trail, then approve or reject.
import { randomBytes } from "node:crypto";
import { appendFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { AgentFailed, runAgent, type ModelLike, type SessionLike } from "./agent.ts";
import { MAX_ENTRIES, collectChanges, exportChanges, makeActivity, printable, toMarkdown, toTerminal, type AuditRecord, type Entry } from "./review.ts";

export const REQUIRED_ENV = ["NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"] as const;
const MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp";
const MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1";
const DEFAULT_MODEL = "glm-4-7";
const DEFAULT_TASK =
  "Add input validation to create_user in signup.py: raise ValueError when the email does not have " +
  "exactly one '@' with text on both sides, when its domain is in the BLOCKED_DOMAINS setting, or when " +
  "age is not an integer from 13 to 120. Add tests for each case to test_signup.py, then run the tests " +
  "with: python3 -m unittest -v";
const PAGE_SIZE = 25;
const MAX_PAGES = 100; // a cap on one read, in case the server never stops handing out cursors
export const AUDIT_WAIT_MS = 30_000; // records usually appear within a second; this bounds the wait if they are slow
const AUDIT_POLL_MS = 1000;

// The small repository the agent changes. The .env holds obviously fake values and only ever exists in the sandbox.
export const PROJECT: Record<string, string> = {
  "README.md":
    "# signup\n\nA tiny signup module.\n\n" +
    "- `signup.py` creates user records.\n" +
    "- `config.py` loads settings from `.env`.\n" +
    "- Run the tests with `python3 -m unittest -v`.\n",
  "config.py":
    "import os\n\n\n" +
    "def load(path=\".env\"):\n" +
    "    \"\"\"Reads KEY=VALUE lines from the dotenv file into a dict.\"\"\"\n" +
    "    values = {}\n" +
    "    if os.path.exists(path):\n" +
    "        for line in open(path):\n" +
    "            if \"=\" in line and not line.startswith(\"#\"):\n" +
    "                key, value = line.strip().split(\"=\", 1)\n" +
    "                values[key] = value\n" +
    "    return values\n",
  "signup.py":
    "import config\n\n\n" +
    "def create_user(email, age):\n" +
    "    \"\"\"Returns a new user record.\"\"\"\n" +
    "    settings = config.load()\n" +
    "    return {\"email\": email.strip().lower(), \"age\": age, \"welcome_from\": settings.get(\"WELCOME_FROM\", \"\")}\n",
  "test_signup.py":
    "import unittest\n\nfrom signup import create_user\n\n\n" +
    "class CreateUserTest(unittest.TestCase):\n" +
    "    def test_normalises_email(self):\n" +
    "        self.assertEqual(create_user(\"  Ada@Example.com \", 36)[\"email\"], \"ada@example.com\")\n\n\n" +
    "if __name__ == \"__main__\":\n" +
    "    unittest.main()\n",
  ".env": "WELCOME_FROM=hello@example.com\nBLOCKED_DOMAINS=mailinator.com,trashmail.example\nSMTP_PASSWORD=dummy-not-a-real-password\n",
};

export interface NeevLike {
  sandboxes: { create(params: Record<string, unknown>): Promise<any>; list(params: { name?: string; limit?: number }): Promise<{ items: any[] }> };
}

// Connect opens an MCP session bound to one sandbox.
export type Connect = (sandboxName: string) => Promise<SessionLike & { close(): Promise<void> }>;

// mcpConnect returns a Connect for the sandbox MCP server, authenticated with the sandbox API key.
export function mcpConnect(apiKey: string): Connect {
  return async (sandboxName) => {
    const { Client } = await import("@modelcontextprotocol/sdk/client/index.js");
    const { StreamableHTTPClientTransport } = await import("@modelcontextprotocol/sdk/client/streamableHttp.js");
    const client = new Client({ name: "human-review-gate", version: "1.0.0" });
    const headers = { Authorization: `Bearer ${apiKey}`, "x-sandbox-name": sandboxName };
    await client.connect(new StreamableHTTPClientTransport(new URL(MCP_URL), { requestInit: { headers } }));
    return client as unknown as SessionLike & { close(): Promise<void> };
  };
}

// missingEnv returns the required variables that are not set.
export function missingEnv(env: Record<string, string | undefined>): string[] {
  return REQUIRED_ENV.filter((name) => !env[name]);
}

type Auditable = { audit(q: { cursor?: string; limit?: number }): Promise<{ records: AuditRecord[]; next_cursor?: string; retention_days: number }> };

// readTrail reads the whole audit trail, following next_cursor page by page.
export async function readTrail(sandbox: Auditable, pageSize = PAGE_SIZE, maxPages = MAX_PAGES): Promise<{ records: AuditRecord[]; retentionDays: number }> {
  const records: AuditRecord[] = [];
  let cursor: string | undefined;
  let retentionDays = 0;
  for (let i = 0; i < maxPages; i++) {
    const page = await sandbox.audit({ cursor, limit: pageSize });
    records.push(...page.records);
    retentionDays = page.retention_days;
    cursor = page.next_cursor;
    if (!cursor) break;
  }
  return { records, retentionDays };
}

// uploads maps each written path to its earliest fs.write record time, to find where the script's upload ends.
function uploads(records: AuditRecord[]): Map<string, string> {
  const times = new Map<string, string>();
  for (const r of records) {
    if (r.tool === "fs.write" && r.target) {
      const seen = times.get(r.target);
      if (!seen || Date.parse(r.at) < Date.parse(seen)) times.set(r.target, r.at);
    }
  }
  return times;
}

// run lets the agent change the project in a sandbox, puts its diff beside its audit trail, and exports only on approval.
// The trail is read before the sandbox is deleted, because it cannot be read afterwards.
export async function run(
  task: string, reviewPath: string, outRoot: string, decision: "approve" | "reject" | null,
  neev: NeevLike, modelClient: ModelLike, model: string, connect: Connect,
  opts: { log?: (s: string) => void; ask?: (prompt: string) => Promise<string>; sleep?: (ms: number) => Promise<void>;
    clock?: () => number; signal?: AbortSignal; pageSize?: number } = {},
): Promise<number> {
  const { ask, signal, pageSize = PAGE_SIZE } = opts;
  const sleep = opts.sleep ?? ((ms: number) => delay(ms, undefined, { signal }));
  const clock = opts.clock ?? (() => performance.now());
  const rawLog = opts.log ?? console.log;
  // Every line passes here, so model or sandbox text cannot drive the terminal.
  const log = (line: string) => rawLog(printable(line));
  // waitForTrail re-reads the trail until enough(records) holds or AUDIT_WAIT_MS passes; records land a moment after the call.
  const waitForTrail = async (sandbox: Auditable, enough: (rs: AuditRecord[]) => boolean) => {
    const deadline = clock() + AUDIT_WAIT_MS;
    for (;;) {
      const trail = await readTrail(sandbox, pageSize);
      if (enough(trail.records) || clock() >= deadline) return trail;
      await sleep(AUDIT_POLL_MS);
    }
  };

  const name = `review-gate-js-${randomBytes(4).toString("hex")}`;
  let sandbox: any;
  let code = 1;
  try {
    log("1. Creating a sandbox (no internet access)...");
    sandbox = await neev.sandboxes.create({ name, egress: { mode: "deny_all" } });
    await sandbox.waitUntilReady({ timeoutMs: 300_000 });
    const paths = Object.keys(PROJECT);
    log(`2. Uploading the project (${paths.length} files, including a .env with dummy values)...`);
    for (const path of paths) await sandbox.files.write(path, PROJECT[path]);
    const setup = await waitForTrail(sandbox, (rs) => paths.every((p) => uploads(rs).has(p)));
    const uploaded = uploads(setup.records);
    if (!paths.every((p) => uploaded.has(p))) {
      log(`The audit trail did not show the upload within ${AUDIT_WAIT_MS / 1000}s; try again shortly.`);
      return code;
    }
    // Everything after the newest upload record is the agent's. Both sides are server timestamps.
    const boundary = paths.map((p) => uploaded.get(p)!).reduce((a, b) => (Date.parse(a) > Date.parse(b) ? a : b));
    log(`3. Asking ${model} to: ${task}`);
    const session = await connect(sandbox.name);
    let result: { summary: string; calls: number };
    try { result = await runAgent(session, modelClient, model, task, { log, signal }); } finally { await session.close(); }
    log(`4. Agent finished: ${result.summary}`);
    log("5. Reading the audit trail page by page...");
    // Read before diffing, so the script's own file reads stay out of the agent's activity.
    const trail = await waitForTrail(sandbox, (rs) => rs.filter((r) => Date.parse(r.at) > Date.parse(boundary)).length >= result.calls);
    log("6. Diffing the sandbox against the original files...");
    const entries: Entry[] = await sandbox.files.list(".", { recursive: true, maxCount: MAX_ENTRIES });
    if (entries.length >= MAX_ENTRIES) {
      log(`The workspace holds more than ${MAX_ENTRIES} entries; too many to review.`);
      return code;
    }
    const changes = await collectChanges(PROJECT, entries, (p) => sandbox.files.read(p));
    if (!changes.length) {
      log("The agent changed no files; nothing to review.");
      return code;
    }
    const packet = { sandbox: sandbox.name, model, task, summary: result.summary, changes,
      activity: makeActivity(trail.records, boundary), agentCalls: result.calls, retentionDays: trail.retentionDays };
    log("");
    log(toTerminal(packet));
    log("");
    writeFileSync(reviewPath, toMarkdown(packet), "utf8");
    log(`7. Review packet written to ${reviewPath}.`);
    const approved = await decide(decision, ask, signal, log);
    const how = decision ? `with --${decision}` : "by the reviewer";
    const stamp = new Date().toISOString().slice(0, 16).replace("T", " ") + " UTC";
    if (approved) {
      const out = join(outRoot, sandbox.name);
      const written = exportChanges(changes, out);
      log(`8. Approved ${how}. Wrote ${written.length} changed files to ${out}/`);
      for (const c of changes) if (c.status === "deleted" || c.status === "skipped") log(`   ${c.status} in the sandbox, not written: ${c.path}`);
    } else {
      log(`8. Rejected ${how}: nothing written. The change is discarded with the sandbox.`);
    }
    appendFileSync(reviewPath, `\n## Decision\n\n${approved ? "Approved" : "Rejected"} ${how}, ${stamp}.\n`, "utf8");
    code = 0;
  } catch (e) {
    if (signal?.aborted) { log("Interrupted."); code = 130; }
    else if (e instanceof AgentFailed) log(`The agent did not finish: ${e.message}`);
    // e.g. a rejected model key: one line instead of a stack trace.
    else log(`Failed: ${(e as Error).name}: ${(e as Error).message}`);
  } finally {
    // A sandbox left behind fails the run even when the gate itself completed.
    if (!(await cleanup(neev, sandbox, name, log))) code = Math.max(code, 1);
  }
  return code;
}

// decide returns true to approve: from --approve/--reject, else the reviewer's answer, where only y/yes approves.
async function decide(decision: "approve" | "reject" | null, ask: ((p: string) => Promise<string>) | undefined,
  signal: AbortSignal | undefined, log: (s: string) => void): Promise<boolean> {
  if (decision !== null) return decision === "approve";
  try {
    return ["y", "yes"].includes((await ask!("Approve this change? [y/N] ")).trim().toLowerCase());
  } catch (e) {
    if (signal?.aborted) throw e;
    log("   No answer (stdin is closed); rejecting."); // no terminal to answer from: the safe default is to reject
    return false;
  }
}

// cleanup deletes the sandbox, looking it up by name if create never returned; true when nothing is left behind.
async function cleanup(neev: NeevLike, sandbox: any, name: string, log: (s: string) => void): Promise<boolean> {
  if (!sandbox) {
    try {
      sandbox = (await neev.sandboxes.list({ name, limit: 10 })).items.find((s) => s.name === name);
    } catch (e) { // we cannot tell whether the server made it, so say which name to look for
      log(`   Could not check for sandbox ${name} (${(e as Error).name}: ${(e as Error).message}); if it exists, delete it from the console.`);
      return false;
    }
    if (!sandbox) return true;
  }
  try {
    await sandbox.delete();
    log("   Sandbox deleted.");
    return true;
  } catch (e) {
    log(`   Could not delete sandbox ${name} (${(e as Error).name}: ${(e as Error).message}); delete it from the console.`);
    return false;
  }
}

// main parses arguments, checks the environment, and runs the recipe with Ctrl+C wired to cleanup.
async function main(): Promise<number> {
  let values: { approve?: boolean; reject?: boolean; review?: string; out?: string }, positionals: string[];
  try {
    ({ values, positionals } = parseArgs({
      args: process.argv.slice(2), allowPositionals: true,
      options: { approve: { type: "boolean" }, reject: { type: "boolean" },
        review: { type: "string", default: "review.md" }, out: { type: "string", default: "approved" } },
    }));
  } catch (e) { console.error((e as Error).message); return 2; }
  if (values.approve && values.reject) { console.error("--approve and --reject cannot both be given."); return 2; }
  const missing = missingEnv(process.env);
  if (missing.length) { console.error(`Missing environment variables: ${missing.join(", ")}. See README.md.`); return 2; }
  const { Neev } = await import("@neevcloud/sdk");
  const { default: OpenAI } = await import("openai");
  const ac = new AbortController();
  // Keep the handler for repeated presses, so a second Ctrl+C cannot skip the cleanup.
  process.on("SIGINT", () => ac.abort());
  const decision = values.approve ? "approve" : values.reject ? "reject" : null;
  // Only an interactive run asks. readline swallows Ctrl+C, so route it to the same abort.
  const rl = decision === null ? (await import("node:readline/promises")).createInterface({ input: process.stdin, output: process.stdout }) : undefined;
  rl?.on("SIGINT", () => ac.abort());
  const ask = rl ? (prompt: string) => rl.question(prompt, { signal: ac.signal }) : undefined;
  const modelClient = new OpenAI({ baseURL: MODEL_BASE_URL, apiKey: process.env.NEEV_MODEL_API_KEY });
  try {
    return await run(positionals[0] ?? DEFAULT_TASK, values.review!, values.out!, decision, new Neev() as unknown as NeevLike,
      modelClient as unknown as ModelLike, process.env.MODEL ?? DEFAULT_MODEL, mcpConnect(process.env.NEEV_API_KEY!), { signal: ac.signal, ask });
  } finally {
    rl?.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().then((code) => process.exit(code), (e) => { console.error(`Failed: ${e.message}`); process.exit(1); });
}
