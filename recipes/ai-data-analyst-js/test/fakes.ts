// test/fakes.ts: in-memory stand-ins for the sandbox's MCP session, the sandbox SDK handle and a model client.
import type { ModelLike, SessionLike } from "../agent.ts";

// Everything the real server lists, so tests can check the model only ever sees the tools it is given.
const SERVER_TOOLS = ["exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox", "rollback_sandbox", "expose_port"];

const ok = (data: Record<string, unknown>) => ({ isError: false, structuredContent: data, content: [{ type: "text", text: JSON.stringify(data) }] });
const err = (message: string) => ({ isError: true, content: [{ type: "text", text: message }] });

type ExecResult = { exit_code: number; stdout: string; stderr: string };
type OnExec = (files: Map<string, string>, script: string) => ExecResult;

// savesChart is the default exec behaviour: a script that calls savefig writes chart.png, and its prints come back.
export const savesChart: OnExec = (files, script) => {
  if (script.includes("savefig")) files.set("chart.png", "\x89PNG...");
  return { exit_code: 0, stdout: "ok\n", stderr: "" };
};

// fakeSession mimics an MCP session bound to one sandbox: fs_* backed by a map, exec runs onExec on the script.
export function fakeSession(onExec: OnExec = savesChart) {
  const files = new Map<string, string>();
  const calls: [string, Record<string, unknown>][] = [];
  const signals: (AbortSignal | undefined)[] = [];
  const raiseOn = new Map<string, Error>();
  const session: SessionLike & { files: typeof files; calls: typeof calls; signals: typeof signals; raiseOn: typeof raiseOn } = {
    files, calls, signals, raiseOn,
    async listTools() {
      return { tools: SERVER_TOOLS.map((name) => ({ name, description: `${name} from the server`, inputSchema: { type: "object", properties: { x: { type: "string" } } } })) };
    },
    async callTool({ name, arguments: a = {} }, _schema, options) {
      const args = a as Record<string, any>;
      calls.push([name, args]);
      signals.push(options?.signal);
      const thrown = raiseOn.get(name);
      if (thrown) throw thrown;
      if (name === "fs_write") {
        files.set(args.path, args.content);
        return ok({ bytes_written: args.content.length });
      }
      if (name === "fs_read") {
        const v = files.get(args.path);
        return v === undefined ? err(`the sandbox refused this call: not_found: file not found: "${args.path}"`) : ok({ content: v, size: v.length, eof: true });
      }
      if (name === "fs_list") return ok({ entries: [...files.keys()].sort().map((n) => ({ name: n, type: "file" })) });
      if (name === "exec") {
        if (args.env !== undefined && !Array.isArray(args.env)) return err("invalid params: env must be an array"); // the server wants KEY=VALUE strings
        return ok(onExec(files, files.get((args.args ?? [""])[0]) ?? ""));
      }
      return err(`unexpected tool ${name}`);
    },
  };
  return session;
}

type Msg = { content: string | null; tool_calls?: { id: string; type: "function"; function: { name: string; arguments: string } }[] };

// fakeModel replays scripted replies, one per call, and records each request.
export function fakeModel(replies: Msg[]) {
  const requests: any[] = [];
  const model: ModelLike & { requests: any[] } = {
    requests,
    chat: { completions: { async create(body: any) { requests.push(structuredClone(body)); return { choices: [{ message: replies.shift()! }] }; } } },
  };
  return model;
}

// hangingModel never answers until its signal fires, like a model call stuck on the network.
export const hangingModel: ModelLike = {
  chat: { completions: { create: (_body: any, options?: { signal?: AbortSignal }) => new Promise((_, reject) => {
    options?.signal?.addEventListener("abort", () => reject(options.signal!.reason), { once: true });
  }) } },
};

export const toolCall = (name: string, args: unknown, id = "c1"): Msg => ({
  content: null, tool_calls: [{ id, type: "function", function: { name, arguments: JSON.stringify(args) } }],
});
export const text = (content: string): Msg => ({ content });

export const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x63, 0x68, 0x61, 0x72, 0x74]);

// fakeSandbox records exec, uploads, egress updates and deletion; chart.png reads back as `chart`.
export function fakeSandbox(opts: { chart?: Uint8Array | null; pipExit?: number } = {}) {
  const { chart = PNG, pipExit = 0 } = opts;
  const sb = {
    name: "data-analyst-1", execs: [] as string[][], uploads: [] as [string, string][], updates: [] as unknown[], deleted: false,
    order: [] as string[], // what happened, in order: install, update, upload
    async waitUntilReady() { return sb; },
    async exec(command: string[]) {
      sb.execs.push(command); sb.order.push("install");
      return { exitCode: pipExit, stdout: "", stderr: "ERROR: no matching distribution" };
    },
    async update(params: unknown) { sb.updates.push(params); sb.order.push("update"); return sb; },
    files: {
      async uploadFile(local: string, remote: string) { sb.uploads.push([local, remote]); sb.order.push("upload"); return { bytes_written: 1 }; },
      async read(path: string) { if (chart === null) throw new Error(`file not found: "${path}"`); return chart; },
    },
    async delete() { sb.deleted = true; },
  };
  return sb;
}

// fakeNeev mimics neev.sandboxes.create and records the params.
export function fakeNeev(sandbox: unknown) {
  const created: Record<string, any>[] = [];
  return { created, sandboxes: { async create(params: Record<string, any>) { created.push(params); return sandbox; } } };
}
