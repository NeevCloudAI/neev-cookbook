// test/fakes.ts: in-memory stand-ins for the sandbox's MCP session, the sandbox SDK handle and a model client.
import type { ModelLike, SessionLike } from "../agent.ts";

// Everything the real server lists, so tests can check the model only ever sees the tools it is given.
const SERVER_TOOLS = ["exec", "fs_read", "fs_write", "fs_list", "interpreter_run", "process_start", "delete_sandbox",
  "rollback_sandbox", "expose_port"];

const ok = (data: Record<string, unknown>) => ({ isError: false, structuredContent: data, content: [{ type: "text", text: JSON.stringify(data) }] });
const err = (message: string) => ({ isError: true, content: [{ type: "text", text: message }] });

type Cell = { stdout: string; stderr: string; results: Record<string, string>[]; error?: unknown; end_reason: string; generation?: string };
type OnRun = (files: Map<string, string>, code: string) => Cell;

// savesChart is the default kernel behaviour: a cell that calls savefig writes chart.png, and its prints come back.
export const savesChart: OnRun = (files, code) => {
  if (code.includes("savefig")) files.set("chart.png", "\x89PNG...");
  return { stdout: "ok\n", stderr: "", results: [], end_reason: "ok", generation: "g1" };
};

// fakeSession mimics an MCP session bound to one sandbox: fs_* backed by a map, interpreter_run runs onRun on the code.
export function fakeSession(onRun: OnRun = savesChart) {
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
      if (name === "fs_read") {
        const v = files.get(args.path);
        return v === undefined ? err(`the sandbox refused this call: not_found: file not found: "${args.path}"`) : ok({ content: v, size: v.length, eof: true });
      }
      if (name === "fs_list") return ok({ entries: [...files.keys()].sort().map((n) => ({ name: n, type: "file" })) });
      if (name === "interpreter_run") {
        // The real schema sets additionalProperties: false.
        if (Object.keys(args).some((k) => k !== "code" && k !== "context_id")) return err(`invalid params: ${Object.keys(args)}`);
        return ok(onRun(files, args.code));
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

// fakeSandbox records uploads and deletion; chart.png reads back as `chart`.
export function fakeSandbox(opts: { chart?: Uint8Array | null } = {}) {
  const { chart = PNG } = opts;
  const sb = {
    name: "data-analyst-1", uploads: [] as [string, string][], deleted: false,
    async waitUntilReady() { return sb; },
    files: {
      async uploadFile(local: string, remote: string) { sb.uploads.push([local, remote]); return { bytes_written: 1 }; },
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
