// Agent loop for the human-review-gate recipe: the model changes code through the sandbox's MCP tools.

// The only MCP tools the model is given; lifecycle tools such as delete_sandbox stay with the script.
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
  function: { name: "finish", description: "Call once the task is done.", parameters: { type: "object", properties: { summary: { type: "string" } }, required: ["summary"] } },
};

export const SYSTEM_PROMPT =
  "You are making a code change in a small Python project inside a Linux sandbox. Work only through " +
  "the tools. Read files with fs_read and list directories with fs_list; use exec only to run programs " +
  "(exec takes a program and an argument list, not a shell string). There is no internet access and no " +
  "package installs. Keep the change focused on the task, and run the tests before you finish. Never " +
  "copy a secret value (a password, key or token) into a file or an answer: name the variable instead. " +
  "When you are done, call finish with a plain-text summary of at most five sentences.";
const BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); try again";

// AgentFailed means the agent loop ended without doing any work in the sandbox.
export class AgentFailed extends Error {}

// Session is what the agent reported, and how many tool calls it sent to the sandbox.
export interface Session { summary: string; calls: number }

// openaiTools turns the server's tool list into OpenAI tools, keeping only AGENT_TOOLS, and adds finish.
export async function openaiTools(session: SessionLike): Promise<OpenAITool[]> {
  const listed = new Map((await session.listTools()).tools.map((t) => [t.name, t]));
  const tools: OpenAITool[] = AGENT_TOOLS.filter((name) => listed.has(name)).map((name) => ({
    type: "function", function: { name, description: listed.get(name)!.description ?? "", parameters: listed.get(name)!.inputSchema },
  }));
  return [...tools, FINISH];
}

// clip keeps tool output small enough for the model's context.
const clip = (s: string) => (s.length <= MAX_OUTPUT ? s : `${s.slice(0, MAX_OUTPUT)}\n... [truncated ${s.length - MAX_OUTPUT} chars]`);

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
  return clip(out);
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
  "program" in args ? [args.program, ...((args.args as unknown[]) ?? [])].map(String).join(" ") : String(args.path ?? "");

// runAgent runs a bounded tool-calling loop until the model calls finish, counting the calls that reached the sandbox.
export async function runAgent(
  session: SessionLike, modelClient: ModelLike, model: string, task: string,
  opts: { maxSteps?: number; deadlineMs?: number; log?: (s: string) => void; signal?: AbortSignal } = {},
): Promise<Session> {
  const { maxSteps = 20, deadlineMs = 240_000, log = console.log, signal } = opts;
  const tools = await openaiTools(session);
  const allowed = new Set(tools.map((t) => t.function.name));
  const messages: any[] = [{ role: "system", content: SYSTEM_PROMPT }, { role: "user", content: task }];
  let calls = 0;
  let acted = false; // whether the agent has written a file or run a program yet
  let nudged = false;
  // Every model and tool call shares one budget, so a slow reply or a hung program cannot stretch the run.
  const budget = AbortSignal.timeout(deadlineMs);
  const bounded = signal ? AbortSignal.any([signal, budget]) : budget;
  // outOfBudget ends a run that hit a limit: what the agent did so far still goes to review, if it did anything.
  const outOfBudget = (reason: string): Session => {
    if (calls === 0) throw new AgentFailed(`${reason} before the agent used the sandbox`);
    log(`   ${reason}; reviewing what the agent did so far`);
    return { summary: `${reason}; the agent did not call finish`, calls };
  };
  const timeLimit = `time limit of ${deadlineMs / 1000}s reached`;
  // timedOut tells the budget firing apart from Ctrl+C and real errors, which pass through.
  const timedOut = (e: unknown) => { if (budget.aborted && !signal?.aborted) return true; throw e; };
  for (let step = 1; step <= maxSteps; step++) {
    if (budget.aborted) return outOfBudget(timeLimit);
    let reply: any;
    try {
      reply = (await modelClient.chat.completions.create({ model, messages, tools, max_tokens: 4000 }, { signal: bounded })).choices[0].message;
    } catch (e) { if (timedOut(e)) return outOfBudget(timeLimit); }
    const toolCalls = reply.tool_calls ?? [];
    if (toolCalls.length === 0) {
      // A text reply once the agent has changed or run something is its summary; after reads only,
      // it is usually a plan or a reply cut off mid-thought.
      if (acted) return { summary: reply.content || "done", calls };
      // One reminder covers models that describe the change instead of making it.
      if (nudged) throw new AgentFailed("the model kept answering without using the tools");
      nudged = true;
      messages.push({ role: "assistant", content: reply.content ?? "" });
      messages.push({ role: "user", content: "Please use the tools to make the change, run the tests, then call finish." });
      continue;
    }
    messages.push({ role: "assistant", content: reply.content ?? "", tool_calls: toolCalls.map(echo) });
    for (const call of toolCalls) {
      const name: string = call.function.name;
      const args = parseArgs(call.function.arguments);
      let result: string;
      if (!args) {
        log(`   step ${step}: ${name} (invalid arguments)`);
        result = BAD_ARGUMENTS;
      } else if (name === "finish") {
        log(`   step ${step}: finish`);
        return { summary: String(args.summary ?? ""), calls };
      } else if (!allowed.has(name)) {
        log(`   step ${step}: ${name} (refused)`);
        result = `error: ${name} is not one of your tools`;
      } else {
        log(`   step ${step}: ${name} ${describe(args)}`.trimEnd());
        calls++;
        acted ||= name === "fs_write" || name === "exec";
        try { result = await callTool(session, name, args, bounded); } catch (e) { if (timedOut(e)) return outOfBudget(timeLimit); throw e; }
      }
      messages.push({ role: "tool", tool_call_id: call.id, content: result });
    }
  }
  return outOfBudget(`step limit of ${maxSteps} reached`);
}
