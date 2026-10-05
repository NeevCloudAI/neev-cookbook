import assert from "node:assert/strict";
import { test } from "node:test";
import { AGENT_TOOLS, AgentFailed, buildApp, callTool, MAX_OUTPUT, openaiTools } from "../agent.ts";
import { fakeModel, fakeSession, text, toolCall } from "./fakes.ts";

const quiet = { log: () => {} };

test("the model sees only the workspace tools, with the server's schemas", async () => {
  const tools = await openaiTools(fakeSession());
  const names = tools.map((t) => t.function.name);
  assert.deepEqual(names, [...AGENT_TOOLS, "finish"]);
  const fsWrite = tools[names.indexOf("fs_write")].function;
  assert.equal(fsWrite.description, "fs_write from the server");
  assert.deepEqual(fsWrite.parameters, { type: "object", properties: { x: { type: "string" } } });
});

test("callTool renders structured content as plain JSON", async () => {
  const s = fakeSession();
  await callTool(s, "fs_write", { path: "index.html", content: "<h1>hi</h1>" });
  assert.equal(JSON.parse(await callTool(s, "fs_read", { path: "index.html" })).content, "<h1>hi</h1>");
});

test("a server refusal is an error for the model", async () => {
  const out = await callTool(fakeSession(), "fs_write", { path: "/etc/x", content: "x" });
  assert.ok(out.startsWith("error:") && out.includes("escapes workspace root"));
});

test("a call that throws is an error for the model, not a crash", async () => {
  const s = fakeSession();
  s.raiseOn.set("exec", new Error("deadline_exceeded"));
  assert.ok((await callTool(s, "exec", { program: "sleep", args: ["999"] })).startsWith("error:"));
});

test("reads keep whole files and other output is clipped", async () => {
  const s = fakeSession("x".repeat(MAX_OUTPUT * 3));
  s.files.set("app.js", "y".repeat(12_000));
  assert.ok((await callTool(s, "fs_read", { path: "app.js" })).includes("y".repeat(12_000)));
  const out = await callTool(s, "exec", { program: "cat" });
  assert.ok(out.length < MAX_OUTPUT + 200 && out.includes("truncated"));
});

test("loop writes files then finishes", async () => {
  const s = fakeSession();
  const model = fakeModel([toolCall("fs_write", { path: "index.html", content: "<h1>todo</h1>" }), toolCall("finish", { summary: "todo app" }, "c2")]);
  assert.equal(await buildApp(s, model, "m", "a todo app", quiet), "todo app");
  assert.equal(s.files.get("index.html"), "<h1>todo</h1>");
  assert.equal(model.requests[1].messages.at(-1).role, "tool");
});

test("loop refuses tools the model was not given", async () => {
  const s = fakeSession();
  const model = fakeModel([toolCall("delete_sandbox", {}), toolCall("fs_write", { path: "index.html", content: "x" }, "c2"), toolCall("finish", { summary: "ok" }, "c3")]);
  assert.equal(await buildApp(s, model, "m", "x", quiet), "ok");
  assert.ok(!s.calls.some(([name]) => name === "delete_sandbox"));
  assert.ok(model.requests[1].messages.at(-1).content.startsWith("error:"));
});

test("loop stops at the step limit without index.html", async () => {
  const model = fakeModel(Array.from({ length: 3 }, () => toolCall("fs_list", {})));
  await assert.rejects(buildApp(fakeSession(), model, "m", "x", { ...quiet, maxSteps: 3 }), /step limit/);
});

test("hitting a limit with index.html serves what was written", async () => {
  const s = fakeSession(); const lines: string[] = [];
  s.files.set("index.html", "<h1>almost done</h1>");
  const summary = await buildApp(s, fakeModel(Array.from({ length: 2 }, () => toolCall("fs_list", {}))), "m", "x", { maxSteps: 2, log: (l) => lines.push(l) });
  assert.match(summary, /step limit/);
  assert.ok(lines.some((l) => l.includes("serving what the agent wrote")));
});

test("loop nudges once, then fails on text-only replies", async () => {
  const model = fakeModel([text("Sure! Here is HTML..."), text("Done.")]);
  await assert.rejects(buildApp(fakeSession(), model, "m", "x", quiet), (e: unknown) => e instanceof AgentFailed && /without using the tools/.test(e.message));
  assert.match(model.requests[1].messages.at(-1).content, /use the tools/);
});

test("loop treats a text reply after index.html as finished", async () => {
  const model = fakeModel([text("hmm"), toolCall("fs_write", { path: "index.html", content: "x" }), text("The app is complete!")]);
  assert.equal(await buildApp(fakeSession(), model, "m", "x", quiet), "The app is complete!");
});

test("finish without index.html is a tool error the model can fix", async () => {
  const model = fakeModel([toolCall("finish", { summary: "early" }), toolCall("fs_write", { path: "index.html", content: "x" }, "c2"), toolCall("finish", { summary: "done" }, "c3")]);
  assert.equal(await buildApp(fakeSession(), model, "m", "x", quiet), "done");
  assert.match(model.requests[1].messages.at(-1).content, /index.html/);
});

test("loop explains malformed arguments and echoes them as valid JSON", async () => {
  const bad = toolCall("fs_write", {});
  bad.tool_calls![0].function.arguments = '{"path": "index.html", "content": "<h1>cut of';
  const model = fakeModel([bad, toolCall("fs_write", { path: "index.html", content: "x" }, "c2"), toolCall("finish", { summary: "ok" }, "c3")]);
  assert.equal(await buildApp(fakeSession(), model, "m", "x", quiet), "ok");
  assert.match(model.requests[1].messages.at(-1).content, /not valid JSON/);
  // the history sent back must itself be valid JSON, or the server rejects the next request
  assert.deepEqual(JSON.parse(model.requests[1].messages.at(-2).tool_calls[0].function.arguments), {});
});

const slowModel = { chat: { completions: { create: (_b: unknown, o?: { signal?: AbortSignal }) => new Promise<never>((_, reject) => {
  o?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
}) } } };

test("the deadline cuts a slow model call and serves what was written", async () => {
  const s = fakeSession(); s.files.set("index.html", "x");
  const t = Date.now();
  assert.match(await buildApp(s, slowModel, "m", "x", { ...quiet, deadlineMs: 200 }), /time limit/);
  assert.ok(Date.now() - t < 5000);
});

test("the deadline cuts a slow model call and fails without index.html", async () => {
  await assert.rejects(buildApp(fakeSession(), slowModel, "m", "x", { ...quiet, deadlineMs: 200 }), /time limit/);
});
