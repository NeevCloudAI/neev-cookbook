// AI data analyst: an agent answers a question about a CSV by running pandas in a NeevCloud sandbox, and saves a chart.
import { randomBytes } from "node:crypto";
import { existsSync, writeFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { AgentFailed, CHART, analyse, type ModelLike, type SessionLike } from "./agent.ts";

export const REQUIRED_ENV = ["NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"] as const;
const MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp";
const MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1";
const DEFAULT_MODEL = "glm-4-7";
export const SAMPLE_CSV = fileURLToPath(new URL("data/sales.csv", import.meta.url));
const DEFAULT_QUESTION = "Which cities and product categories bring in the most revenue, and how much do " +
  "October and November lift sales? Chart monthly revenue for the top cities.";
// The only hosts pip needs: the package index and the file host it redirects to.
const PYPI_HOSTS = ["pypi.org", "files.pythonhosted.org"];
// The template's Python is managed by the OS, so pip needs --break-system-packages; --user keeps it out of system dirs.
// No --quiet: the exec stream times out after 60s without output, and pip's progress lines keep it alive.
const PIP_INSTALL = ["python3", "-m", "pip", "install", "--user", "--break-system-packages",
  "--disable-pip-version-check", "--no-warn-script-location", "--root-user-action=ignore", "pandas", "matplotlib"];
const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

export interface NeevLike { sandboxes: { create(params: Record<string, unknown>): Promise<any> } }

// Connect opens an MCP session bound to one sandbox.
export type Connect = (sandboxName: string) => Promise<SessionLike & { close(): Promise<void> }>;

// mcpConnect returns a Connect for the sandbox MCP server, authenticated with the sandbox API key.
export function mcpConnect(apiKey: string): Connect {
  return async (sandboxName) => {
    const { Client } = await import("@modelcontextprotocol/sdk/client/index.js");
    const { StreamableHTTPClientTransport } = await import("@modelcontextprotocol/sdk/client/streamableHttp.js");
    const client = new Client({ name: "ai-data-analyst", version: "1.0.0" });
    const headers = { Authorization: `Bearer ${apiKey}`, "x-sandbox-name": sandboxName };
    await client.connect(new StreamableHTTPClientTransport(new URL(MCP_URL), { requestInit: { headers } }));
    return client as unknown as SessionLike & { close(): Promise<void> };
  };
}

// missingEnv returns the required variables that are not set.
export function missingEnv(env: Record<string, string | undefined>): string[] {
  return REQUIRED_ENV.filter((name) => !env[name]);
}

// isPng checks the first bytes of a file against the PNG signature.
const isPng = (data: Uint8Array) => PNG_SIGNATURE.every((b, i) => data[i] === b);

// run creates a sandbox, installs pandas, has the agent analyse the CSV over MCP, downloads the chart, and always deletes.
export async function run(
  question: string, csvPath: string, outPath: string, neev: NeevLike, modelClient: ModelLike, model: string, connect: Connect,
  opts: { log?: (s: string) => void; signal?: AbortSignal } = {},
): Promise<number> {
  const { log = console.log, signal } = opts;
  let sandbox: any;
  try {
    log("1. Creating a sandbox that can reach only pypi.org and files.pythonhosted.org...");
    sandbox = await neev.sandboxes.create({ name: `data-analyst-js-${randomBytes(4).toString("hex")}`, allowEgress: PYPI_HOSTS });
    await sandbox.waitUntilReady({ timeoutMs: 300_000 });
    log("2. Installing pandas and matplotlib...");
    const installed = await sandbox.exec(PIP_INSTALL, { timeoutMs: 240_000, signal });
    if (installed.exitCode !== 0) {
      log(`Failed: pip install exited ${installed.exitCode}: ${installed.stderr.trim().slice(-300)}`);
      return 1;
    }
    // Lock egress before the data arrives: code the model writes can then reach no host at all.
    await sandbox.update({ egress: { mode: "deny_all" } });
    log("3. Internet access removed. Uploading the data...");
    await sandbox.files.uploadFile(csvPath, "data.csv");
    log(`4. Asking ${model}: ${question}`);
    const session = await connect(sandbox.name);
    let findings: string;
    try { findings = await analyse(session, modelClient, model, question, { log, signal }); } finally { await session.close(); }
    log(`5. Downloading ${CHART}...`);
    const chart: Uint8Array = await sandbox.files.read(CHART);
    if (!isPng(chart)) {
      log(`Failed: the agent's ${CHART} is not a PNG image`);
      return 1;
    }
    writeFileSync(outPath, chart);
    log(`   Saved ${outPath} (${Math.floor(chart.length / 1024)} KB)`);
    log("6. Findings:");
    log(findings);
    return 0;
  } catch (e) {
    if (signal?.aborted) return 130;
    if (e instanceof AgentFailed) { log(`The agent did not finish the analysis: ${e.message}`); return 1; }
    // e.g. a rejected model key: one line instead of a stack trace.
    log(`Failed: ${(e as Error).name}: ${(e as Error).message}`);
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

// main parses arguments, checks the environment and the CSV, and runs the recipe with Ctrl+C wired to cleanup.
async function main(): Promise<number> {
  let values: { csv?: string; out?: string }, positionals: string[];
  try {
    ({ values, positionals } = parseArgs({
      args: process.argv.slice(2), allowPositionals: true,
      options: { csv: { type: "string", default: SAMPLE_CSV }, out: { type: "string", default: "chart.png" } },
    }));
  } catch (e) { console.error((e as Error).message); return 2; }
  const missing = missingEnv(process.env);
  if (missing.length) { console.error(`Missing environment variables: ${missing.join(", ")}. See README.md.`); return 2; }
  if (!existsSync(values.csv!)) { console.error(`CSV file not found: ${values.csv}`); return 2; }
  const { Neev } = await import("@neevcloud/sdk");
  const { default: OpenAI } = await import("openai");
  const ac = new AbortController();
  // Keep the handler for repeated presses, so a second Ctrl+C cannot skip the cleanup.
  process.on("SIGINT", () => ac.abort());
  const modelClient = new OpenAI({ baseURL: MODEL_BASE_URL, apiKey: process.env.NEEV_MODEL_API_KEY });
  return run(positionals[0] ?? DEFAULT_QUESTION, values.csv!, values.out!, new Neev() as unknown as NeevLike,
    modelClient as unknown as ModelLike, process.env.MODEL ?? DEFAULT_MODEL, mcpConnect(process.env.NEEV_API_KEY!), { signal: ac.signal });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().then((code) => process.exit(code), (e) => { console.error(`Failed: ${e.message}`); process.exit(1); });
}
