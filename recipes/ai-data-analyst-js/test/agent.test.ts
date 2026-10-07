import assert from "node:assert/strict";
import { test } from "node:test";
import { AGENT_TOOLS, AgentFailed, MAX_OUTPUT, analyse, callTool, openaiTools, runPython } from "../agent.ts";
import { fakeModel, fakeSession, hangingModel, text, toolCall } from "./fakes.ts";

const CHART_CODE = "import matplotlib.pyplot as plt\nplt.plot([1]); plt.savefig('chart.png')";

const run = (session: any, model: any, opts: Record<string, unknown> = {}) => analyse(session, model, "m", "q", { log: () => {}, ...opts });

test("the model sees fs_list plus run_python and finish", async () => {
  const tools = await openaiTools(fakeSession());
  const names = tools.map((t) => t.function.name);
  assert.deepEqual(names, [...AGENT_TOOLS, "run_python", "finish"]);
  assert.ok(!names.includes("exec") && !names.includes("fs_write") && !names.includes("delete_sandbox"));
  assert.equal(tools[0].function.description, "fs_list from the server");
});

test("run_python writes the script and runs it with python3, headless", async () => {
  const session = fakeSession();
  const out = await runPython(session, CHART_CODE);
  assert.equal(session.files.get("analysis.py"), CHART_CODE);
  const [name, args] = session.calls.at(-1)!;
  assert.equal(name, "exec");
  assert.equal(args.program, "python3");
  assert.deepEqual(args.args, ["analysis.py"]);
  assert.ok((args.env as string[]).includes("MPLBACKEND=Agg"));
  assert.equal(JSON.parse(out).exit_code, 0);
  assert.ok(session.files.has("chart.png"));
});

test("run_python reports a failing script with its traceback", async () => {
  const session = fakeSession(() => ({ exit_code: 1, stdout: "", stderr: "KeyError: 'sales'" }));
  const out = JSON.parse(await runPython(session, "df['sales']"));
  assert.equal(out.exit_code, 1);
  assert.match(out.stderr, /KeyError/);
});

test("run_python stops when the script cannot be written", async () => {
  const session = fakeSession();
  session.raiseOn.set("fs_write", new Error("stream closed"));
  assert.match(await runPython(session, "print(1)"), /^error:/);
  assert.deepEqual(session.calls.map(([n]) => n), ["fs_write"]);
});

test("long output is clipped, and a throwing call is an error, not a crash", async () => {
  const session = fakeSession(() => ({ exit_code: 0, stdout: "x".repeat(MAX_OUTPUT * 3), stderr: "" }));
  const out = await runPython(session, "print('x' * 99999)");
  assert.ok(out.length < MAX_OUTPUT + 200 && out.includes("truncated"));
  session.raiseOn.set("fs_list", new Error("deadline_exceeded"));
  assert.match(await callTool(session, "fs_list", {}), /^error:/);
});

test("the loop runs code, saves the chart and returns the findings", async () => {
  const session = fakeSession();
  const model = fakeModel([toolCall("run_python", { code: CHART_CODE }), toolCall("finish", { findings: "Mumbai leads revenue." }, "c2")]);
  assert.equal(await run(session, model), "Mumbai leads revenue.");
  assert.equal(model.requests[1].messages.at(-1).role, "tool");
  assert.ok(session.files.has("chart.png"));
});

test("finish without a chart is an error the model can fix", async () => {
  const model = fakeModel([toolCall("finish", { findings: "early" }), toolCall("run_python", { code: CHART_CODE }, "c2"), toolCall("finish", { findings: "done" }, "c3")]);
  assert.equal(await run(fakeSession(), model), "done");
  assert.match(model.requests[1].messages.at(-1).content, /chart\.png/);
});

test("finish with empty findings is an error the model can fix", async () => {
  const model = fakeModel([toolCall("run_python", { code: CHART_CODE }), toolCall("finish", { findings: "  " }, "c2"), toolCall("finish", { findings: "real findings" }, "c3")]);
  assert.equal(await run(fakeSession(), model), "real findings");
  assert.match(model.requests[2].messages.at(-1).content, /^error:/);
});

test("the loop refuses tools the model was not given", async () => {
  const session = fakeSession();
  const model = fakeModel([toolCall("exec", { program: "curl" }), toolCall("delete_sandbox", {}, "c2"),
    toolCall("run_python", { code: CHART_CODE }, "c3"), toolCall("finish", { findings: "ok" }, "c4")]);
  assert.equal(await run(session, model), "ok");
  assert.ok(!session.calls.some(([n, a]) => n === "exec" && a.program === "curl"));
  assert.ok(!session.calls.some(([n]) => n === "delete_sandbox"));
  assert.match(model.requests[1].messages.at(-1).content, /^error:/);
});

test("malformed arguments are explained and echoed as valid JSON", async () => {
  const bad = toolCall("run_python", {});
  bad.tool_calls![0].function.arguments = '{"code": "import pandas as pd\\ndf = pd.read_csv(';
  const model = fakeModel([bad, toolCall("run_python", { code: CHART_CODE }, "c2"), toolCall("finish", { findings: "ok" }, "c3")]);
  assert.equal(await run(fakeSession(), model), "ok");
  assert.match(model.requests[1].messages.at(-1).content, /not valid JSON/);
  assert.deepEqual(JSON.parse(model.requests[1].messages.at(-2).tool_calls[0].function.arguments), {});
});

test("run_python without code is an error, not a crash", async () => {
  const model = fakeModel([toolCall("run_python", { source: "print(1)" }), toolCall("run_python", { code: CHART_CODE }, "c2"), toolCall("finish", { findings: "ok" }, "c3")]);
  assert.equal(await run(fakeSession(), model), "ok");
  assert.match(model.requests[1].messages.at(-1).content, /code/);
});

test("a text reply after the chart is nudged towards finish", async () => {
  const model = fakeModel([toolCall("run_python", { code: CHART_CODE }), text("Next I'll compute the lift."), toolCall("finish", { findings: "Pune grew fastest." }, "c2")]);
  assert.equal(await run(fakeSession(), model), "Pune grew fastest.");
  assert.match(model.requests[2].messages.at(-1).content, /call finish/);
});

test("the loop nudges once, then fails on text only", async () => {
  const model = fakeModel([text("I would use pandas..."), text("Done.")]);
  await assert.rejects(run(fakeSession(), model), (e: Error) => e instanceof AgentFailed && /without using the tools/.test(e.message));
  assert.match(model.requests[1].messages.at(-1).content, /use the tools/);
});

test("the step limit fails even when a chart exists", async () => {
  const session = fakeSession();
  session.files.set("chart.png", "\x89PNG");
  const model = fakeModel([toolCall("fs_list", {}), toolCall("fs_list", {}, "c2"), toolCall("fs_list", {}, "c3")]);
  await assert.rejects(run(session, model, { maxSteps: 3 }), /step limit of 3/);
});

test("every tool call is bounded by the time left", async () => {
  const session = fakeSession();
  const model = fakeModel([toolCall("fs_list", {}), toolCall("run_python", { code: CHART_CODE }, "c2"), toolCall("finish", { findings: "ok" }, "c3")]);
  assert.equal(await run(session, model, { deadlineMs: 60_000 }), "ok");
  const bounded = session.calls.map(([n], i) => [n, session.signals[i]] as const).filter(([n]) => ["fs_list", "fs_write", "exec"].includes(n));
  assert.equal(bounded.length, 3);
  assert.ok(bounded.every(([, s]) => s instanceof AbortSignal));
});

test("a hanging script is cut at the deadline", async () => {
  const session = fakeSession();
  const callTool = session.callTool;
  // exec never answers on its own; it only stops when the signal it was given fires.
  session.callTool = (params, schema, options) => params.name !== "exec" ? callTool(params, schema, options)
    : new Promise((_, reject) => options?.signal?.addEventListener("abort", () => reject(options.signal!.reason), { once: true }));
  const model = fakeModel([toolCall("run_python", { code: "while True: pass" }), toolCall("run_python", { code: "while True: pass" }, "c2")]);
  const t = Date.now();
  await assert.rejects(run(session, model, { deadlineMs: 300 }), /time limit/);
  assert.ok(Date.now() - t < 5000);
});

test("the deadline cuts a slow model call", async () => {
  const t = Date.now();
  await assert.rejects(analyse(fakeSession(), hangingModel, "m", "q", { deadlineMs: 200, log: () => {} }), /time limit/);
  assert.ok(Date.now() - t < 5000);
});
