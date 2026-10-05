// Prompt to live app: an agent builds a web app in a NeevCloud sandbox and you get a public URL.
import { randomBytes } from "node:crypto";
import { setTimeout as sleep } from "node:timers/promises";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { AgentFailed, buildApp, type ModelLike, type SessionLike } from "./agent.ts";

export const REQUIRED_ENV = ["NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"] as const;
const MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp";
const MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1";
const DEFAULT_MODEL = "glm-4-7";
const PORT = 3000;

export interface NeevLike { sandboxes: { create(params: Record<string, unknown>): Promise<any> } }

// Connect opens an MCP session bound to one sandbox.
export type Connect = (sandboxName: string) => Promise<SessionLike & { close(): Promise<void> }>;

// mcpConnect returns a Connect for the sandbox MCP server, authenticated with the sandbox API key.
export function mcpConnect(apiKey: string): Connect {
  return async (sandboxName) => {
    const { Client } = await import("@modelcontextprotocol/sdk/client/index.js");
    const { StreamableHTTPClientTransport } = await import("@modelcontextprotocol/sdk/client/streamableHttp.js");
    const client = new Client({ name: "prompt-to-live-app", version: "1.0.0" });
    const headers = { Authorization: `Bearer ${apiKey}`, "x-sandbox-name": sandboxName };
    await client.connect(new StreamableHTTPClientTransport(new URL(MCP_URL), { requestInit: { headers } }));
    return client as unknown as SessionLike & { close(): Promise<void> };
  };
}

// missingEnv returns the required variables that are not set.
export function missingEnv(env: Record<string, string | undefined>): string[] {
  return REQUIRED_ENV.filter((name) => !env[name]);
}

// parseCli reads the request and --keep (minutes) from the command line.
export function parseCli(argv: string[]): { request: string; keep: number } {
  const { values, positionals } = parseArgs({ args: argv, allowPositionals: true, options: { keep: { type: "string", default: "10" } } });
  const keep = Number(values.keep);
  if (!Number.isFinite(keep) || keep < 0) throw new Error(`--keep must be a number of minutes, got "${values.keep}"`);
  return { request: positionals[0] ?? "a todo app with a dark theme", keep };
}

// run creates a sandbox, has the agent build the app over MCP, serves it, and always deletes the sandbox.
export async function run(
  request: string, keepMinutes: number, neev: NeevLike, modelClient: ModelLike, model: string, connect: Connect,
  opts: { log?: (s: string) => void; wait?: (ms: number) => Promise<void>; signal?: AbortSignal } = {},
): Promise<number> {
  const { log = console.log, wait = (ms) => sleep(ms, undefined, { signal: opts.signal }), signal } = opts;
  let sandbox: any;
  let served = false;
  try {
    log("1. Creating a sandbox (no internet access)...");
    sandbox = await neev.sandboxes.create({ name: `live-app-${randomBytes(4).toString("hex")}`, egress: { mode: "deny_all" } });
    await sandbox.waitUntilReady({ timeoutMs: 300_000 });
    log(`2. Asking ${model} to build: ${request}`);
    const session = await connect(sandbox.name);
    let summary: string;
    try { summary = await buildApp(session, modelClient, model, request, { log, signal }); } finally { await session.close(); }
    log(`3. Agent finished: ${summary}`);
    log(`4. Starting the app on port ${PORT}...`);
    // Bind 0.0.0.0: the preview URL cannot reach a server listening on 127.0.0.1.
    await sandbox.processes.start("python3", { args: ["-m", "http.server", String(PORT), "--bind", "0.0.0.0"] });
    const url = await sandbox.getUrl({ port: PORT });
    served = true;
    log(`5. Live at: ${url}`);
    if (keepMinutes > 0) {
      log(`   Keeping it up for ${keepMinutes} minutes. Press Ctrl+C to stop sooner.`);
      await wait(keepMinutes * 60_000);
    }
    return 0;
  } catch (e) {
    if (signal?.aborted) return served ? 0 : 130;
    if (e instanceof AgentFailed) { log(`The agent did not finish the app: ${e.message}`); return 1; }
    // e.g. a rejected model key: one line instead of a stack trace.
    log(`Failed: ${(e as Error).name}: ${(e as Error).message}`);
    return 1;
  } finally {
    if (sandbox) { await sandbox.delete(); log("   Sandbox deleted."); }
  }
}

// main parses arguments, checks the environment, and runs the recipe with Ctrl+C wired to cleanup.
async function main(): Promise<number> {
  let cli: { request: string; keep: number };
  try { cli = parseCli(process.argv.slice(2)); } catch (e) { console.error((e as Error).message); return 2; }
  const { request, keep } = cli;
  const missing = missingEnv(process.env);
  if (missing.length) { console.error(`Missing environment variables: ${missing.join(", ")}. See README.md.`); return 2; }
  const { Neev } = await import("@neevcloud/sdk");
  const { default: OpenAI } = await import("openai");
  const ac = new AbortController();
  // Keep the handler for repeated presses, so a second Ctrl+C cannot skip the cleanup.
  process.on("SIGINT", () => ac.abort());
  const modelClient = new OpenAI({ baseURL: MODEL_BASE_URL, apiKey: process.env.NEEV_MODEL_API_KEY });
  return run(request, keep, new Neev() as unknown as NeevLike, modelClient as unknown as ModelLike, process.env.MODEL ?? DEFAULT_MODEL,
    mcpConnect(process.env.NEEV_API_KEY!), { signal: ac.signal });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().then((code) => process.exit(code), (e) => { console.error(`Failed: ${e.message}`); process.exit(1); });
}
