// test/fakes.ts: in-memory stand-ins for the sandbox's MCP session and a model client.
import type { ModelLike, SessionLike } from "../agent.ts";

// Everything the real server lists, so tests can check the model only ever sees the workspace tools.
const SERVER_TOOLS = ["exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox", "rollback_sandbox", "expose_port"];

const ok = (data: Record<string, unknown>) => ({ isError: false, structuredContent: data, content: [{ type: "text", text: JSON.stringify(data) }] });
const err = (message: string) => ({ isError: true, content: [{ type: "text", text: message }] });

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

export function fakeModel(replies: Msg[]) {
  const requests: any[] = [];
  const model: ModelLike & { requests: any[] } = {
    requests,
    chat: { completions: { async create(body: any) { requests.push(structuredClone(body)); return { choices: [{ message: replies.shift()! }] }; } } },
  };
  return model;
}

export const toolCall = (name: string, args: unknown, id = "c1"): Msg => ({
  content: null, tool_calls: [{ id, type: "function", function: { name, arguments: JSON.stringify(args) } }],
});
export const text = (content: string): Msg => ({ content });
