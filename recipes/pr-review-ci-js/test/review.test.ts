import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import {
  GitHub, MAX_COMMENTS, MAX_DIFF_CHARS, REPO_DIR, REVIEW_MARKER, fetchPullRequest, gitAuthEnv, inlineComments, main, missingEnv,
  numberDiff, prFromActions, priorNote, reviewBody, reviewDiff, run, runTests, waitForHost,
} from "../review.ts";
import { BASE, DIFF, HEAD, fakeGitHub, fakeModel, fakeNeev, fakeSandbox, type FakeSandbox } from "./fakes.ts";

const PR = { title: "Add discounts", base: BASE, head: HEAD };
const FULL_ENV = { NEEV_API_KEY: "k", NEEV_ORG_ID: "o", NEEV_PROJECT_ID: "p", NEEV_MODEL_API_KEY: "m" };
const REVIEWS = "/repos/o/r/pulls/7/reviews";
const REVIEW_PAGE = `${REVIEWS}?per_page=100&page=1`;
const COMMENT_PAGE = "/repos/o/r/pulls/7/comments?per_page=100&page=1";
const BOT = { login: "github-actions[bot]" };

// history returns GitHub routes for a pull request the Actions bot already reviewed on the given commits.
function history(...commits: string[]): Record<string, unknown> {
  return {
    [`GET ${REVIEW_PAGE}`]: commits.map((commit_id, i) => ({ id: i + 1, commit_id, body: REVIEW_MARKER, user: BOT })),
    [`GET ${COMMENT_PAGE}`]: [], [`POST ${REVIEWS}`]: { html_url: "u" },
  };
}
const noSleep = async () => {};

// runWith runs the recipe against fakes and returns the exit code with everything the fakes recorded.
async function runWith(o: { sandbox?: FakeSandbox; createError?: Error; model?: ReturnType<typeof fakeModel>; routes?: Record<string, unknown>; post?: boolean; signal?: AbortSignal; maxReviews?: number } = {}) {
  const { neev, created, sandbox } = fakeNeev(o.sandbox ?? fakeSandbox(), o.createError);
  const gh = fakeGitHub(o.routes);
  const lines: string[] = [];
  const code = await run({
    repo: "o/r", number: 7, testCmd: "npm test", registries: ["registry.npmjs.org"], post: o.post ?? false,
    neev, modelClient: (o.model ?? fakeModel()).model, model: "glm-4-7", github: new GitHub("o/r", "ghs_token", gh.fetchFn),
    log: (s) => lines.push(s), signal: o.signal, sleep: noSleep, maxReviews: o.maxReviews,
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
  it("success: numberDiff numbers new lines and maps hunks", () => {
    const [numbered, lines] = numberDiff(DIFF);
    assert.match(numbered, /^    6 \+export function applyDiscount\(total, code\) \{$/m);
    assert.deepEqual([...lines["cart.js"]], [[5, [1, "}"]], [6, [1, "export function applyDiscount(total, code) {"]],
      [7, [1, "  return total - Number(code.slice(4));"]], [8, [1, "}"]]]);
  });

  it("success: numberDiff reads plus lines in a hunk as content, and skips deleted files", () => {
    const [numbered, lines] = numberDiff("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1,2 @@\n++++ not a header\n+y\n" +
      "diff --git a/g b/g\ndeleted file mode 100644\n--- a/g\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n");
    assert.deepEqual(Object.keys(lines), ["x"]);
    assert.deepEqual([...lines.x.keys()], [1, 2]);
    assert.ok(numbered.includes("    1 ++++ not a header") && numbered.includes("      -gone"));
  });

  it("success: review sends the numbered diff and reads JSON", async () => {
    const { model, calls } = fakeModel();
    const [summary, comments, lines] = await reviewDiff(model, "glm-4-7", "Add discounts", DIFF);
    assert.equal(summary, "Adds discount codes.");
    assert.equal(comments[0].line, 7);
    assert.ok(lines["cart.js"].has(7));
    assert.match(calls[0].messages[1].content, /Add discounts[\s\S]*    7 \+  return total/);
    assert.match(calls[0].messages[0].content, /untrusted/);
  });

  it("success: review strips reasoning and a code fence", async () => {
    const reply = '<think>hmm</think>```json\n{"summary": "Fine.", "comments": []}\n```';
    assert.deepEqual((await reviewDiff(fakeModel(reply).model, "m", "t", DIFF)).slice(0, 2), ["Fine.", []]);
  });

  it("failure: a review that is not JSON becomes the summary", async () => {
    assert.deepEqual((await reviewDiff(fakeModel("Looks good to me.").model, "m", "t", DIFF)).slice(0, 2), ["Looks good to me.", []]);
  });

  it("failure: review drops malformed comments and keeps at most six", async () => {
    const good = { path: "cart.js", line: 7, body: "x" };
    const reply = JSON.stringify({ summary: "s", comments: [{ path: "cart.js", line: "7", body: "x" }, { path: "cart.js", line: 7, body: " " }, "nope", ...Array(9).fill(good)] });
    assert.equal((await reviewDiff(fakeModel(reply).model, "m", "t", DIFF))[1].length, MAX_COMMENTS);
  });

  it("success: review cuts a large diff and says so", async () => {
    const { model, calls } = fakeModel();
    const big = DIFF + Array.from({ length: 20_000 }, (_, i) => `+line ${i}`).join("\n");
    assert.match((await reviewDiff(model, "m", "t", big))[0], /characters of the diff were reviewed\.$/);
    assert.ok(calls[0].messages[1].content.length < MAX_DIFF_CHARS + 200);
  });

  it("success: review of an empty diff skips the model", async () => {
    const { model, calls } = fakeModel();
    assert.match((await reviewDiff(model, "m", "t", ""))[0], /no changes/);
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

describe("inline comments", () => {
  const lines = numberDiff(DIFF)[1];

  it("success: carries a reindented suggestion", () => {
    const [placed, unplaced] = inlineComments([{ path: "cart.js", line: 7, body: "NaN for @octocat's code.", suggestion: "      return total;\n" }], lines);
    assert.deepEqual(unplaced, []);
    assert.deepEqual(placed, [{ path: "cart.js", line: 7, side: "RIGHT", body: "NaN for @\u200boctocat's code.\n\n```suggestion\n  return total;\n```" }]);
  });

  it("success: spans lines in one hunk", () => {
    const [placed] = inlineComments([{ path: "cart.js", line: 6, end_line: 7, body: "b" }], lines);
    assert.deepEqual(placed, [{ path: "cart.js", line: 7, side: "RIGHT", body: "b", start_line: 6, start_side: "RIGHT" }]);
  });

  it("failure: a comment without a diff line goes to the body", () => {
    for (const c of [{ path: "cart.js", line: 2, body: "outside" }, { path: "other.js", line: 7, body: "not in the diff" },
      { path: "cart.js", line: 7, end_line: 6, body: "backwards" }, { path: "cart.js", line: 7, end_line: 30, body: "past the hunk" }]) {
      const [placed, unplaced] = inlineComments([c], lines);
      assert.deepEqual([placed, unplaced], [[], [{ path: c.path, line: c.line, body: c.body }]]);
    }
  });
});

describe("the review body", () => {
  it("success: leads with the test result", () => {
    const body = reviewBody("Adds discounts.", [], "npm test", 0, "ok\n");
    assert.ok(body.startsWith(REVIEW_MARKER));
    assert.ok(body.includes("**Tests passed** in an isolated NeevCloud sandbox: `npm test`\n\nAdds discounts."));
  });

  it("success: lists comments without a line, and failures", () => {
    const body = reviewBody("", [{ path: "a.js", line: 2, body: "b" }], "npm test", 1, "not ok\n");
    assert.ok(body.includes("**Tests failed (exit 1)**") && body.includes("- `a.js:2` b") && body.includes("not ok"));
  });

  it("success: quiets mentions and cannot be closed by test output", () => {
    const body = reviewBody("ping @octocat", [], "npm test", 0, "````\nsneaky\n");
    assert.ok(!body.includes("@octocat") && body.includes("`````\n````\nsneaky"));
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

  it("success: postReview sends inline comments on the head commit", async () => {
    const gh = fakeGitHub({ [`POST ${REVIEWS}`]: { html_url: "https://github.com/o/r/pull/7#r1" } });
    const comments = [{ path: "cart.js", line: 7, side: "RIGHT" as const, body: "b" }];
    assert.equal(await new GitHub("o/r", "t", gh.fetchFn).postReview(7, HEAD, "body", comments), "https://github.com/o/r/pull/7#r1");
    assert.deepEqual(gh.requests.at(-1)!.body, { commit_id: HEAD, event: "COMMENT", body: "body", comments });
  });

  it("failure: postReview folds comments into the body when GitHub refuses a line", async () => {
    const sent: any[] = [];
    const fetchFn = (async (_url: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string);
      sent.push(body);
      return body.comments.length ? new Response("{}", { status: 422 }) : new Response(JSON.stringify({ html_url: "u" }), { status: 200 });
    }) as unknown as typeof fetch;
    await new GitHub("o/r", "t", fetchFn).postReview(7, HEAD, "body", [{ path: "a.js", line: 3, side: "RIGHT", body: "b" }]);
    assert.deepEqual([sent.at(-1).comments, sent.at(-1).body], [[], "body\n\n- `a.js:3` b"]);
  });

  it("success: history counts only our reviews and collects their comments", async () => {
    const gh = fakeGitHub({
      [`GET ${REVIEW_PAGE}`]: [
        { id: 1, commit_id: BASE, body: REVIEW_MARKER, user: BOT },
        { id: 2, commit_id: HEAD, body: `${REVIEW_MARKER} quoted`, user: { login: "attacker" } },
        { id: 3, commit_id: BASE, body: "lgtm", user: BOT }],
      [`GET ${COMMENT_PAGE}`]: [
        { id: 10, pull_request_review_id: 1, path: "a.js", line: 4, body: "NaN here." },
        { id: 11, pull_request_review_id: 1, in_reply_to_id: 10, body: "Not reachable." },
        { id: 12, pull_request_review_id: 2, path: "b.js", line: 1, body: "someone else's" }],
    });
    assert.deepEqual(await new GitHub("o/r", "t", gh.fetchFn).history(7, HEAD), {
      passes: 1, reviewedHead: false, prior: [{ path: "a.js", line: 4, body: "NaN here.", replies: ["Not reachable."] }] });
  });

  it("success: history pages and uses the token's login", async () => {
    const gh = fakeGitHub({
      "GET /user": { login: "maintainer" },
      [`GET ${REVIEW_PAGE}`]: Array(100).fill({ id: 0, commit_id: BASE, body: "x" }),
      [`GET ${REVIEWS}?per_page=100&page=2`]: [{ id: 5, commit_id: HEAD, body: REVIEW_MARKER, user: { login: "maintainer" } }],
      [`GET ${COMMENT_PAGE}`]: [],
    });
    assert.deepEqual(await new GitHub("o/r", "t", gh.fetchFn).history(7, HEAD), { passes: 1, reviewedHead: true, prior: [] });
  });

  it("success: history without reviews skips the comments", async () => {
    const gh = fakeGitHub({ [`GET ${REVIEW_PAGE}`]: [] });
    assert.deepEqual(await new GitHub("o/r", "t", gh.fetchFn).history(7, HEAD), { passes: 0, reviewedHead: false, prior: [] });
    assert.ok(!gh.requests.some((r) => r.path === COMMENT_PAGE));
  });

  it("success: priorNote tells the model not to repeat", () => {
    const note = priorNote([{ path: "a.js", line: 4, body: "NaN\nhere.", replies: ["Not reachable."] }]);
    assert.ok(note.includes("Do not raise these again") && note.includes("- a.js:4 NaN here.\n  reply: Not reachable."));
    assert.equal(priorNote([]), "");
  });

  it("success: review sends earlier comments to the model", async () => {
    const { model, calls } = fakeModel();
    await reviewDiff(model, "m", "t", DIFF, undefined, [{ path: "a.js", line: 4, body: "NaN here.", replies: [] }]);
    assert.ok(calls[0].messages[1].content.includes("- a.js:4 NaN here."));
  });
});

describe("the whole run", () => {
  it("success: prints the review and deletes the sandbox", async () => {
    const { code, created, sandbox, gh, lines } = await runWith();
    assert.equal(code, 0);
    assert.ok(sandbox.deleted);
    assert.deepEqual(created[0].allowEgress, ["github.com", "registry.npmjs.org"]);
    assert.match(created[0].name as string, /^pr-review-[0-9a-f]{8}$/);
    assert.equal((created[0].lifecycle as Record<string, unknown>).on_idle, "delete");
    assert.ok(lines.includes("   Review ready in 0s: 1 inline comments"));
    assert.ok(lines.some((l) => l.startsWith(REVIEW_MARKER)));
    assert.ok(lines.includes("   cart.js:7\n      This returns NaN for an unknown code.\n      \n      ```suggestion\n        return total;\n      ```"));
    assert.deepEqual(gh.requests.map((r) => r.method), ["GET"]); // read the pull request, posted nothing
    assert.ok(lines.includes("   8 diff lines; the GitHub token was used for this step only and never stored"));
  });

  it("success: GitHub access is removed before the tests run", async () => {
    const { sandbox } = await runWith();
    assert.deepEqual(sandbox.order, ["update", "tests"]);
    assert.deepEqual(sandbox.updates[0], { egress_remove: { allow: [{ host: "github.com" }] } });
  });

  it("success: posts a review when asked", async () => {
    const { code, gh, lines } = await runWith({
      post: true,
      routes: { [`GET ${REVIEW_PAGE}`]: [], [`POST ${REVIEWS}`]: { html_url: "https://github.com/o/r/pull/7#r1" } },
    });
    assert.equal(code, 0);
    assert.ok(lines.includes("6. Posted the review: https://github.com/o/r/pull/7#r1"));
    const sent = gh.requests.at(-1)!.body;
    assert.ok(sent.body.startsWith(REVIEW_MARKER));
    assert.equal(sent.comments[0].path, "cart.js");
  });

  it("success: does not review one commit twice", async () => {
    const model = fakeModel();
    const { code, gh, lines } = await runWith({ post: true, model, routes: history(HEAD) });
    assert.equal(code, 0);
    assert.equal(model.calls.length, 0);
    assert.ok(lines.includes("4. Not reviewing: bbbbbbb already has this recipe's review; the tests still run."));
    assert.ok(!gh.requests.some((r) => r.method === "POST"));
  });

  it("success: stops reviewing at the ceiling but still tests", async () => {
    const model = fakeModel();
    const sandbox = fakeSandbox({ testEvents: [{ type: "exit", exitCode: 1 }] });
    const { code, lines } = await runWith({ post: true, model, sandbox, routes: history(BASE, BASE, BASE) });
    assert.equal(code, 1);
    assert.equal(model.calls.length, 0);
    assert.equal(sandbox.streams.length, 1);
    assert.ok(lines.includes("6. Not posting: the pull request has had its 3 reviews."));
  });

  it("success: the last review says it is the last", async () => {
    const model = fakeModel();
    const routes = history(BASE, BASE);
    routes[`GET ${COMMENT_PAGE}`] = [{ id: 9, pull_request_review_id: 1, path: "cart.js", line: 7, body: "Old point." }];
    const { code, gh, lines } = await runWith({ post: true, model, routes });
    assert.equal(code, 0);
    assert.ok(gh.requests.at(-1)!.body.body.includes("_Review 3 of 3: later pushes are tested but not reviewed._"));
    assert.ok(model.calls[0].messages[1].content.includes("- cart.js:7 Old point."));
    assert.ok(lines.includes("   2 earlier review(s), 1 inline comment(s)"));
  });

  it("success: no limit keeps reviewing", async () => {
    const { code, gh } = await runWith({ post: true, maxReviews: 0, routes: history(BASE, BASE, BASE, BASE, BASE) });
    assert.equal(code, 0);
    assert.equal(gh.requests.at(-1)!.method, "POST");
    assert.ok(!gh.requests.at(-1)!.body.body.includes("Review 6 of"));
  });

  it("failure: failing tests fail the run but still review", async () => {
    const { code, sandbox, lines } = await runWith({ sandbox: fakeSandbox({ testEvents: [{ type: "stdout", data: "not ok\n" }, { type: "exit", exitCode: 1 }] }) });
    assert.equal(code, 1);
    assert.ok(sandbox.deleted);
    assert.ok(lines.some((l) => l.includes("**Tests failed (exit 1)**")));
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

  it("failure: rejects a negative review ceiling", async () => {
    assert.equal(await main(["--max-reviews", "-1"], FULL_ENV), 2);
  });
});
