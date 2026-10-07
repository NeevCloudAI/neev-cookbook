// Agent loop for the best-of-N recipe: the model fixes a bug through one fork's MCP tools.

// The only MCP tools the model is given; lifecycle tools such as delete_sandbox stay with the script.
export const AGENT_TOOLS = ["fs_write", "fs_read", "fs_list", "exec"] as const;
export const MAX_OUTPUT = 4000;
const MAX_FILE = 30_000; // reads return whole source files, so the model never "repairs" a file it saw cut short

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
  function: { name: "finish", description: "Call once the tests pass. The tests are then run again to check.",
    parameters: { type: "object", properties: { summary: { type: "string" } }, required: ["summary"] } },
};

const SYSTEM_PROMPT =
  "You are fixing a bug in a small Python package inside a Linux sandbox. Work only through the tools. " +
  "The project is in the `project` directory: the package is `project/scheduler`, its unittest suite is " +
  "`project/tests`, and some tests fail. Run them with exec: program `python3`, args `[\"-m\", \"unittest\"]`, " +
  "cwd `project`. Fix the package code so every test passes. Do not edit or add test files: the original " +
  "tests are put back before your fix is checked. There is no internet access and nothing to install. " +
  "When the tests pass, call finish with a one-sentence summary of the fix.\n\nStrategy: ";
const BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); try again";

// AgentFailed means the agent loop ended without passing tests.
export class AgentFailed extends Error {}

// Verify runs the tests the script's own way and returns whether they passed, with their output.
export type Verify = (signal: AbortSignal) => Promise<[boolean, string]>;

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

// describe summarises a call's target for the progress log.
const describe = (args: Record<string, unknown>) =>
  "program" in args ? [args.program, ...((args.args as unknown[]) ?? [])].map(String).join(" ") : String(args.path ?? "");

// fixBug runs a bounded tool-calling loop until verify reports passing tests; the model's word is never enough.
// verify runs when the model calls finish or stops calling tools, and a failure goes back to the model as the tool result.
export async function fixBug(
  session: SessionLike, modelClient: ModelLike, model: string, temperature: number, hint: string, verify: Verify,
  opts: { maxSteps?: number; deadlineMs?: number; log?: (s: string) => void; signal?: AbortSignal } = {},
): Promise<string> {
  const { maxSteps = 20, deadlineMs = 240_000, log = console.log, signal } = opts;
  // Every model call, tool call and test run shares one budget, so a slow reply or a hung program cannot stretch the run.
  const budget = AbortSignal.timeout(deadlineMs);
  const bounded = signal ? AbortSignal.any([signal, budget]) : budget;
  // timedOut turns the budget firing into AgentFailed; a cancel from outside and real errors pass through.
  const timedOut = (e: unknown): never => {
    if (budget.aborted && !signal?.aborted) throw new AgentFailed(`time limit of ${deadlineMs / 1000}s reached`);
    throw e;
  };
  const tools = await openaiTools(session);
  const allowed = new Set(tools.map((t) => t.function.name));
  const messages: any[] = [{ role: "system", content: SYSTEM_PROMPT + hint }, { role: "user", content: "Find and fix the bug so that the test suite passes." }];
  let nudged = false;
  for (let step = 1; step <= maxSteps; step++) {
    let reply: any;
    try {
      bounded.throwIfAborted();
      reply = (await modelClient.chat.completions.create({ model, messages, tools, temperature, max_tokens: 4000 }, { signal: bounded })).choices[0].message;
    } catch (e) { timedOut(e); }
    const toolCalls = reply.tool_calls ?? [];
    if (toolCalls.length === 0) {
      // A text reply means the model thinks it is done: take it only if the tests agree.
      const [passed] = await verify(bounded).catch(timedOut);
      if (passed) return reply.content || "done";
      // One reminder covers models that describe the fix instead of making it.
      if (nudged) throw new AgentFailed("the model kept answering without using the tools");
      nudged = true;
      messages.push({ role: "assistant", content: reply.content ?? "" });
      messages.push({ role: "user", content: "The tests still fail. Please use the tools to fix the code, then call finish." });
      continue;
    }
    messages.push({ role: "assistant", content: reply.content ?? "", tool_calls: toolCalls.map(echo) });
    for (const call of toolCalls) {
      const name: string = call.function.name;
      const args = parseArgs(call.function.arguments);
      let result: string;
      if (!args) {
        log(`step ${step}: ${name} (invalid arguments)`);
        result = BAD_ARGUMENTS;
      } else if (name === "finish") {
        const [passed, output] = await verify(bounded).catch(timedOut);
        log(`step ${step}: finish (${passed ? "tests pass" : "tests still fail"})`);
        if (passed) return String(args.summary ?? "");
        result = clip(`error: the tests still fail:\n${output}`, MAX_OUTPUT);
      } else if (!allowed.has(name)) {
        log(`step ${step}: ${name} (refused)`);
        result = `error: ${name} is not one of your tools`;
      } else {
        log(`step ${step}: ${name} ${describe(args)}`.trimEnd());
        result = await callTool(session, name, args, bounded).catch(timedOut);
      }
      messages.push({ role: "tool", tool_call_id: call.id, content: result });
    }
  }
  throw new AgentFailed(`step limit of ${maxSteps} reached`);
}
