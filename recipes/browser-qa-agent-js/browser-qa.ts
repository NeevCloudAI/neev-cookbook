// Browser QA agent: a real browser QA pass on three viewports, run in an isolated NeevCloud sandbox.
import { randomBytes } from "node:crypto";
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

export const REQUIRED_ENV = ["NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID"] as const;
const TEMPLATE = "sb-ubuntu-26-04-playwright";
export const SANDBOX_DIR = fileURLToPath(new URL("./sandbox/", import.meta.url));
const SITE_FILES = ["index.html", "about.html", "style.css"];
const DEMO_URL = "http://127.0.0.1:8080/";
const REPORTS = ["report.md", "report.json"];
const VIEWPORTS = [["phone", 8], ["tablet", 10], ["desktop", 10]] as const; // with each summary column's width
// Waits for the demo server, so the first page load never races its startup.
const WAIT_FOR_SITE = `for i in $(seq 50); do curl -sf -o /dev/null ${DEMO_URL} && exit 0; sleep 0.2; done; exit 1`;

export interface NeevLike { sandboxes: { create(params: Record<string, unknown>): Promise<any> } }
type Event = { type: "stdout" | "stderr"; data: string } | { type: "exit"; exitCode: number };
type Visit = { viewport: string; url: string; status: number; ms: number; errors: string[] };
export type Report = { url: string; pages: Visit[]; screenshots: string[]; links: { checked: number; not_checked: number; broken: { url: string; status: number }[] } };

// missingEnv returns the required variables that are not set.
export function missingEnv(env: Record<string, string | undefined>): string[] {
  return REQUIRED_ENV.filter((name) => !env[name]);
}

// egressFor gives the demo site no internet; a public URL gets only its host and the --allow hosts.
export function egressFor(url: string | undefined, allow: string[]): { mode: string; allow?: { host: string }[] } {
  if (url === undefined) return { mode: "deny_all" };
  return { mode: "allow_list", allow: [...new Set([new URL(url).hostname, ...allow])].map((host) => ({ host })) };
}

// stream logs the command's output line by line as it arrives and returns its exit code.
export async function stream(events: AsyncIterable<Event>, log: (s: string) => void): Promise<number> {
  let pending = "";
  for await (const event of events) {
    if (event.type === "exit") {
      if (pending) log(`   ${pending}`);
      return event.exitCode;
    }
    const lines = (pending + event.data).split("\n");
    pending = lines.pop()!;
    for (const line of lines) log(`   ${line}`);
  }
  throw new Error("the QA run ended without an exit code");
}

// clip shortens text to fit one terminal column.
const clip = (text: string, limit: number) => (text.length <= limit ? text : `${text.slice(0, limit - 3)}...`);

// summary formats the report as one row per page, with its load time on each viewport, then the broken links.
export function summary(report: Report): string[] {
  const pages = new Map<string, Map<string, Visit>>();
  for (const visit of report.pages) {
    const { pathname, search } = new URL(visit.url);
    if (!pages.has(pathname + search)) pages.set(pathname + search, new Map());
    pages.get(pathname + search)!.set(visit.viewport, visit);
  }
  const header = VIEWPORTS.map(([v, w]) => (v[0].toUpperCase() + v.slice(1)).padStart(w)).join("");
  const lines = [`   ${"Page".padEnd(16)}${"Status".padEnd(11)}${header}  Console errors`];
  for (const [path, byViewport] of pages) {
    const visits = [...byViewport.values()];
    const status = [...new Set(visits.map((v) => String(v.status || "failed")))].join("/");
    const loads = VIEWPORTS.map(([v, w]) => (byViewport.has(v) ? `${byViewport.get(v)!.ms} ms` : "-").padStart(w)).join("");
    const errors = [...new Set(visits.flatMap((v) => v.errors))];
    lines.push(`   ${clip(path, 15).padEnd(16)}${status.padEnd(11)}${loads}  ${errors.length}${errors.length ? `  ${clip(errors[0], 60)}` : ""}`);
  }
  const { checked, not_checked, broken } = report.links;
  lines.push(`   Broken links: ${broken.length} of ${checked} checked, ${not_checked} not checked`);
  lines.push(...broken.map((b) => `     ${b.url} (${b.status || "unreachable"})`));
  return lines;
}

// run creates a Playwright sandbox, runs the QA pass in it, downloads the results, and always deletes it.
export async function run(
  url: string | undefined, allow: string[], outDir: string, neev: NeevLike,
  opts: { log?: (s: string) => void; signal?: AbortSignal } = {},
): Promise<number> {
  const { log = console.log, signal } = opts;
  let sandbox: any;
  const began = Date.now();
  try {
    const target = url ?? DEMO_URL;
    const egress = egressFor(url, allow);
    const reach = url ? `internet access to ${egress.allow!.map((h) => h.host).join(", ")}` : "no internet access";
    log(`1. Creating a sandbox from ${TEMPLATE} (${reach})...`);
    sandbox = await neev.sandboxes.create({ name: `browser-qa-${randomBytes(4).toString("hex")}`, sandbox_template_id: TEMPLATE, egress });
    await sandbox.waitUntilReady({ timeoutMs: 300_000 });
    signal?.throwIfAborted(); // waitUntilReady takes no signal, so a Ctrl+C during boot is honoured here
    log("2. Uploading the QA script...");
    await sandbox.files.write("qa.js", readFileSync(join(SANDBOX_DIR, "qa.js"), "utf8"));
    if (url === undefined) {
      log(`   Serving the bundled demo site on ${DEMO_URL}`);
      for (const name of SITE_FILES) await sandbox.files.write(`site/${name}`, readFileSync(join(SANDBOX_DIR, "site", name), "utf8"));
      await sandbox.processes.start("python3", { args: ["-m", "http.server", "8080", "--bind", "127.0.0.1", "--directory", "site"] });
      if ((await sandbox.exec(["sh", "-c", WAIT_FOR_SITE], { timeoutMs: 30_000 })).exitCode !== 0) throw new Error("the demo site did not start");
    }
    log(`3. Checking ${target} on phone, tablet and desktop...`);
    // Streamed, so each page's progress line arrives as it is checked rather than at the end.
    const events = sandbox.exec(["node", "qa.js", target, ...allow], { timeoutMs: 600_000, stream: true, signal });
    if ((await stream(events, log)) !== 0) throw new Error("the QA script failed");
    const report: Report = JSON.parse(await sandbox.files.readText("qa-out/report.json"));
    if (!report.pages.some((p) => p.status)) throw new Error(`${target} did not load on any viewport`);
    log(`4. Downloading the screenshots and report to ${outDir}/...`);
    mkdirSync(outDir, { recursive: true });
    for (const name of [...report.screenshots, ...REPORTS]) await sandbox.files.downloadFile(`qa-out/${name}`, join(outDir, name));
    log("5. Summary:");
    for (const line of summary(report)) log(line);
    log(`   Done in ${Math.round((Date.now() - began) / 1000)} s. Open ${join(outDir, "report.md")} and the screenshots.`);
    return 0;
  } catch (e) {
    if (signal?.aborted) return 130;
    log(`Failed: ${(e as Error).message}`); // one line instead of a stack trace
    return 1;
  } finally {
    if (sandbox) { await sandbox.delete(); log("   Sandbox deleted."); }
  }
}

// parseCli reads --url, the repeatable --allow and --out, rejecting a URL that is not http(s).
export function parseCli(argv: string[]): { url?: string; allow: string[]; out: string } {
  const { values } = parseArgs({ args: argv, options: {
    url: { type: "string" }, allow: { type: "string", multiple: true, default: [] }, out: { type: "string", default: "qa-report" } } });
  const { url, allow, out } = values;
  if (url !== undefined && !(URL.canParse(url) && ["http:", "https:"].includes(new URL(url).protocol))) {
    throw new Error("--url must start with http:// or https://");
  }
  if (allow.length && url === undefined) throw new Error("--allow needs --url: the demo site runs without internet access");
  return { url, allow, out };
}

// main parses arguments, checks the environment, and runs the recipe with Ctrl+C wired to cleanup.
async function main(): Promise<number> {
  let cli: ReturnType<typeof parseCli>;
  try { cli = parseCli(process.argv.slice(2)); } catch (e) { console.error((e as Error).message); return 2; }
  const missing = missingEnv(process.env);
  if (missing.length) { console.error(`Missing environment variables: ${missing.join(", ")}. See README.md.`); return 2; }
  const { Neev } = await import("@neevcloud/sdk");
  const ac = new AbortController();
  // Keep the handler for repeated presses, so a second Ctrl+C cannot skip the cleanup.
  process.on("SIGINT", () => ac.abort());
  return run(cli.url, cli.allow, cli.out, new Neev() as unknown as NeevLike, { signal: ac.signal });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().then((code) => process.exit(code), (e) => { console.error(`Failed: ${e.message}`); process.exit(1); });
}
