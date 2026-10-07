import assert from "node:assert/strict";
import { test } from "node:test";
import { AGENT_TOOLS, AgentFailed, MAX_OUTPUT, callTool, fixBug, openaiTools } from "../agent.ts";
import { FIXED, fakeModel, fakeSession, runTests, text, toolCall, type FakeSession } from "./fakes.ts";

const BUGGY = "merged[-1][1] = end";

// project is a session holding the buggy package, as a fork starts out.
const project = () => fakeSession(new Map([["project/scheduler/intervals.py", BUGGY], ["project/tests/test_slots.py", "tests"]]));

// verifier checks the session's files the way the script does, counting how often it ran.
function verifier(session: FakeSession) {
  const verify = async (): Promise<[boolean, string]> => {
    verify.runs++;
    const out = runTests(session.files, "python3");
    return [out.exit_code === 0, out.stderr];
  };
  verify.runs = 0;
  return verify;
}

const fix = (session: FakeSession, model: any, opts: Record<string, any> = {}) =>
  fixBug(session, model, "m", 0.2, "hint", opts.verify ?? verifier(session), { log: () => {}, ...opts });
const model = (...replies: any[]) => fakeModel({ 0.2: replies });
const WRITE_FIX = (id = "c2") => toolCall("fs_write", { path: "project/scheduler/intervals.py", content: FIXED }, id);

test("the model sees only the workspace tools, with the server's schemas", async () => {
  const tools = await openaiTools(fakeSession());
  const names = tools.map((t) => t.function.name);
  assert.deepEqual(names, [...AGENT_TOOLS, "finish"]);
  assert.equal(tools[names.indexOf("exec")].function.description, "exec from the server");
});

test("a server refusal or a thrown call is an error for the model, not a crash", async () => {
  const session = fakeSession();
  assert.match(await callTool(session, "fs_write", { path: "/etc/x", content: "x" }), /escapes workspace root/);
  session.raiseOn.set("exec", new Error("deadline_exceeded"));
  assert.match(await callTool(session, "exec", { program: "sleep" }), /^error:/);
});

test("long output is clipped, but whole source files are returned", async () => {
  const session = fakeSession(new Map([["x".repeat(MAX_OUTPUT * 3), ""], ["big.py", "y".repeat(MAX_OUTPUT * 3)]]));
  const listed = await callTool(session, "fs_list", {});
  assert.ok(listed.length < MAX_OUTPUT + 200 && listed.includes("truncated"));
  assert.ok(!(await callTool(session, "fs_read", { path: "big.py" })).includes("truncated"));
});

test("fix then finish returns once the script sees the tests pass", async () => {
  const m = model(toolCall("exec", { program: "python3", args: ["-m", "unittest"], cwd: "project" }), WRITE_FIX(),
    toolCall("finish", { summary: "use max for the end" }, "c3"));
  assert.equal(await fix(project(), m), "use max for the end");
  assert.equal(m.requests[0].temperature, 0.2);
  assert.match(m.requests[0].messages[0].content, /Strategy: hint/);
});

test("finish with failing tests hands the output back and continues", async () => {
  const session = project();
  const verify = verifier(session);
  const m = model(toolCall("finish", { summary: "done?" }), WRITE_FIX(), toolCall("finish", { summary: "fixed" }, "c3"));
  assert.equal(await fix(session, m, { verify }), "fixed");
  assert.equal(verify.runs, 2);
  const back = m.requests[1].messages.at(-1).content;
  assert.match(back, /^error: the tests still fail/); assert.match(back, /FAILED \(failures=3\)/);
});

test("a text reply with passing tests counts as finished", async () => {
  assert.equal(await fix(project(), model(WRITE_FIX("c1"), text("All fixed."))), "All fixed.");
});

test("text replies with failing tests get one reminder, then fail", async () => {
  const m = model(text("The bug is in merge."), text("Use max()."));
  await assert.rejects(fix(project(), m), (e: Error) => e instanceof AgentFailed && /without using the tools/.test(e.message));
  assert.match(m.requests[1].messages.at(-1).content, /use the tools/);
});

test("the loop refuses tools the model was not given", async () => {
  const session = project();
  const m = model(toolCall("delete_sandbox", {}), WRITE_FIX(), toolCall("finish", { summary: "ok" }, "c3"));
  assert.equal(await fix(session, m), "ok");
  assert.ok(!session.calls.some(([n]) => n === "delete_sandbox"));
  assert.equal(m.requests[1].messages.at(-1).content, "error: delete_sandbox is not one of your tools");
});

test("malformed arguments are explained and echoed back as valid JSON", async () => {
  const bad = toolCall("fs_write", {});
  bad.tool_calls![0].function.arguments = '{"path": "project/scheduler/intervals.py", "content": "cut of';
  const m = model(bad, WRITE_FIX(), toolCall("finish", { summary: "ok" }, "c3"));
  assert.equal(await fix(project(), m), "ok");
  assert.match(m.requests[1].messages.at(-1).content, /not valid JSON/);
  assert.deepEqual(JSON.parse(m.requests[1].messages.at(-2).tool_calls[0].function.arguments), {});
});

test("the loop stops at the step limit", async () => {
  const m = model(toolCall("fs_list", {}), toolCall("fs_list", {}), toolCall("fs_list", {}));
  await assert.rejects(fix(project(), m, { maxSteps: 3 }), /step limit of 3/);
});

test("the deadline cuts a slow model call", async () => {
  const slow = fakeModel({ 0.2: [text("late")] }, { 0.2: 30_000 });
  const t = Date.now();
  await assert.rejects(fix(project(), slow, { deadlineMs: 200 }), (e: Error) => e instanceof AgentFailed && /time limit/.test(e.message));
  assert.ok(Date.now() - t < 5000);
});

test("the deadline cuts a hung tool call", async () => {
  const session = project();
  session.hangOn.add("exec");
  const t = Date.now();
  await assert.rejects(fix(session, model(toolCall("exec", { program: "python3" })), { deadlineMs: 200 }), /time limit/);
  assert.ok(Date.now() - t < 5000);
});

test("a cancel from outside passes through instead of becoming a failure", async () => {
  const ac = new AbortController();
  const slow = fakeModel({ 0.2: [text("late")] }, { 0.2: 30_000 });
  const run = fix(project(), slow, { signal: ac.signal });
  ac.abort();
  await assert.rejects(run, (e: Error) => !(e instanceof AgentFailed));
});
