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
    chat: { completions: { async create(body: any) {
      requests.push(structuredClone(body));
      if (body.stream !== true) throw new Error("expected stream: true");
      return streamOf(replies.shift()!);
    } } },
  };
  return model;
}

// streamOf streams msg as chunks, splitting text and each tool call's arguments in two like a real reply.
async function* streamOf(msg: Msg) {
  const chunk = (delta: any) => ({ choices: [{ delta }] });
  if (msg.content) {
    const half = Math.floor(msg.content.length / 2);
    yield chunk({ content: msg.content.slice(0, half) });
    yield chunk({ content: msg.content.slice(half) });
  }
  for (const [index, call] of (msg.tool_calls ?? []).entries()) {
    const args = call.function.arguments, half = Math.floor(args.length / 2);
    yield chunk({ tool_calls: [{ index, id: call.id, function: { name: call.function.name, arguments: args.slice(0, half) } }] });
    yield chunk({ tool_calls: [{ index, function: { arguments: args.slice(half) } }] });
  }
}

export const toolCall = (name: string, args: unknown, id = "c1"): Msg => ({
  content: null, tool_calls: [{ id, type: "function", function: { name, arguments: JSON.stringify(args) } }],
});
export const text = (content: string): Msg => ({ content });
