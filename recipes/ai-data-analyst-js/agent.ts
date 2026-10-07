// Agent loop for the AI data analyst recipe: the model analyses a CSV by running Python in the sandbox over MCP.

// MCP tools the model gets as-is; it writes and runs code only through run_python, never exec or fs_write directly.
export const AGENT_TOOLS = ["fs_list"] as const;
const SCRIPT = "analysis.py";
export const CHART = "chart.png";
export const MAX_OUTPUT = 6000;

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

const RUN_PYTHON: OpenAITool = {
  type: "function",
  function: {
    name: "run_python",
    description: "Runs a Python 3 script in the workspace and returns its exit code, stdout and stderr. " +
      "Each call is a fresh process: variables do not carry over, so reload the data every time.",
    parameters: { type: "object", properties: { code: { type: "string", description: "The full Python script." } }, required: ["code"] },
  },
};
const FINISH: OpenAITool = {
  type: "function",
  function: {
    name: "finish", description: "Call once chart.png is saved, with your findings.",
    parameters: { type: "object", properties: { findings: { type: "string", description: "3 to 5 short bullet points, each with concrete numbers from your outputs." } }, required: ["findings"] },
  },
};

export const SYSTEM_PROMPT =
  "You are a data analyst working in a Linux sandbox. The dataset is data.csv in the working directory. " +
  "pandas and matplotlib are installed; do not install anything, there is no internet access. " +
  "Use run_python to inspect the data first (columns, types, a few rows), then compute the answer with code. " +
  "Never guess a number: every figure you report must come from a script's output. " +
  "Save exactly one clear chart that answers the question to chart.png with matplotlib " +
  "(a title, labelled axes, readable tick labels, plt.savefig('chart.png', dpi=150, bbox_inches='tight')). " +
  "Then call finish with your findings.";
const BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); send a shorter script";
const NO_CODE = "error: run_python needs a non-empty string argument named code";
const NO_CHART = "error: chart.png is missing from the working directory; save the chart, then call finish";
const NO_FINDINGS = "error: findings are empty; call finish with your findings";

export class AgentFailed extends Error {}

// openaiTools turns the server's tool list into OpenAI tools, keeping only AGENT_TOOLS, and adds run_python and finish.
export async function openaiTools(session: SessionLike): Promise<OpenAITool[]> {
  const listed = new Map((await session.listTools()).tools.map((t) => [t.name, t]));
  const tools: OpenAITool[] = AGENT_TOOLS.filter((name) => listed.has(name)).map((name) => ({
    type: "function", function: { name, description: listed.get(name)!.description ?? "", parameters: listed.get(name)!.inputSchema },
  }));
  return [...tools, RUN_PYTHON, FINISH];
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
    out = `error: ${(e as Error).message}`; // e.g. a script that outlived the sandbox's per-call time limit
  }
  return clip(out);
}

// runPython writes the code to analysis.py with fs_write, then runs it with exec; MPLBACKEND=Agg since there is no display.
export async function runPython(session: SessionLike, code: string, signal?: AbortSignal): Promise<string> {
  const written = await callTool(session, "fs_write", { path: SCRIPT, content: code }, signal);
  if (written.startsWith("error:")) return written;
  return callTool(session, "exec", { program: "python3", args: [SCRIPT], env: ["MPLBACKEND=Agg"] }, signal);
}

// parseArgs parses tool-call arguments, or returns null if they are not a JSON object.
function parseArgs(raw: string | undefined): Record<string, unknown> | null {
  let parsed: unknown;
  try { parsed = JSON.parse(raw || "{}"); } catch { return null; }
  return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : null;
}

// echo rebuilds a tool call for the history; the server rejects invalid JSON there, so a broken call is echoed as {}.
const echo = (c: any) => ({ id: c.id, type: "function", function: { name: c.function.name, arguments: parseArgs(c.function.arguments) ? c.function.arguments : "{}" } });

// hasChart reports whether chart.png exists in the workspace yet.
async function hasChart(session: SessionLike): Promise<boolean> {
  try { return !(await session.callTool({ name: "fs_read", arguments: { path: CHART, length: 1 } })).isError; } catch { return false; }
}

// analyse runs a bounded tool-calling loop until the model finishes with chart.png saved; returns the findings.
export async function analyse(
  session: SessionLike, modelClient: ModelLike, model: string, question: string,
  opts: { maxSteps?: number; deadlineMs?: number; log?: (s: string) => void; signal?: AbortSignal } = {},
): Promise<string> {
  const { maxSteps = 20, deadlineMs = 240_000, log = console.log, signal } = opts;
  const tools = await openaiTools(session);
  const allowed = new Set(tools.map((t) => t.function.name));
  const messages: any[] = [{ role: "system", content: SYSTEM_PROMPT }, { role: "user", content: question }];
  const timeLimit = `time limit of ${deadlineMs / 1000}s reached`;
  // Every model and tool call shares one budget, so a slow reply or a hanging script cannot stretch the run.
  const budget = AbortSignal.timeout(deadlineMs);
  const bounded = signal ? AbortSignal.any([signal, budget]) : budget;
  // outOfTime turns the budget firing into the agent's own failure; Ctrl+C and real errors pass through.
  const outOfTime = (e: unknown): never => {
    if (budget.aborted && !signal?.aborted) throw new AgentFailed(timeLimit);
    throw e;
  };
  let nudged = false;
  for (let step = 1; step <= maxSteps; step++) {
    if (budget.aborted) throw new AgentFailed(timeLimit);
    let reply: any;
    try {
      reply = (await modelClient.chat.completions.create({ model, messages, tools, max_tokens: 4000 }, { signal: bounded })).choices[0].message;
    } catch (e) { outOfTime(e); }
    const calls = reply.tool_calls ?? [];
    if (calls.length === 0) {
      // One reminder covers models that describe the analysis, or report it, without the tools.
      if (nudged) throw new AgentFailed("the model kept answering without using the tools");
      nudged = true;
      messages.push({ role: "assistant", content: reply.content ?? "" });
      messages.push({ role: "user", content: "Please use the tools to run the analysis and save chart.png, then call finish." });
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
        const findings = String(args.findings ?? "").trim();
        if (findings && (await hasChart(session))) return findings;
        result = findings ? NO_CHART : NO_FINDINGS;
      } else if (!allowed.has(name)) {
        log(`   step ${step}: ${name} (refused)`);
        result = `error: ${name} is not one of your tools`;
      } else if (name === "run_python") {
        const code = args.code;
        if (typeof code !== "string" || !code.trim()) {
          log(`   step ${step}: run_python (no code)`);
          result = NO_CODE;
        } else {
          log(`   step ${step}: run_python (${code.split("\n").length} lines)`);
          try { result = await runPython(session, code, bounded); } catch (e) { outOfTime(e); }
        }
      } else {
        log(`   step ${step}: ${name} ${String(args.path ?? "")}`.trimEnd());
        try { result = await callTool(session, name, args, bounded); } catch (e) { outOfTime(e); }
      }
      messages.push({ role: "tool", tool_call_id: call.id, content: result! });
    }
  }
  throw new AgentFailed(`step limit of ${maxSteps} reached`);
}
