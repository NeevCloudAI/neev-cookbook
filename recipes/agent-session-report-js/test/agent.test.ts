import assert from "node:assert/strict";
import { test } from "node:test";
import { AGENT_TOOLS, AgentFailed, MAX_OUTPUT, callTool, openaiTools, runAgent } from "../agent.ts";
import { fakeModel, fakeSession, hangingModel, text, toolCall } from "./fakes.ts";

const run = (session: any, model: any, opts: Record<string, unknown> = {}) => runAgent(session, model, "m", "x", { log: () => {}, ...opts });

test("the model sees only the workspace tools, with the server's schemas", async () => {
  const tools = await openaiTools(fakeSession());
  const names = tools.map((t) => t.function.name);
  assert.deepEqual(names, [...AGENT_TOOLS, "finish"]);
  const fsRead = tools[names.indexOf("fs_read")].function;
  assert.equal(fsRead.description, "fs_read from the server");
  assert.deepEqual(fsRead.parameters, { type: "object", properties: { x: { type: "string" } } });
});

test("callTool renders structured content as plain JSON", async () => {
  const session = fakeSession();
  session.sandbox.fs.set(".env", "A=1");
  assert.equal(JSON.parse(await callTool(session, "fs_read", { path: ".env" })).content, "A=1");
});

test("a server refusal is an error for the model", async () => {
  const out = await callTool(fakeSession(), "fs_write", { path: "/etc/x", content: "x" });
  assert.match(out, /^error:/); assert.match(out, /escapes workspace root/);
});

test("a call that throws is an error for the model, not a crash", async () => {
  const session = fakeSession();
  session.raiseOn.set("exec", new Error("deadline_exceeded"));
  assert.match(await callTool(session, "exec", { program: "sleep", args: ["999"] }), /^error:/);
});

test("long output is clipped", async () => {
  const out = await callTool(fakeSession(undefined, "x".repeat(MAX_OUTPUT * 3)), "exec", { program: "cat" });
  assert.ok(out.length < MAX_OUTPUT + 200 && out.includes("truncated"));
});

test("the loop counts the calls that reached the sandbox and returns the summary", async () => {
  const session = fakeSession();
  session.sandbox.fs.set("README.md", "# demo");
  const model = fakeModel([toolCall("fs_read", { path: "README.md" }), toolCall("exec", { program: "python3", args: ["app.py"] }, "c2"),
    toolCall("finish", { summary: "port 8080, one secret missing" }, "c3")]);
  const result = await run(session, model);
  assert.equal(result.summary, "port 8080, one secret missing");
  assert.equal(result.calls, 2);
  assert.equal(model.requests[1].messages.at(-1).role, "tool");
});

test("refused and malformed calls do not count as sandbox calls", async () => {
  const bad = toolCall("fs_write", {});
  bad.tool_calls![0].function.arguments = '{"path": "a", "content": "cut of';
  const model = fakeModel([toolCall("delete_sandbox", {}), bad, toolCall("fs_list", {}, "c3"), toolCall("finish", { summary: "ok" }, "c4")]);
  const session = fakeSession();
  const result = await run(session, model);
  assert.equal(result.calls, 1);
  assert.deepEqual(session.calls.map(([n]) => n), ["fs_list"]);
  assert.match(model.requests[1].messages.at(-1).content, /^error:/);
  // the history sent back must itself be valid JSON, or the model server rejects the next request
  assert.deepEqual(JSON.parse(model.requests[2].messages.at(-2).tool_calls[0].function.arguments), {});
});

test("the step limit after some work ends the session so it can be reported", async () => {
  const lines: string[] = [];
  const model = fakeModel([toolCall("fs_list", {}), toolCall("fs_list", {}, "c2"), toolCall("fs_list", {}, "c3")]);
  const result = await run(fakeSession(), model, { maxSteps: 3, log: (l: string) => lines.push(l) });
  assert.equal(result.calls, 3);
  assert.match(result.summary, /step limit/);
  assert.ok(lines.some((l) => l.includes("step limit")));
});

test("the step limit without any sandbox call fails", async () => {
  const model = fakeModel([toolCall("delete_sandbox", {}), toolCall("delete_sandbox", {}, "c2")]);
  await assert.rejects(run(fakeSession(), model, { maxSteps: 2 }), (e: Error) => e instanceof AgentFailed && /step limit/.test(e.message));
});

test("a text reply after some work is the summary", async () => {
  const model = fakeModel([toolCall("fs_read", { path: "README.md" }), text("Port 8080; SMTP_PASSWORD is missing.")]);
  const session = fakeSession();
  session.sandbox.fs.set("README.md", "# demo");
  assert.deepEqual(await run(session, model), { summary: "Port 8080; SMTP_PASSWORD is missing.", calls: 1 });
});

test("the loop nudges once, then fails on text only", async () => {
  const model = fakeModel([text("I would read the README..."), text("Done.")]);
  await assert.rejects(run(fakeSession(), model), /without using the tools/);
  assert.match(model.requests[1].messages.at(-1).content, /use the tools/);
});

test("the deadline cuts a hung tool call and reports what was done", async () => {
  const session = fakeSession();
  const callToolReal = session.callTool;
  // exec never answers on its own; it only stops when the signal it was given fires.
  session.callTool = (params, schema, options) => params.name !== "exec" ? callToolReal(params, schema, options)
    : new Promise((_, reject) => options?.signal?.addEventListener("abort", () => reject(options.signal!.reason), { once: true }));
  const model = fakeModel([toolCall("fs_list", {}), toolCall("exec", { program: "python3" }, "c2")]);
  const t = Date.now();
  const result = await run(session, model, { deadlineMs: 300 });
  assert.match(result.summary, /time limit/);
  assert.equal(result.calls, 2);
  assert.ok(Date.now() - t < 5000);
});

test("the deadline cuts a slow model call", async () => {
  const t = Date.now();
  await assert.rejects(runAgent(fakeSession(), hangingModel, "m", "x", { deadlineMs: 200, log: () => {} }), /time limit/);
  assert.ok(Date.now() - t < 5000);
});
