// Untrusted code runner: POST /run executes an end user's code in that user's own NeevCloud sandbox.
import http from "node:http";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { CapacityError, ClosedError, LANGUAGES, type Language, type NeevLike, Runner, type RunResult } from "./runner.ts";

export const REQUIRED_ENV = ["NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID"] as const;
const MAX_BODY = 400_000; // room for MAX_CODE characters even when every one is sent as a \uXXXX escape
const MAX_CODE = 64_000;
const USER_ID = /^[A-Za-z0-9._@-]{1,64}$/;

export interface Cli { port: number; maxSandboxes: number; idleTtlMs: number; runTimeoutMs: number }

// missingEnv returns the required variables that are not set.
export function missingEnv(env: Record<string, string | undefined>): string[] {
  return REQUIRED_ENV.filter((name) => !env[name]);
}

// parseCli reads --port, --max-sandboxes, --idle-ttl (seconds) and --run-timeout (seconds).
export function parseCli(argv: string[]): Cli {
  const { values } = parseArgs({ args: argv, options: {
    port: { type: "string", default: "8080" }, "max-sandboxes": { type: "string", default: "3" },
    "idle-ttl": { type: "string", default: "120" }, "run-timeout": { type: "string", default: "5" },
  } });
  const num = (flag: keyof typeof values, ok: (n: number) => boolean) => {
    const n = Number(values[flag]);
    if (!ok(n)) throw new Error(`--${flag} got "${values[flag]}"; see README.md for valid values`);
    return n;
  };
  return {
    port: num("port", (n) => Number.isInteger(n) && n >= 0 && n < 65536),
    maxSandboxes: num("max-sandboxes", (n) => Number.isInteger(n) && n >= 1),
    idleTtlMs: num("idle-ttl", (n) => n > 0) * 1000,
    runTimeoutMs: num("run-timeout", (n) => n > 0 && n <= 60) * 1000,
  };
}

class HttpError extends Error { constructor(readonly status: number, message: string) { super(message); } }

// readJson reads a request body up to MAX_BODY bytes and parses it as JSON.
async function readJson(req: http.IncomingMessage): Promise<unknown> {
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY) throw new HttpError(413, `body is larger than ${MAX_BODY} bytes`);
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch { throw new HttpError(400, "body is not valid JSON"); }
}

// validate checks the request at the trust boundary: who is asking, which language, and how much code.
function validate(body: unknown): { user: string; language: Language; code: string } {
  const b = (typeof body === "object" && body !== null ? body : {}) as Record<string, unknown>;
  if (typeof b.user !== "string" || !USER_ID.test(b.user)) throw new HttpError(400, "user must be 1-64 letters, digits or . _ @ -");
  if (!LANGUAGES.includes(b.language as Language)) throw new HttpError(400, `language must be one of: ${LANGUAGES.join(", ")}`);
  if (typeof b.code !== "string" || b.code.length === 0 || b.code.length > MAX_CODE) throw new HttpError(400, `code must be a string of 1-${MAX_CODE} characters`);
  return { user: b.user, language: b.language as Language, code: b.code };
}

// toJson renders a run result in the API's snake_case shape.
const toJson = (r: RunResult) => ({
  stdout: r.stdout, stderr: r.stderr, exit_code: r.exitCode, timed_out: r.timedOut, duration_ms: r.durationMs,
  ...(r.sandboxRestarted ? { sandbox_restarted: r.sandboxRestarted } : {}),
});

// createServer serves POST /run on top of a Runner; every failure becomes a JSON error with a status.
export function createServer(runner: Runner): http.Server {
  return http.createServer(async (req, res) => {
    const send = (status: number, body: unknown) => { res.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify(body)); };
    try {
      if (req.method !== "POST" || req.url !== "/run") throw new HttpError(404, "use POST /run");
      const { user, language, code } = validate(await readJson(req));
      send(200, toJson(await runner.run(user, language, code)));
    } catch (err) {
      if (err instanceof HttpError) send(err.status, { error: err.message });
      else if (err instanceof CapacityError) send(429, { error: err.message });
      else if (err instanceof ClosedError) send(503, { error: err.message });
      else send(502, { error: `the sandbox platform failed: ${(err as Error).message}` });
    }
  });
}

// main checks the environment, starts the server, and deletes every sandbox on Ctrl+C or SIGTERM.
async function main(): Promise<void> {
  let cli: Cli;
  try { cli = parseCli(process.argv.slice(2)); } catch (e) { console.error((e as Error).message); process.exit(2); }
  const missing = missingEnv(process.env);
  if (missing.length) { console.error(`Missing environment variables: ${missing.join(", ")}. See README.md.`); process.exit(2); }
  const { Neev } = await import("@neevcloud/sdk");
  const runner = new Runner(new Neev() as unknown as NeevLike, cli);
  const server = createServer(runner);
  // Keep the handlers after the first signal, so a second Ctrl+C cannot skip the cleanup.
  let stopping = false;
  const stop = async () => {
    if (stopping) return;
    stopping = true;
    console.log("Shutting down: deleting every sandbox...");
    server.close();
    await runner.close();
    process.exit(0);
  };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
  server.listen(cli.port, "127.0.0.1", () => {
    console.log(`Code runner listening on http://127.0.0.1:${cli.port}/run`);
    console.log(`  at most ${cli.maxSandboxes} sandboxes, ${cli.runTimeoutMs / 1000}s per run, idle sandboxes deleted after ${cli.idleTtlMs / 1000}s`);
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((e) => { console.error(`Failed: ${e.message}`); process.exit(1); });
}
