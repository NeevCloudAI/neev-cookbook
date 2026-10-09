import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import {
  COMMENT_MARKER, GitHub, MAX_DIFF_CHARS, REPO_DIR, commentBody, fetchPullRequest, gitAuthEnv, main, missingEnv,
  prFromActions, reviewDiff, run, runTests, waitForHost,
} from "../review.ts";
import { BASE, HEAD, fakeGitHub, fakeModel, fakeNeev, fakeSandbox, type FakeSandbox } from "./fakes.ts";

const PR = { title: "Add discounts", base: BASE, head: HEAD };
const FULL_ENV = { NEEV_API_KEY: "k", NEEV_ORG_ID: "o", NEEV_PROJECT_ID: "p", NEEV_MODEL_API_KEY: "m" };
const noSleep = async () => {};

// runWith runs the recipe against fakes and returns the exit code with everything the fakes recorded.
async function runWith(o: { sandbox?: FakeSandbox; createError?: Error; model?: ReturnType<typeof fakeModel>; routes?: Record<string, unknown>; post?: boolean; signal?: AbortSignal } = {}) {
  const { neev, created, sandbox } = fakeNeev(o.sandbox ?? fakeSandbox(), o.createError);
  const gh = fakeGitHub(o.routes);
  const lines: string[] = [];
  const code = await run({
    repo: "o/r", number: 7, testCmd: "npm test", registries: ["registry.npmjs.org"], post: o.post ?? false,
    neev, modelClient: (o.model ?? fakeModel()).model, model: "glm-4-7", github: new GitHub("o/r", "ghs_token", gh.fetchFn),
    log: (s) => lines.push(s), signal: o.signal, sleep: noSleep,
  });
  return { code, created, sandbox, gh, lines };
}

describe("environment", () => {
  it("success: missingEnv names every unset variable", () => {
    assert.deepEqual(missingEnv({ NEEV_API_KEY: "k" }), ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]);
    assert.deepEqual(missingEnv(FULL_ENV), []);
  });

  it("success: prFromActions reads the event", () => {
    const event = join(mkdtempSync(join(tmpdir(), "pr-")), "event.json");
    writeFileSync(event, JSON.stringify({ pull_request: { number: 42 } }));
    assert.deepEqual(prFromActions({ GITHUB_ACTIONS: "true", GITHUB_EVENT_PATH: event, GITHUB_REPOSITORY: "o/r" }), ["o/r", 42]);
  });

  it("failure: prFromActions outside a pull request job", () => {
    const push = join(mkdtempSync(join(tmpdir(), "pr-")), "event.json");
    writeFileSync(push, JSON.stringify({ ref: "refs/heads/main" }));
    for (const env of [{}, { GITHUB_ACTIONS: "true" }, { GITHUB_ACTIONS: "true", GITHUB_EVENT_PATH: "/nope" }, { GITHUB_ACTIONS: "true", GITHUB_EVENT_PATH: push }]) {
      assert.equal(prFromActions(env), null);
    }
  });
});

describe("fetching into the sandbox", () => {
  it("success: gitAuthEnv puts the token in a header only", () => {
    const env = gitAuthEnv("ghs_secret");
    assert.equal(env.GIT_CONFIG_KEY_0, "http.extraHeader");
    const encoded = env.GIT_CONFIG_VALUE_0.replace("Authorization: Basic ", "");
    assert.equal(Buffer.from(encoded, "base64").toString(), "x-access-token:ghs_secret");
    assert.deepEqual(gitAuthEnv(undefined), { GIT_TERMINAL_PROMPT: "0" });
  });

  it("success: fetch never puts the token in a command line", async () => {
    const sandbox = fakeSandbox();
    const diff = await fetchPullRequest(sandbox, "o/r", PR, "ghs_secret", { sleep: noSleep });
    assert.match(diff, /applyDiscount/);
    const git = sandbox.execs.filter((e) => e.command[0] === "git").map((e) => e.command);
    assert.ok(git.every((c) => !c.join(" ").includes("ghs_secret")));
    assert.deepEqual(git.find((c) => c.includes("fetch"))!.slice(-3), ["https://github.com/o/r.git", BASE, HEAD]);
    assert.deepEqual(git.find((c) => c.includes("diff"))!.slice(3), ["diff", "--no-color", "--no-ext-diff", `${BASE}...${HEAD}`]);
  });

  it("success: fetch waits for the network", async () => {
    const sandbox = fakeSandbox({ dnsFailures: 3 });
    await fetchPullRequest(sandbox, "o/r", PR, undefined, { sleep: noSleep });
    assert.equal(sandbox.execs.filter((e) => e.command[0] === "getent").length, 4);
  });

  it("failure: waitForHost gives up after its budget", async () => {
    await assert.rejects(waitForHost(fakeSandbox({ dnsFailures: 99 }), "github.com", 0, noSleep), /could not resolve github.com/);
  });

  it("failure: fetch reports git's error", async () => {
    await assert.rejects(fetchPullRequest(fakeSandbox({ fetchExit: 128 }), "o/r", PR, undefined, { sleep: noSleep }),
      /git fetch failed: fatal: repository not found/);
  });
});

describe("review and tests", () => {
  it("success: review sends the diff and strips reasoning", async () => {
    const { model, calls } = fakeModel("<think>hmm</think>- looks fine");
    assert.equal(await reviewDiff(model, "glm-4-7", "Add discounts", "+x\n"), "- looks fine");
    assert.match(calls[0].messages[1].content, /Add discounts[\s\S]*<diff>\n\+x\n\n<\/diff>/);
    assert.match(calls[0].messages[0].content, /untrusted/);
  });

  it("success: review cuts a large diff and says so", async () => {
    const { model, calls } = fakeModel("- ok");
    const text = await reviewDiff(model, "m", "t", "+".repeat(MAX_DIFF_CHARS + 10));
    assert.match(text, /characters of the diff were reviewed\._$/);
    assert.ok(calls[0].messages[1].content.length < MAX_DIFF_CHARS + 100);
  });

  it("success: review of an empty diff skips the model", async () => {
    const { model, calls } = fakeModel();
    assert.match(await reviewDiff(model, "m", "t", "  \n"), /no changes/);
    assert.equal(calls.length, 0);
  });

  it("success: runTests prints whole lines and returns the exit code", async () => {
    const lines: string[] = [];
    const sandbox = fakeSandbox();
    const [code, tail] = await runTests(sandbox, "npm ci && npm test", (s) => lines.push(s));
    assert.equal(code, 0);
    assert.deepEqual(lines, ["   | ✔ adds", "   | ✔ subtracts"]);
    assert.equal(tail, "✔ adds\n✔ subtracts\n");
    assert.deepEqual(sandbox.streams, [{ command: ["bash", "-c", "npm ci && npm test"], cwd: REPO_DIR, env: { CI: "true" } }]);
  });

  it("failure: runTests records a stopped command", async () => {
    const sandbox = fakeSandbox({ testEvents: [{ type: "stdout", data: "partial" }], streamError: Object.assign(new Error("exec timed out"), { name: "DeadlineExceededError" }) });
    const lines: string[] = [];
    const [code, tail] = await runTests(sandbox, "npm test", (s) => lines.push(s));
    assert.equal(code, 1);
    assert.match(tail, /\[stopped: DeadlineExceededError: exec timed out\]/);
    assert.equal(lines.at(-1), "   | partial");
  });
});

describe("the comment", () => {
  it("success: carries the marker, status and output", () => {
    const body = commentBody("- bug", "npm test", 1, "not ok 1\n", HEAD);
    assert.ok(body.startsWith(COMMENT_MARKER));
    assert.match(body, /### Review of bbbbbbb[\s\S]*### Tests failed \(exit 1\)[\s\S]*not ok 1/);
  });

  it("success: quiets mentions and cannot be closed by test output", () => {
    const body = commentBody("ping @octocat", "npm test", 0, "````\nsneaky\n", HEAD);
    assert.ok(!body.includes("@octocat") && body.includes("@​octocat"));
    assert.ok(body.includes("`````\n````\nsneaky"));
  });
});

describe("GitHub", () => {
  it("success: reads the pull request with the token", async () => {
    const gh = fakeGitHub();
    assert.deepEqual(await new GitHub("o/r", "ghs_t", gh.fetchFn).pullRequest(7), PR);
    assert.equal(gh.requests[0].headers.Authorization, "Bearer ghs_t");
  });

  it("failure: rejects an unexpected commit id", async () => {
    const gh = fakeGitHub({ "GET /repos/o/r/pulls/7": { base: { sha: "main" }, head: { sha: HEAD } } });
    await assert.rejects(new GitHub("o/r", undefined, gh.fetchFn).pullRequest(7), /unexpected commit id/);
  });

  it("failure: an HTTP error is one line", async () => {
    await assert.rejects(new GitHub("o/r", undefined, fakeGitHub().fetchFn).pullRequest(8), /GitHub GET \/repos\/o\/r\/pulls\/8 returned 404/);
  });

  it("success: upsert updates this recipe's earlier comment", async () => {
    const gh = fakeGitHub({
      "GET /repos/o/r/issues/7/comments?per_page=100&page=1": [{ id: 1, body: "lgtm" }, { id: 2, body: `${COMMENT_MARKER}\nold` }],
      "PATCH /repos/o/r/issues/comments/2": { html_url: "https://github.com/o/r/pull/7#c2" },
    });
    assert.equal(await new GitHub("o/r", "t", gh.fetchFn).upsertComment(7, "new"), "https://github.com/o/r/pull/7#c2");
    assert.deepEqual(gh.requests.at(-1)!.body, { body: "new" });
  });

  it("success: upsert pages, then posts when there is no earlier comment", async () => {
    const gh = fakeGitHub({
      "GET /repos/o/r/issues/7/comments?per_page=100&page=1": Array.from({ length: 100 }, (_, id) => ({ id, body: "x" })),
      "GET /repos/o/r/issues/7/comments?per_page=100&page=2": [],
      "POST /repos/o/r/issues/7/comments": { html_url: "https://github.com/o/r/pull/7#c9" },
    });
    assert.equal(await new GitHub("o/r", "t", gh.fetchFn).upsertComment(7, "new"), "https://github.com/o/r/pull/7#c9");
    assert.deepEqual(gh.requests.map((r) => r.method), ["GET", "GET", "POST"]);
  });
});

describe("the whole run", () => {
  it("success: prints the comment and deletes the sandbox", async () => {
    const { code, created, sandbox, gh, lines } = await runWith();
    assert.equal(code, 0);
    assert.ok(sandbox.deleted);
    assert.deepEqual(created[0].allowEgress, ["github.com", "registry.npmjs.org"]);
    assert.match(created[0].name as string, /^pr-review-[0-9a-f]{8}$/);
    assert.equal((created[0].lifecycle as Record<string, unknown>).on_idle, "delete");
    assert.ok(lines.some((l) => l.startsWith(COMMENT_MARKER)));
    assert.deepEqual(gh.requests.map((r) => r.method), ["GET"]); // read the pull request, posted nothing
  });

  it("success: GitHub access is removed before the tests run", async () => {
    const { sandbox } = await runWith();
    assert.deepEqual(sandbox.order, ["update", "tests"]);
    assert.deepEqual(sandbox.updates[0], { egress_remove: { allow: [{ host: "github.com" }] } });
  });

  it("success: posts when asked", async () => {
    const { code, gh, lines } = await runWith({
      post: true,
      routes: {
        "GET /repos/o/r/issues/7/comments?per_page=100&page=1": [],
        "POST /repos/o/r/issues/7/comments": { html_url: "https://github.com/o/r/pull/7#c1" },
      },
    });
    assert.equal(code, 0);
    assert.ok(lines.includes("6. Posted the review: https://github.com/o/r/pull/7#c1"));
    assert.ok(gh.requests.at(-1)!.body.body.startsWith(COMMENT_MARKER));
  });

  it("failure: failing tests fail the run but still comment", async () => {
    const { code, sandbox, lines } = await runWith({ sandbox: fakeSandbox({ testEvents: [{ type: "stdout", data: "not ok\n" }, { type: "exit", exitCode: 1 }] }) });
    assert.equal(code, 1);
    assert.ok(sandbox.deleted);
    assert.ok(lines.some((l) => l.includes("### Tests failed (exit 1)")));
  });

  it("failure: a fetch error is one line and cleans up", async () => {
    const { code, sandbox, lines } = await runWith({ sandbox: fakeSandbox({ fetchExit: 128 }) });
    assert.equal(code, 1);
    assert.ok(sandbox.deleted);
    assert.ok(lines.some((l) => l.startsWith("Failed: Error: git fetch failed")));
  });

  it("failure: a model error fails the run and cleans up", async () => {
    const { code, sandbox, lines } = await runWith({ model: fakeModel("", new Error("401 unauthorized")) });
    assert.equal(code, 1);
    assert.ok(sandbox.deleted);
    assert.ok(lines.includes("Failed: Error: 401 unauthorized"));
  });

  it("failure: Ctrl+C returns 130 and cleans up", async () => {
    const ac = new AbortController();
    const model = fakeModel();
    model.model.chat.completions.create = async () => { ac.abort(); throw new Error("aborted"); };
    const { code, sandbox } = await runWith({ model, signal: ac.signal });
    assert.equal(code, 130);
    assert.ok(sandbox.deleted);
  });

  it("failure: a create error leaves nothing to delete", async () => {
    const { code, lines } = await runWith({ createError: new Error("quota exceeded") });
    assert.equal(code, 1);
    assert.ok(lines.includes("Failed: Error: quota exceeded"));
  });

  it("failure: a delete error is a warning, not a stack trace", async () => {
    const { code, lines } = await runWith({ sandbox: fakeSandbox({ deleteError: new Error("gone") }) });
    assert.equal(code, 0);
    assert.ok(lines.some((l) => l.includes("Warning: could not delete sandbox pr-review-test")));
  });
});

describe("main", () => {
  it("failure: names missing variables", async () => {
    assert.equal(await main([], {}), 2);
  });

  it("failure: needs a GitHub token to post", async () => {
    assert.equal(await main(["--repo", "o/r", "--pr", "7"], FULL_ENV), 2);
  });

  it("failure: rejects a bad repository name or number", async () => {
    assert.equal(await main(["--repo", "o/r/../x", "--pr", "7", "--dry-run"], FULL_ENV), 2);
    assert.equal(await main(["--repo", "o/r", "--pr", "seven", "--dry-run"], FULL_ENV), 2);
  });

  it("failure: wants --repo and --pr together", async () => {
    assert.equal(await main(["--repo", "o/r"], FULL_ENV), 2);
  });
});
