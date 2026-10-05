// Agent loop for the prompt-to-live-app recipe: the model works through the sandbox's MCP tools.

// The only MCP tools the model is given; lifecycle tools such as delete_sandbox stay with the script.
export const AGENT_TOOLS = ["fs_write", "fs_read", "fs_list", "exec"] as const;
export const MAX_OUTPUT = 4000;
const MAX_FILE = 30_000; // reads return whole app files, so the model never "repairs" a file it saw cut short

// SessionLike is the part of an MCP client this loop uses.
export interface SessionLike {
  listTools(): Promise<{ tools: { name: string; description?: string; inputSchema: unknown }[] }>;
  callTool(params: { name: string; arguments?: Record<string, unknown> }, resultSchema?: undefined, options?: { signal?: AbortSignal }):
    Promise<{ isError?: boolean; structuredContent?: unknown; content?: unknown }>;
}

export interface ModelLike {
  chat: { completions: { create(body: any, options?: { signal?: AbortSignal }): Promise<{ choices: { message: any }[] }> } };
}

type OpenAITool = { type: "function"; function: { name: string; description: string; parameters: unknown } };

const FINISH: OpenAITool = {
  type: "function",
  function: { name: "finish", description: "Call once the app is complete.", parameters: { type: "object", properties: { summary: { type: "string" } }, required: ["summary"] } },
};

export const SYSTEM_PROMPT =
  "You are building a small web app inside a Linux sandbox. Work only through the tools. " +
  "Write a complete single-page app as static files in the workspace root: index.html, plus " +
  "styles.css and app.js if useful. There is no internet access and no package installs, so " +
  "use plain HTML, CSS and JavaScript with no CDN links. Make it polished and fully working. " +
  "Do not start a web server: the app is served for you after you finish. " +
  "When the app is complete, call finish with a one-sentence summary.";
const BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); write smaller files";
const NO_INDEX = "error: index.html is missing from the workspace root; write it, then call finish";

export class AgentFailed extends Error {}

// openaiTools turns the server's tool list into OpenAI tools, keeping only AGENT_TOOLS, and adds finish.
export async function openaiTools(session: SessionLike): Promise<OpenAITool[]> {
  const listed = new Map((await session.listTools()).tools.map((t) => [t.name, t]));
  const tools: OpenAITool[] = AGENT_TOOLS.filter((name) => listed.has(name)).map((name) => ({
    type: "function", function: { name, description: listed.get(name)!.description ?? "", parameters: listed.get(name)!.inputSchema },
  }));
  return [...tools, FINISH];
}

// clip keeps tool output small enough for the model's context.
const clip = (s: string, limit: number) => (s.length <= limit ? s : `${s.slice(0, limit)}\n... [truncated ${s.length - limit} chars]`);

// callTool calls one MCP tool and returns the text for the model; failures become errors the model can act on.
export async function callTool(session: SessionLike, name: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<string> {
  let out: string;
  try {
    const r = await session.callTool({ name, arguments: args }, undefined, { signal });
    out = r.structuredContent !== undefined
      ? JSON.stringify(r.structuredContent)
      : (Array.isArray(r.content) ? r.content : []).map((b: any) => b.text ?? "").join("\n");
    if (r.isError) out = `error: ${out}`;
  } catch (e) {
    if (signal?.aborted) throw e;
    out = `error: ${(e as Error).message}`; // e.g. a command that outlived the sandbox's per-call time limit
  }
  return clip(out, name === "fs_read" ? MAX_FILE : MAX_OUTPUT);
}

// parseArgs parses tool-call arguments, or returns null if they are not a JSON object.
function parseArgs(raw: string | undefined): Record<string, unknown> | null {
  let parsed: unknown;
  try { parsed = JSON.parse(raw || "{}"); } catch { return null; }
  return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : null;
}

// echo rebuilds a tool call for the history; the server rejects invalid JSON there, so a broken call is echoed as {}.
const echo = (c: any) => ({ id: c.id, type: "function", function: { name: c.function.name, arguments: parseArgs(c.function.arguments) ? c.function.arguments : "{}" } });

// hasIndex reports whether the app's entry page exists yet.
async function hasIndex(session: SessionLike): Promise<boolean> {
  try { return !(await session.callTool({ name: "fs_read", arguments: { path: "index.html", length: 1 } })).isError; } catch { return false; }
}

// outOfBudget ends a run that hit a limit: serve what exists if index.html was written, otherwise fail.
async function outOfBudget(session: SessionLike, reason: string, log: (s: string) => void): Promise<string> {
  if (!(await hasIndex(session))) throw new AgentFailed(`${reason} without an index.html`);
  log(`   ${reason}; serving what the agent wrote`);
  return `${reason}; the app is what the agent wrote so far`;
}

// describe summarises a call's target for the progress log.
const describe = (args: Record<string, unknown>) =>
  "program" in args ? [args.program, ...((args.args as unknown[]) ?? [])].map(String).join(" ") : String(args.path ?? "");

// buildApp runs a bounded tool-calling loop until the model calls finish with index.html written.
export async function buildApp(
  session: SessionLike, modelClient: ModelLike, model: string, request: string,
  opts: { maxSteps?: number; deadlineMs?: number; log?: (s: string) => void; signal?: AbortSignal } = {},
): Promise<string> {
  const { maxSteps = 25, deadlineMs = 240_000, log = console.log, signal } = opts;
  const tools = await openaiTools(session);
  const allowed = new Set(tools.map((t) => t.function.name));
  const messages: any[] = [{ role: "system", content: SYSTEM_PROMPT }, { role: "user", content: request }];
  const timeLimit = `time limit of ${deadlineMs / 1000}s reached`;
  let nudged = false;
  const start = Date.now();
  for (let step = 1; step <= maxSteps; step++) {
    if (signal?.aborted) throw new AgentFailed("stopped");
    const left = deadlineMs - (Date.now() - start);
    if (left <= 0) return outOfBudget(session, timeLimit, log);
    // The model call gets only what is left of the budget, so one slow reply cannot stretch the run.
    const budget = AbortSignal.timeout(left);
    let reply: any;
    try {
      reply = (await modelClient.chat.completions.create(
        { model, messages, tools, max_tokens: 8000 },
        { signal: signal ? AbortSignal.any([signal, budget]) : budget },
      )).choices[0].message;
    } catch (e) {
      if (budget.aborted && !signal?.aborted) return outOfBudget(session, timeLimit, log);
      throw e;
    }
    const calls = reply.tool_calls ?? [];
    if (calls.length === 0) {
      // A text reply once index.html exists means the model considers the app done.
      if (await hasIndex(session)) return reply.content || "done";
      // One reminder covers models that describe the app instead of writing it.
      if (nudged) throw new AgentFailed("the model kept answering without using the tools");
      nudged = true;
      messages.push({ role: "assistant", content: reply.content ?? "" });
      messages.push({ role: "user", content: "Please use the tools to write the files, then call finish." });
      continue;
    }
    messages.push({ role: "assistant", content: reply.content ?? "", tool_calls: calls.map(echo) });
    for (const call of calls) {
      const name: string = call.function.name;
      const args = parseArgs(call.function.arguments);
      let result: string;
      if (!args) {
        log(`   step ${step}: ${name} (invalid arguments)`);
        result = BAD_ARGUMENTS;
      } else if (name === "finish") {
        log(`   step ${step}: finish`);
        if (await hasIndex(session)) return String(args.summary ?? "");
        result = NO_INDEX;
      } else if (!allowed.has(name)) {
        log(`   step ${step}: ${name} (refused)`);
        result = `error: ${name} is not one of your tools`;
      } else {
        log(`   step ${step}: ${name} ${describe(args)}`.trimEnd());
        result = await callTool(session, name, args, signal);
      }
      messages.push({ role: "tool", tool_call_id: call.id, content: result });
    }
  }
  return outOfBudget(session, `step limit of ${maxSteps} reached`, log);
}
