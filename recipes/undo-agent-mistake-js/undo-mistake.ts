// Undo the agent's mistake: snapshot a running app, let an agent wreck it, roll back, and prove it all came back.
import { randomBytes } from "node:crypto";
import { readFile } from "node:fs/promises";
import { setTimeout as sleep } from "node:timers/promises";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { cleanUp, type ModelLike, type SessionLike } from "./agent.ts";

export const REQUIRED_ENV = ["NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"] as const;
const MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp";
const MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1";
const DEFAULT_MODEL = "glm-4-7";
const PORT = 8000;
const APP_DIR = new URL("./app/", import.meta.url);
const DATA_FILES = ["data/customers.csv", "data/shop.db"] as const;
const TASK = "Clean up the workspace to save space.";
const SCRIPTED_MISTAKE = "rm -rf data";
const SNAPSHOT_TIMEOUT_MS = 300_000;
const APP_TIMEOUT_MS = 60_000;
const DESCRIPTION = "Undo the agent's mistake: snapshot a running app, let an agent wreck it, roll back, and prove it all came back.";

export interface NeevLike {
  sandboxes: { create(params: Record<string, unknown>): Promise<any>; getSnapshot(id: string): Promise<any> };
}

// Connect opens an MCP session bound to one sandbox.
export type Connect = (sandboxName: string) => Promise<SessionLike & { close(): Promise<void> }>;

// Stats is one /stats answer: the HTTP status (null if unreachable) and the JSON body.
type Stats = [number | null, Record<string, any>];
type Fetch = (url: string) => Promise<Stats>;

// missingEnv returns the required variables that are not set.
export function missingEnv(env: Record<string, string | undefined>): string[] {
  return REQUIRED_ENV.filter((name) => !env[name]);
}

// mcpConnect returns a Connect for the sandbox MCP server, authenticated with the sandbox API key.
export function mcpConnect(apiKey: string): Connect {
  return async (sandboxName) => {
    const { Client } = await import("@modelcontextprotocol/sdk/client/index.js");
    const { StreamableHTTPClientTransport } = await import("@modelcontextprotocol/sdk/client/streamableHttp.js");
    const client = new Client({ name: "undo-agent-mistake", version: "1.0.0" });
    const headers = { Authorization: `Bearer ${apiKey}`, "x-sandbox-name": sandboxName };
    await client.connect(new StreamableHTTPClientTransport(new URL(MCP_URL), { requestInit: { headers } }));
    return client as unknown as SessionLike & { close(): Promise<void> };
  };
}

// fetchStats GETs /stats on the app's preview URL: [status, JSON body], or [null, {error}] if unreachable.
export async function fetchStats(url: string, signal?: AbortSignal): Promise<Stats> {
  const timeout = AbortSignal.timeout(10_000);
  let response: Response;
  let body: string;
  try {
    response = await fetch(`${url}/stats`, { signal: signal ? AbortSignal.any([signal, timeout]) : timeout });
    body = await response.text();
  } catch (e) {
    if (signal?.aborted) throw e;
    return [null, { error: `unreachable (${(e as Error).name})` }];
  }
  try { return [response.status, JSON.parse(body)]; } catch { return [response.status, { error: body.slice(0, 200) }]; }
}

// describe renders one /stats answer for the progress log.
function describe([status, body]: Stats): string {
  return status === null ? `not answering: ${body.error}` : `${status} ${JSON.stringify(body)}`;
}

// dataIntact reports whether the app still answers with the customer and order counts it had before the agent ran.
export function dataIntact(status: number | null, body: Record<string, any>, baseline: Record<string, any>): boolean {
  return status === 200 && ["customers", "orders"].every((k) => body[k] === baseline[k]);
}

// waitForApp polls /stats until the app answers with anything but a gateway error, or APP_TIMEOUT_MS passes.
async function waitForApp(fetchFn: Fetch, url: string, wait: (ms: number) => Promise<void>, clock: () => number): Promise<Stats> {
  const deadline = clock() + APP_TIMEOUT_MS;
  while (true) {
    const stats = await fetchFn(url);
    if (![null, 502, 503, 504].includes(stats[0]) || clock() >= deadline) return stats;
    await wait(1000);
  }
}

// startApp uploads the app, seeds its data, starts the server and returns its preview URL, process and first answer.
async function startApp(sandbox: any, fetchFn: Fetch, wait: (ms: number) => Promise<void>, clock: () => number) {
  for (const name of ["server.py", "seed.py"]) await sandbox.files.write(name, await readFile(new URL(name, APP_DIR), "utf8"));
  const seeded = await sandbox.exec(["python3", "seed.py"]);
  if (seeded.exitCode !== 0) throw new Error(`seeding the data failed: ${seeded.stderr.trim()}`);
  const proc = await sandbox.processes.start(["python3", "server.py"]);
  const url: string = await sandbox.getUrl({ port: PORT }); // waits for the route, not the server: a gateway 502 counts as routable
  const stats = await waitForApp(fetchFn, url, wait, clock);
  if (stats[0] !== 200) throw new Error(`the app did not answer: ${describe(stats)}`);
  return { url, proc, baseline: stats[1] };
}

// takeSnapshot snapshots the sandbox and waits while it is Pending or Running; anything but Ready is an error.
async function takeSnapshot(neev: NeevLike, sandbox: any, wait: (ms: number) => Promise<void>, clock: () => number) {
  let snap = await sandbox.snapshot({ name: `${sandbox.name}-before-cleanup` });
  const deadline = clock() + SNAPSHOT_TIMEOUT_MS;
  while (snap.status === "Pending" || snap.status === "Running") {
    if (clock() >= deadline) throw new Error(`snapshot ${snap.id} did not become Ready within ${SNAPSHOT_TIMEOUT_MS / 1000}s`);
    await wait(1000);
    snap = await neev.sandboxes.getSnapshot(snap.id);
  }
  if (snap.status !== "Ready") throw new Error(`snapshot ${snap.id} is ${snap.status}: ${snap.error_message ?? "no reason given"}`);
  return snap;
}

// showDamage prints which data files are gone and what the app answers now.
async function showDamage(sandbox: any, url: string, stats: Stats, log: (s: string) => void) {
  for (const path of DATA_FILES) log(`   ${path}: ${(await sandbox.files.exists(path)) ? "still there" : "GONE"}`);
  log(`   ${url}/stats -> ${describe(stats)}`);
}

// customerRows counts data rows in customers.csv as read back from the sandbox, or null if it cannot be read.
async function customerRows(sandbox: any): Promise<number | null> {
  try { return (await sandbox.files.readText(DATA_FILES[0])).trimEnd().split(/\r?\n/).length - 1; } catch { return null; }
}

// proveRestore checks files, data, process and memory against the moment of the snapshot and prints each result.
// The request count lives only in the server's memory: a restarted server would start again from 0,
// so count == snapshot count + 1 (this request) proves the same process came back with its memory.
async function proveRestore(sandbox: any, proc: any, [status, body]: Stats, baseline: Record<string, any>, log: (s: string) => void) {
  const rows = await customerRows(sandbox);
  const pid = body.pid;
  const checks: [string, boolean][] = [
    [`files: customers.csv read back with ${rows} rows, shop.db present`,
      rows === baseline.customers && await sandbox.files.exists(DATA_FILES[1])],
    [`data: the app answers ${describe([status, body])}`, dataIntact(status, body, baseline)],
    [`process: same server, PID ${pid} (was ${baseline.pid}), ${proc.id} running`,
      pid === baseline.pid && (await sandbox.processes.get(proc.id)).state === "running"],
    [`memory: request count is ${body.served} (was ${baseline.served} at the snapshot, +1 for this request)`,
      body.served === baseline.served + 1],
  ];
  for (const [label, ok] of checks) log(`   ${ok ? "ok    " : "FAILED"} ${label}`);
  return checks.every(([, ok]) => ok);
}

// run does the whole demo in one sandbox and always deletes it; returns 0 only when the restore was proven.
// With `ask`, it waits twice so you can open the app: after the damage, and after the restore is proven.
export async function run(
  neev: NeevLike, modelClient: ModelLike, model: string, connect: Connect,
  opts: { log?: (s: string) => void; fetch?: Fetch; wait?: (ms: number) => Promise<void>; clock?: () => number; signal?: AbortSignal;
    ask?: (prompt: string) => Promise<string> } = {},
): Promise<number> {
  const { log = console.log, signal, clock = () => performance.now(), ask } = opts;
  // pause waits for Enter; a prompt that fails, such as on a closed stdin, carries on unless it was Ctrl+C.
  const pause = async (prompt: string) => {
    try { await ask?.(prompt); } catch (e) { if (signal?.aborted) throw e; }
  };
  const wait = opts.wait ?? ((ms: number) => sleep(ms, undefined, { signal }));
  const fetchFn = opts.fetch ?? ((url: string) => fetchStats(url, signal));
  // say prints a numbered step; a Ctrl+C that landed during an SDK call stops the run here.
  const say = (line: string) => { signal?.throwIfAborted(); log(line); };
  // abortable lets Ctrl+C end an SDK wait that takes no signal; the sandbox is already known, so finally deletes it.
  const abortable = <T>(p: Promise<T>): Promise<T> => !signal ? p : Promise.race([p, new Promise<never>((_, reject) => {
    if (signal.aborted) reject(signal.reason);
    signal.addEventListener("abort", () => reject(signal.reason), { once: true });
  })]);
  const seconds = (since: number) => ((clock() - since) / 1000).toFixed(1);
  let sandbox: any;
  try {
    say("1. Creating a sandbox (no internet access)...");
    sandbox = await neev.sandboxes.create({ name: `undo-mistake-js-${randomBytes(4).toString("hex")}`, egress: { mode: "deny_all" } });
    await abortable(sandbox.waitUntilReady({ timeoutMs: 300_000 }));
    say("2. Starting a small shop app: data files plus a running server...");
    const { url, proc, baseline } = await abortable(startApp(sandbox, fetchFn, wait, clock));
    log(`   ${url}/stats -> ${describe([200, baseline])}`);
    say("3. Taking a memory snapshot (files, memory and running processes)...");
    let started = clock();
    const snap = await abortable(takeSnapshot(neev, sandbox, wait, clock));
    log(`   snapshot ${snap.id} Ready in ${seconds(started)}s`);
    say(`4. Asking ${model} to: "${TASK}"`);
    const session = await connect(sandbox.name);
    let summary: string;
    try { summary = await cleanUp(session, modelClient, model, TASK, { log, signal }); } finally { await session.close(); }
    log(`   agent's summary: ${summary}`);
    say("5. Checking the damage...");
    let stats = await waitForApp(fetchFn, url, wait, clock); // a gateway blip is not damage
    if (dataIntact(stats[0], stats[1], baseline)) {
      log("   The agent left the data alone. Making the mistake for it so the rollback has something to undo.");
      log(`   scripted mistake: ${SCRIPTED_MISTAKE}`);
      await sandbox.exec(["sh", "-c", SCRIPTED_MISTAKE]);
      stats = await waitForApp(fetchFn, url, wait, clock);
      if (dataIntact(stats[0], stats[1], baseline)) throw new Error(`the app still has its data after ${SCRIPTED_MISTAKE}`);
    } else {
      log("   The agent did the damage itself.");
    }
    await showDamage(sandbox, url, stats, log);
    if (ask) {
      log(`   See it yourself: open ${url}/stats in a browser. The app is broken: its data is gone.`);
      await pause("   Press Enter to roll back. ");
    }
    say("6. Rolling back to the snapshot...");
    started = clock();
    await abortable(sandbox.rollback(snap.id).then(() => sandbox.waitUntilReady({ timeoutMs: 300_000 })));
    const readyS = seconds(started);
    stats = await waitForApp(fetchFn, url, wait, clock);
    log(`   sandbox Ready ${readyS}s after the rollback call, app answering after ${seconds(started)}s`);
    say("7. Checking everything against the snapshot...");
    if (!(await abortable(proveRestore(sandbox, proc, stats, baseline, log)))) {
      log("The rollback did not restore everything.");
      return 1;
    }
    log("Restore proven: the files, the data, the running server and its memory are back.");
    if (ask) {
      log(`   Open ${url}/stats again: the same app, with its data back.`);
      await pause("   Press Enter to finish and delete the sandbox. ");
    }
    return 0;
  } catch (e) {
    if (signal?.aborted) return 130;
    // e.g. a rejected model key: one line instead of a stack trace.
    log(`Failed: ${(e as Error).constructor.name}: ${(e as Error).message}`);
    return 1;
  } finally {
    if (sandbox) {
      try {
        await sandbox.delete();
        log("   Sandbox deleted.");
      } catch (e) { // keep the run's exit code; tell the user what to clean up by hand
        log(`   Could not delete sandbox ${sandbox.name} (${(e as Error).name}: ${(e as Error).message}); delete it from the console.`);
      }
    }
  }
}

// main parses arguments, checks the environment, and runs the recipe with Ctrl+C wired to cleanup.
async function main(): Promise<number> {
  let noWait = false;
  try {
    const { values } = parseArgs({ args: process.argv.slice(2), options: { help: { type: "boolean", short: "h" }, "no-wait": { type: "boolean" } } });
    if (values.help) {
      console.log(DESCRIPTION);
      return 0;
    }
    noWait = values["no-wait"] ?? false;
  } catch (e) { console.error((e as Error).message); return 2; }
  const missing = missingEnv(process.env);
  if (missing.length) { console.error(`Missing environment variables: ${missing.join(", ")}. See README.md.`); return 2; }
  const { Neev } = await import("@neevcloud/sdk");
  const { default: OpenAI } = await import("openai");
  const ac = new AbortController();
  // Keep the handler for repeated presses, so a second Ctrl+C cannot skip the cleanup.
  process.on("SIGINT", () => ac.abort());
  const modelClient = new OpenAI({ baseURL: MODEL_BASE_URL, apiKey: process.env.NEEV_MODEL_API_KEY });
  // In a terminal, pause for you to open the app. readline swallows Ctrl+C, so route it to the same abort.
  const rl = process.stdin.isTTY && !noWait ? (await import("node:readline/promises")).createInterface({ input: process.stdin, output: process.stdout }) : undefined;
  rl?.on("SIGINT", () => ac.abort());
  const ask = rl ? (prompt: string) => rl.question(prompt, { signal: ac.signal }) : undefined;
  try {
    return await run(new Neev() as unknown as NeevLike, modelClient as unknown as ModelLike, process.env.MODEL ?? DEFAULT_MODEL,
      mcpConnect(process.env.NEEV_API_KEY!), { signal: ac.signal, ask });
  } finally {
    rl?.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().then((code) => process.exit(code), (e) => { console.error(`Failed: ${e.message}`); process.exit(1); });
}
