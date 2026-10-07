// What did my agent do? Run a short agent task in a sandbox, then report it from the sandbox's audit trail.
import { randomBytes } from "node:crypto";
import { writeFileSync } from "node:fs";
import { setTimeout as delay } from "node:timers/promises";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { AgentFailed, runAgent, type ModelLike, type SessionLike } from "./agent.ts";
import { makeReport, printable, toMarkdown, toTerminal, type AuditRecord } from "./report.ts";

export const REQUIRED_ENV = ["NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"] as const;
const MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp";
const MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1";
const DEFAULT_MODEL = "glm-4-7";
const DEFAULT_TASK =
  "Set up the project in the workspace by following its README, then summarise its configuration: " +
  "which settings it uses, where each one comes from, and whether anything is missing.";
const PAGE_SIZE = 25;
const MAX_PAGES = 100; // a cap on one read, in case the server never stops handing out cursors
export const AUDIT_WAIT_MS = 30_000; // records usually appear within a second; this bounds the wait if they are slow
const AUDIT_POLL_MS = 1000;

// The small project the agent works on. The .env holds obviously fake values and only ever exists in the sandbox.
export const PROJECT: Record<string, string> = {
  "README.md":
    "# invoice-api\n\nA tiny invoice service.\n\n## Setup\n\n" +
    "1. Remove the stale lock file `cache/stale.lock` left by an old run.\n" +
    "2. Check the configuration with `python3 app.py --check`. Settings live in `settings.ini`; " +
    "secrets come from `.env`.\n" +
    "3. Write what you found to `SETUP_NOTES.md`.\n",
  "app.py":
    "import configparser, sys\n\n" +
    "def main():\n" +
    "    cfg = configparser.ConfigParser()\n" +
    "    cfg.read('settings.ini')\n" +
    "    env = dict(l.strip().split('=', 1) for l in open('.env') if '=' in l)\n" +
    "    needed = [cfg[s][k] for s in cfg.sections() for k in cfg[s] if k.endswith('_env')]\n" +
    "    missing = [n for n in needed if not env.get(n)]\n" +
    "    print('port', cfg['server']['port'], '| secrets needed:', ', '.join(needed))\n" +
    "    print('missing:', ', '.join(missing) or 'none')\n" +
    "    return 1 if missing else 0\n\n" +
    "if __name__ == '__main__':\n" +
    "    sys.exit(main())\n",
  "settings.ini":
    "[server]\nhost = 0.0.0.0\nport = 8080\n\n[database]\nurl_env = DATABASE_URL\n\n" +
    "[payments]\nprovider = dummypay\napi_key_env = PAYMENTS_API_KEY\n\n" +
    "[email]\nsmtp_host = smtp.example.com\npassword_env = SMTP_PASSWORD\n",
  ".env":
    "DATABASE_URL=postgres://demo:not-a-real-password@localhost:5432/invoices\n" +
    "PAYMENTS_API_KEY=dummy-key-0000-not-real\n",
  "cache/stale.lock": "pid=4242\n",
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
    const client = new Client({ name: "agent-session-report", version: "1.0.0" });
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

// uploads maps each written path to its earliest fs.write record time, to find where the script's setup ends.
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

// run seeds a sandbox, lets the agent work over MCP, writes the report from the audit trail, and always deletes.
// The trail is read before the sandbox is deleted, because it cannot be read afterwards.
export async function run(
  task: string, out: string, neev: NeevLike, modelClient: ModelLike, model: string, connect: Connect,
  opts: { log?: (s: string) => void; sleep?: (ms: number) => Promise<void>; clock?: () => number; signal?: AbortSignal; pageSize?: number } = {},
): Promise<number> {
  const { signal, pageSize = PAGE_SIZE } = opts;
  const sleep = opts.sleep ?? ((ms: number) => delay(ms, undefined, { signal }));
  const clock = opts.clock ?? (() => performance.now());
  const rawLog = opts.log ?? console.log;
  // Every line passes here, so model or sandbox text cannot drive the terminal; line breaks are kept.
  const log = (text: string) => rawLog(text.split("\n").map(printable).join("\n"));
  // waitForTrail re-reads the trail until enough(records) holds or AUDIT_WAIT_MS passes; records land a moment after the call.
  const waitForTrail = async (sandbox: Auditable, enough: (rs: AuditRecord[]) => boolean) => {
    const deadline = clock() + AUDIT_WAIT_MS;
    for (;;) {
      const trail = await readTrail(sandbox, pageSize);
      if (enough(trail.records) || clock() >= deadline) return trail;
      await sleep(AUDIT_POLL_MS);
    }
  };

  const name = `session-report-js-${randomBytes(4).toString("hex")}`;
  let sandbox: any;
  try {
    log("1. Creating a sandbox (no internet access)...");
    sandbox = await neev.sandboxes.create({ name, egress: { mode: "deny_all" } });
    await sandbox.waitUntilReady({ timeoutMs: 300_000 });
    const paths = Object.keys(PROJECT);
    log(`2. Seeding ${paths.length} project files, including a .env with dummy values...`);
    for (const path of paths) await sandbox.files.write(path, PROJECT[path]);
    const setup = await waitForTrail(sandbox, (rs) => paths.every((p) => uploads(rs).has(p)));
    const uploaded = uploads(setup.records);
    if (!paths.every((p) => uploaded.has(p))) {
      log(`The audit trail did not show the setup writes within ${AUDIT_WAIT_MS / 1000}s; try again shortly.`);
      return 1;
    }
    // Everything after the newest setup write is the agent's. Both sides are server timestamps.
    const boundary = paths.map((p) => uploaded.get(p)!).reduce((a, b) => (Date.parse(a) > Date.parse(b) ? a : b));
    log(`3. Asking ${model} to: ${task}`);
    const session = await connect(sandbox.name);
    let result: { summary: string; calls: number };
    try { result = await runAgent(session, modelClient, model, task, { log, signal }); } finally { await session.close(); }
    log(`4. Agent finished: ${result.summary}`);
    log("5. Reading the audit trail page by page...");
    const trail = await waitForTrail(sandbox, (rs) => rs.filter((r) => Date.parse(r.at) > Date.parse(boundary)).length >= result.calls);
    const report = makeReport(sandbox.name, trail.records, boundary, trail.retentionDays);
    if (report.agentActions === 0) {
      log(`The audit trail shows no agent actions after ${AUDIT_WAIT_MS / 1000}s; no report written.`);
      return 1;
    }
    log("");
    log(toTerminal(report));
    log("");
    writeFileSync(out, toMarkdown(report), "utf8");
    log(`6. Report written to ${out} (${report.agentActions} of ${result.calls} agent calls in the trail).`);
    return 0;
  } catch (e) {
    if (signal?.aborted) { log("Interrupted."); return 130; }
    if (e instanceof AgentFailed) { log(`The agent did not finish: ${e.message}`); return 1; }
    log(`Failed: ${(e as Error).name}: ${(e as Error).message}`); // e.g. a rejected model key: one line instead of a stack trace
    return 1;
  } finally {
    await cleanup(neev, sandbox, name, log);
  }
}

// cleanup deletes the sandbox, looking it up by name if create never returned (Ctrl+C, timeout).
// A failed delete is reported with the name instead of hiding the run's own result.
async function cleanup(neev: NeevLike, sandbox: any, name: string, log: (s: string) => void): Promise<void> {
  if (!sandbox) {
    try {
      sandbox = (await neev.sandboxes.list({ name, limit: 10 })).items.find((s) => s.name === name);
    } catch (e) { // we cannot tell whether the server made it, so say which name to look for
      log(`   Could not check for sandbox ${name} (${(e as Error).name}: ${(e as Error).message}); if it exists, delete it from the console.`);
      return;
    }
    if (!sandbox) return;
  }
  try {
    await sandbox.delete();
    log("   Sandbox deleted.");
  } catch (e) {
    log(`   Could not delete sandbox ${name} (${(e as Error).name}: ${(e as Error).message}); delete it from the console.`);
  }
}

// main parses arguments, checks the environment, and runs the recipe with Ctrl+C wired to cleanup.
async function main(): Promise<number> {
  let values: { out?: string }, positionals: string[];
  try {
    ({ values, positionals } = parseArgs({ args: process.argv.slice(2), allowPositionals: true, options: { out: { type: "string", default: "report.md" } } }));
  } catch (e) { console.error((e as Error).message); return 2; }
  const missing = missingEnv(process.env);
  if (missing.length) { console.error(`Missing environment variables: ${missing.join(", ")}. See README.md.`); return 2; }
  const { Neev } = await import("@neevcloud/sdk");
  const { default: OpenAI } = await import("openai");
  const ac = new AbortController();
  // Keep the handler for repeated presses, so a second Ctrl+C cannot skip the cleanup.
  process.on("SIGINT", () => ac.abort());
  const modelClient = new OpenAI({ baseURL: MODEL_BASE_URL, apiKey: process.env.NEEV_MODEL_API_KEY });
  return run(positionals[0] ?? DEFAULT_TASK, values.out!, new Neev() as unknown as NeevLike, modelClient as unknown as ModelLike,
    process.env.MODEL ?? DEFAULT_MODEL, mcpConnect(process.env.NEEV_API_KEY!), { signal: ac.signal });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().then((code) => process.exit(code), (e) => { console.error(`Failed: ${e.message}`); process.exit(1); });
}
