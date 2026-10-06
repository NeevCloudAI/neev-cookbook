// test/fakes.ts: in-memory stand-ins for the sandbox's MCP session, the SDK sandbox, and a model client.
import type { ModelLike, SessionLike } from "../agent.ts";
import type { AuditRecordLike, Connect, ExecResultLike, SandboxLike } from "../injection-demo.ts";

// Everything the real server lists, so tests can check the model only ever sees the workspace tools.
const SERVER_TOOLS = ["exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox", "expose_port"];

const ok = (data: Record<string, unknown>) => ({ isError: false, structuredContent: data, content: [{ type: "text", text: JSON.stringify(data) }] });
const err = (message: string) => ({ isError: true, content: [{ type: "text", text: message }] });

// fakeSession mimics an MCP session bound to one sandbox: fs_* backed by a map, exec scripted.
export function fakeSession(execOutput = "") {
  const files = new Map<string, string>();
  const calls: [string, Record<string, unknown>][] = [];
  const raiseOn = new Map<string, Error>();
  const session: SessionLike & { files: typeof files; calls: typeof calls; raiseOn: typeof raiseOn } = {
    files, calls, raiseOn,
    async listTools() {
      return { tools: SERVER_TOOLS.map((name) => ({ name, description: `${name} from the server`, inputSchema: { type: "object", properties: { x: { type: "string" } } } })) };
    },
    async callTool({ name, arguments: a = {} }) {
      const args = a as Record<string, any>;
      calls.push([name, args]);
      const thrown = raiseOn.get(name);
      if (thrown) throw thrown;
      if (name === "fs_write") {
        if (args.path.startsWith("/") || args.path.split("/").includes("..")) return err(`the sandbox refused this call: invalid_argument: path "${args.path}" escapes workspace root`);
        files.set(args.path, args.content);
        return ok({ bytes_written: args.content.length });
      }
      if (name === "fs_read") {
        const v = files.get(args.path);
        return v === undefined ? err(`the sandbox refused this call: not_found: ${args.path}`) : ok({ content: v, size: v.length, eof: true });
      }
      if (name === "fs_list") return ok({ entries: [...files.keys()].sort().map((n) => ({ name: n, type: "file" })) });
      if (name === "exec") return ok({ exit_code: 0, stdout: execOutput, stderr: "" });
      return err(`unexpected tool ${name}`);
    },
  };
  return session;
}

type Msg = { content: string | null; tool_calls?: { id: string; type: "function"; function: { name: string; arguments: string } }[] };

// fakeModel replays a scripted list of assistant messages, one per call, and records each request.
export function fakeModel(replies: Msg[]) {
  const requests: any[] = [];
  const model: ModelLike & { requests: any[] } = {
    requests,
    chat: { completions: { async create(body: any) { requests.push(structuredClone(body)); return { choices: [{ message: replies.shift()! }] }; } } },
  };
  return model;
}

// hangingModel never answers until its signal aborts, like a model call that outlives the budget.
export const hangingModel: ModelLike = { chat: { completions: { create: (_b: unknown, o?: { signal?: AbortSignal }) => new Promise<never>((_, reject) => {
  o?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
}) } } };

export const toolCall = (name: string, args: unknown, id = "c1"): Msg => ({
  content: null, tool_calls: [{ id, type: "function", function: { name, arguments: JSON.stringify(args) } }],
});
export const text = (content: string): Msg => ({ content });

// auditRecord builds one audit record with the fields the script reads.
export const auditRecord = (o: Partial<AuditRecordLike> = {}): AuditRecordLike => ({ tool: "exec", command: "curl", outcome: "success", ...o });

// fakeSandbox mimics an SDK sandbox handle: records fixture writes and execs, scripts curl results, serves audit pages.
export function fakeSandbox(o: { pasteExit?: number; pasteOut?: string; pypiExit?: number; pypiHttp?: string;
  auditPages?: AuditRecordLike[][]; readyError?: Error } = {}) {
  const { pasteExit = 28, pasteOut = "0 000", pypiExit = 0, pypiHttp = "200", readyError } = o;
  // Each audit() call takes one page; the last page repeats so extra polls still get records.
  const pages = o.auditPages ? [...o.auditPages] : [[auditRecord(), auditRecord()]];
  const sb = {
    name: "injection-proof-js-test",
    written: new Map<string, string>(),
    execs: [] as { command: string; args: string[]; cwd?: string; signal?: AbortSignal }[],
    auditQueries: [] as (string | undefined)[],
    deleted: false,
    async waitUntilReady(): Promise<unknown> { if (readyError) throw readyError; return undefined; },
    files: { async write(path: string, content: string) { sb.written.set(path, content); return { bytes_written: content.length }; } },
    async exec(command: string, options: { args?: string[]; cwd?: string; signal?: AbortSignal } = {}): Promise<ExecResultLike> {
      const args = options.args ?? [];
      sb.execs.push({ command, args, cwd: options.cwd, signal: options.signal });
      const joined = [command, ...args].join(" ");
      if (joined.includes("paste.rs")) return { exitCode: pasteExit, stdout: pasteOut, stderr: "curl: (28) timed out" };
      if (joined.includes("pypi.org")) return { exitCode: pypiExit, stdout: pypiHttp, stderr: "" };
      return { exitCode: 0, stdout: "", stderr: "" };
    },
    onAudit: undefined as (() => void) | undefined,
    async audit(params: { from?: string } = {}) {
      sb.auditQueries.push(params.from);
      sb.onAudit?.();
      return { records: pages.length === 1 ? pages[0] : pages.shift()! };
    },
    async delete() { sb.deleted = true; },
  } satisfies SandboxLike & Record<string, unknown>;
  return sb;
}

// fakeNeev mimics the SDK client's sandboxes.create and records the create params.
export function fakeNeev(sandbox: SandboxLike) {
  const created: any[] = [];
  return { created, sandboxes: { async create(p: Record<string, unknown>) { created.push(p); return sandbox; } } };
}

// connector returns a Connect that hands out the given session and records names and closes.
export function connector(session = fakeSession()) {
  const names: string[] = [];
  let closed = 0;
  const connect: Connect = async (name) => { names.push(name); return Object.assign(session, { close: async () => { closed++; } }); };
  return { connect, names, closed: () => closed };
}
