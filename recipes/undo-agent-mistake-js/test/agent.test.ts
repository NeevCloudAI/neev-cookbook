import assert from "node:assert/strict";
import { test } from "node:test";
import { AGENT_TOOLS, callTool, cleanUp, MAX_OUTPUT, openaiTools } from "../agent.ts";
import { FakeSandbox, fakeModel, fakeSession, hangingModel, shell, text, toolCall } from "./fakes.ts";

const quiet = { log: () => {} };

// seededSession is an MCP session on a sandbox whose data has been seeded.
async function seededSession() {
  const sandbox = new FakeSandbox();
  await sandbox.exec(["python3", "seed.py"]);
  return fakeSession(sandbox);
}

test("the model sees only the workspace tools, with the server's schemas", async () => {
  const tools = await openaiTools(fakeSession());
  const names = tools.map((t) => t.function.name);
  assert.deepEqual(names, [...AGENT_TOOLS, "finish"]);
  assert.ok(!names.includes("rollback_sandbox") && !names.includes("create_snapshot"));
  const exec = tools[names.indexOf("exec")].function;
  assert.equal(exec.description, "exec from the server");
  assert.deepEqual(exec.parameters, { type: "object", properties: { x: { type: "string" } } });
});

test("the agent runs its commands in the sandbox and finishes", async () => {
  const session = await seededSession(); const lines: string[] = [];
  const model = fakeModel([shell("rm -rf data"), toolCall("finish", { summary: "freed space" }, "c2")]);
  assert.equal(await cleanUp(session, model, "m", "clean up", { log: (l) => lines.push(l) }), "freed space");
  assert.ok(!session.sandbox.workspace.has("data/customers.csv"));
  assert.ok(lines.some((l) => l.includes("exec sh -c rm -rf data")));
  assert.equal(model.requests[1].messages.at(-1).role, "tool");
});

test("a text reply ends the run", async () => {
  assert.equal(await cleanUp(await seededSession(), fakeModel([text("Nothing to clean up.")]), "m", "x", quiet), "Nothing to clean up.");
});

test("the agent cannot call tools it was not given", async () => {
  const session = await seededSession();
  const model = fakeModel([toolCall("rollback_sandbox", {}), toolCall("finish", { summary: "ok" }, "c2")]);
  assert.equal(await cleanUp(session, model, "m", "x", quiet), "ok");
  assert.ok(!session.calls.some(([name]) => name === "rollback_sandbox"));
  assert.ok(model.requests[1].messages.at(-1).content.startsWith("error:"));
});

test("the step limit ends the run without throwing", async () => {
  const lines: string[] = [];
  const model = fakeModel(Array.from({ length: 3 }, () => toolCall("fs_list", {})));
  const summary = await cleanUp(await seededSession(), model, "m", "x", { maxSteps: 3, log: (l) => lines.push(l) });
  assert.match(summary, /step limit/);
  assert.ok(lines.some((l) => l.includes("step limit")));
});

test("malformed arguments are explained and echoed as valid JSON", async () => {
  const bad = toolCall("exec", {});
  bad.tool_calls![0].function.arguments = '{"program": "sh", "args": ["-c", "rm';
  const model = fakeModel([bad, toolCall("finish", { summary: "ok" }, "c2")]);
  assert.equal(await cleanUp(await seededSession(), model, "m", "x", quiet), "ok");
  assert.match(model.requests[1].messages.at(-1).content, /not valid JSON/);
  // the history sent back must itself be valid JSON, or the server rejects the next request
  assert.deepEqual(JSON.parse(model.requests[1].messages.at(-2).tool_calls[0].function.arguments), {});
});

test("server refusals and thrown calls are errors for the model", async () => {
  const session = fakeSession();
  const out = await callTool(session, "fs_write", { path: "/etc/x", content: "x" });
  assert.ok(out.startsWith("error:") && out.includes("escapes workspace root"));
  session.raiseOn.set("exec", new Error("deadline_exceeded"));
  assert.ok((await callTool(session, "exec", { program: "sleep" })).startsWith("error:"));
});

test("long output is clipped", async () => {
  const out = await callTool(fakeSession(new FakeSandbox(), "x".repeat(MAX_OUTPUT * 3)), "exec", { program: "du" });
  assert.ok(out.length < MAX_OUTPUT + 200 && out.includes("truncated"));
});

test("the deadline cuts a slow model call", async () => {
  const t = Date.now();
  assert.match(await cleanUp(await seededSession(), hangingModel, "m", "x", { ...quiet, deadlineMs: 200 }), /time limit/);
  assert.ok(Date.now() - t < 5000);
});

test("an abort stops a slow model call with an error, not a summary", async () => {
  const ac = new AbortController();
  setTimeout(() => ac.abort(), 20);
  const t = Date.now();
  await assert.rejects(cleanUp(await seededSession(), hangingModel, "m", "x", { ...quiet, signal: ac.signal }));
  assert.ok(Date.now() - t < 5000);
});

test("exec args that are not a list do not crash the run", async () => {
  const lines: string[] = [];
  const model = fakeModel([toolCall("exec", { program: "ls", args: {} }), toolCall("finish", { summary: "ok" }, "c2")]);
  assert.equal(await cleanUp(await seededSession(), model, "m", "x", { log: (l) => lines.push(l) }), "ok");
  assert.ok(lines.includes("   step 1: exec ls"));
});
