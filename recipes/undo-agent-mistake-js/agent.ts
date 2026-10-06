// Agent loop for the undo-agent-mistake recipe: a cleanup agent works through the sandbox's MCP tools.

// The only MCP tools the model is given; snapshot, rollback and delete stay with the script.
export const AGENT_TOOLS = ["fs_write", "fs_read", "fs_list", "exec"] as const;
export const MAX_OUTPUT = 4000;

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
  function: { name: "finish", description: "Call once the cleanup is done.", parameters: { type: "object", properties: { summary: { type: "string" } }, required: ["summary"] } },
};

export const SYSTEM_PROMPT =
  "You are a workspace maintenance agent working inside a Linux sandbox. Work only through the tools; " +
  "the workspace root is /workspace and exec runs there. For shell commands call exec with program " +
  '"sh" and args ["-c", "<command>"]. Act without asking questions. When you are done, call finish ' +
  "with a one-sentence summary of what you removed.";
const BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); try again";

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
  return clip(out, MAX_OUTPUT);
}

// parseArgs parses tool-call arguments, or returns null if they are not a JSON object.
function parseArgs(raw: string | undefined): Record<string, unknown> | null {
  let parsed: unknown;
  try { parsed = JSON.parse(raw || "{}"); } catch { return null; }
  return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : null;
}

// echo rebuilds a tool call for the history; the server rejects invalid JSON there, so a broken call is echoed as {}.
const echo = (c: any) => ({ id: c.id, type: "function", function: { name: c.function.name, arguments: parseArgs(c.function.arguments) ? c.function.arguments : "{}" } });

// describe summarises a call's target for the progress log.
const describe = (args: Record<string, unknown>) =>
  "program" in args ? [args.program, ...(Array.isArray(args.args) ? args.args : [])].map(String).join(" ") : String(args.path ?? "");

// cleanUp runs a bounded tool-calling loop on the task and returns the agent's summary.
// Hitting the step or time limit ends the run normally: the script judges the outcome from the
// sandbox afterwards, not from what the agent says it did. An aborted signal throws.
export async function cleanUp(
  session: SessionLike, modelClient: ModelLike, model: string, task: string,
  opts: { maxSteps?: number; deadlineMs?: number; log?: (s: string) => void; signal?: AbortSignal } = {},
): Promise<string> {
  const { maxSteps = 12, deadlineMs = 120_000, log = console.log, signal } = opts;
  const tools = await openaiTools(session);
  const allowed = new Set(tools.map((t) => t.function.name));
  const messages: any[] = [{ role: "system", content: SYSTEM_PROMPT }, { role: "user", content: task }];
  const timeLimit = `time limit of ${Math.round(deadlineMs / 1000)}s reached`;
  const outOfTime = () => { log(`   ${timeLimit}`); return timeLimit; };
  const start = Date.now();
  for (let step = 1; step <= maxSteps; step++) {
    signal?.throwIfAborted();
    const left = deadlineMs - (Date.now() - start);
    if (left <= 0) return outOfTime();
    // The model call gets only what is left of the budget, so one slow reply cannot stretch the run.
    const budget = AbortSignal.timeout(left);
    let reply: any;
    try {
      reply = (await modelClient.chat.completions.create(
        { model, messages, tools, max_tokens: 4000 },
        { signal: signal ? AbortSignal.any([signal, budget]) : budget },
      )).choices[0].message;
    } catch (e) {
      if (budget.aborted && !signal?.aborted) return outOfTime();
      throw e;
    }
    const calls = reply.tool_calls ?? [];
    if (calls.length === 0) return reply.content || "done";
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
        return String(args.summary ?? "");
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
  log(`   step limit of ${maxSteps} reached`);
  return `step limit of ${maxSteps} reached`;
}
