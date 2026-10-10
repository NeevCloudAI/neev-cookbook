import base64
import json
import os
from types import SimpleNamespace

import pytest

import review
from review import (
    REVIEW_MARKER, GitHub, ReviewError, fetch_pull_request, git_auth_env, guide_note, inline_comments, local_diff, missing_env,
    number_diff, pr_from_actions, prior_note, review_body, review_diff, run, run_tests, wait_for_host,
)
from tests.fakes import BASE, DIFF, HEAD, FakeClient, FakeGitHubAPI, FakeModel, FakeSandbox

PR = {"title": "Add discounts", "base": BASE, "head": HEAD}
FULL_ENV = {"NEEV_API_KEY": "k", "NEEV_ORG_ID": "o", "NEEV_PROJECT_ID": "p", "NEEV_MODEL_API_KEY": "m"}


def _run(sandbox=None, client=None, model=None, api=None, post=False, test_cmd="npm test", lines=None, max_reviews=3):
    """Runs the recipe against fakes and returns (exit code, client, api, printed lines)."""
    client = client or FakeClient(sandbox or FakeSandbox())
    api = api or FakeGitHubAPI()
    lines = [] if lines is None else lines
    code = run("o/r", 7, test_cmd, ["registry.npmjs.org"], post, client, model or FakeModel(), "glm-4-7",
               GitHub("o/r", "ghs_token", urlopen=api), log=lines.append, max_reviews=max_reviews)
    return code, client, api, lines


# --- environment -------------------------------------------------------------------------------

def test_success_missing_env_names_every_unset_variable():
    assert missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]
    assert missing_env(FULL_ENV) == []


def test_success_pr_from_actions_reads_the_event(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 42}}))
    env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "o/r"}
    assert pr_from_actions(env) == ("o/r", 42)


@pytest.mark.parametrize("env", [{}, {"GITHUB_ACTIONS": "true"}, {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_PATH": "/nope"}])
def test_failure_pr_from_actions_outside_a_pull_request_job(env):
    assert pr_from_actions(env) is None


def test_failure_pr_from_actions_on_a_push_event(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"ref": "refs/heads/main"}))
    assert pr_from_actions({"GITHUB_ACTIONS": "true", "GITHUB_EVENT_PATH": str(event)}) is None


# --- fetching into the sandbox -----------------------------------------------------------------

def test_success_git_auth_env_puts_the_token_in_a_header_only():
    env = git_auth_env("ghs_secret")
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    encoded = env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
    assert base64.b64decode(encoded).decode() == "x-access-token:ghs_secret"
    assert git_auth_env(None) == {"GIT_TERMINAL_PROMPT": "0"}


def test_success_fetch_never_puts_the_token_in_a_command_line():
    sandbox = FakeSandbox()
    diff = fetch_pull_request(sandbox, "o/r", PR, "ghs_secret")
    assert "applyDiscount" in diff
    git_calls = [(args, env) for command, args, env in sandbox.execs if command == "git"]
    assert all("ghs_secret" not in " ".join(args) for args, _ in git_calls)
    fetch_args = next(args for args, _ in git_calls if "fetch" in args)
    assert fetch_args[-3:] == ["https://github.com/o/r.git", BASE, HEAD]
    assert ["diff", "--no-color", "--no-ext-diff", "--no-textconv", f"{BASE}...{HEAD}"] == next(
        args for args, _ in git_calls if "diff" in args)[2:]


def test_success_fetch_waits_for_the_network(monkeypatch):
    monkeypatch.setattr(review.time, "sleep", lambda s: None)
    sandbox = FakeSandbox(dns_failures=3)
    fetch_pull_request(sandbox, "o/r", PR, None)
    assert [c for c, _, _ in sandbox.execs].count("getent") == 4


def test_success_fetch_leaves_excluded_files_out_of_the_diff():
    sandbox = FakeSandbox()
    fetch_pull_request(sandbox, "o/r", PR, None, exclude=["**/*.gen.go", "**/mocks/**"])
    diff_args = next(args for command, args, _ in sandbox.execs if command == "git" and "diff" in args)
    assert diff_args[-4:] == ["--", ".", ":(exclude,glob)**/*.gen.go", ":(exclude,glob)**/mocks/**"]


class FakeGit:
    """Plays git for local_diff: records each command and its environment, and fails the step named in fail."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __call__(self, args, env=None, capture_output=None, text=None, timeout=None):
        self.calls.append((args, env))
        if self.fail and self.fail in args:
            return SimpleNamespace(returncode=128, stdout="", stderr="fatal: repository not found")
        return SimpleNamespace(returncode=0, stdout=DIFF if "diff" in args else "", stderr="")


def test_success_local_diff_fetches_without_a_checkout_and_cleans_up():
    git = FakeGit()
    assert local_diff("o/r", PR, "ghs_secret", ["**/*.gen.go"], run=git) == DIFF
    commands = [args[3:] for args, _ in git.calls]
    assert [c[0] for c in commands] == ["init", "fetch", "diff"]
    assert commands[1][-3:] == ["https://github.com/o/r.git", BASE, HEAD]
    assert commands[2][-1] == ":(exclude,glob)**/*.gen.go"
    workdir = git.calls[0][0][2]
    assert not os.path.exists(workdir)
    assert all("ghs_secret" not in " ".join(args) for args, _ in git.calls)
    assert git.calls[1][1]["GIT_CONFIG_KEY_0"] == "http.extraHeader"


def test_failure_local_diff_reports_gits_error_and_cleans_up():
    git = FakeGit(fail="fetch")
    with pytest.raises(ReviewError, match="git fetch failed: fatal: repository not found"):
        local_diff("o/r", PR, None, run=git)
    assert not os.path.exists(git.calls[0][0][2])


def test_failure_wait_for_host_gives_up_after_its_budget():
    with pytest.raises(ReviewError, match="could not resolve github.com"):
        wait_for_host(FakeSandbox(dns_failures=99), "github.com", timeout_s=0, wait=lambda s: None)


def test_failure_fetch_reports_gits_error():
    with pytest.raises(ReviewError, match="git fetch failed: fatal: repository not found"):
        fetch_pull_request(FakeSandbox(fetch_exit=128), "o/r", PR, None)


# --- review and tests --------------------------------------------------------------------------

def test_success_number_diff_numbers_new_lines_and_maps_hunks():
    numbered, lines = number_diff(DIFF)
    assert "    6 +export function applyDiscount(total, code) {" in numbered
    assert lines == {"cart.js": {5: (1, "}"), 6: (1, "export function applyDiscount(total, code) {"),
                                 7: (1, "  return total - Number(code.slice(4));"), 8: (1, "}")}}


def test_success_number_diff_reads_plus_lines_in_a_hunk_as_content():
    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1,2 @@\n++++ not a header\n+y\n"
    numbered, lines = number_diff(diff)
    assert set(lines["x"]) == {1, 2}
    assert "    1 ++++ not a header" in numbered


def test_success_number_diff_skips_deleted_files():
    diff = "diff --git a/x b/x\ndeleted file mode 100644\n--- a/x\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
    numbered, lines = number_diff(diff)
    assert lines == {} and "      -gone" in numbered


def test_success_review_sends_the_numbered_diff_and_reads_json():
    model = FakeModel()
    summary, comments, lines = review_diff(model, "glm-4-7", "Add discounts", DIFF)
    assert summary == "Adds discount codes."
    assert comments[0]["line"] == 7 and 7 in lines["cart.js"]
    user = model.calls[0]["messages"][1]["content"]
    assert "Add discounts" in user and "    7 +  return total" in user
    assert "untrusted" in model.calls[0]["messages"][0]["content"]


def test_success_review_strips_reasoning_and_a_code_fence():
    reply = '<think>hmm</think>```json\n{"summary": "Fine.", "comments": []}\n```'
    assert review_diff(FakeModel(reply=reply), "m", "t", DIFF)[:2] == ("Fine.", [])


def test_failure_review_that_is_not_json_becomes_the_summary():
    assert review_diff(FakeModel(reply="Looks good to me."), "m", "t", DIFF)[:2] == ("Looks good to me.", [])


def test_failure_review_drops_malformed_comments_and_keeps_at_most_six():
    good = {"path": "cart.js", "line": 7, "body": "x"}
    reply = json.dumps({"summary": "s", "comments": [{"path": "cart.js", "line": "7", "body": "x"}, {"path": "cart.js", "line": True, "body": "x"},
                                                     {"path": "cart.js", "line": 7, "body": " "}, "nope"] + [good] * 9})
    assert len(review_diff(FakeModel(reply=reply), "m", "t", DIFF)[1]) == review.MAX_COMMENTS


def test_success_review_cuts_a_large_diff_and_says_so():
    model = FakeModel()
    big = DIFF + "\n".join(f"+line {i}" for i in range(20_000))
    summary = review_diff(model, "m", "t", big)[0]
    assert summary.endswith("characters of the diff were reviewed.")
    assert len(model.calls[0]["messages"][1]["content"]) < review.MAX_DIFF_CHARS + 200


def test_success_review_of_an_empty_diff_skips_the_model():
    model = FakeModel()
    assert "no changes" in review_diff(model, "m", "t", "")[0]
    assert model.calls == []


# --- inline comments ---------------------------------------------------------------------------

LINES = number_diff(DIFF)[1]


def test_success_inline_comment_carries_a_reindented_suggestion():
    placed, unplaced = inline_comments([{"path": "cart.js", "line": 7, "body": "NaN for @octocat's code.",
                                         "suggestion": "      return total;\n"}], LINES)
    assert unplaced == []
    assert placed == [{"path": "cart.js", "line": 7, "side": "RIGHT",
                       "body": "NaN for @\u200boctocat's code.\n\n```suggestion\n  return total;\n```"}]


def test_success_inline_comment_spans_lines_in_one_hunk():
    placed, _ = inline_comments([{"path": "cart.js", "line": 6, "end_line": 7, "body": "b"}], LINES)
    assert placed[0] | {} == {"path": "cart.js", "line": 7, "side": "RIGHT", "body": "b",
                              "start_line": 6, "start_side": "RIGHT"}


@pytest.mark.parametrize("comment", [
    {"path": "cart.js", "line": 2, "body": "outside the diff"},
    {"path": "other.js", "line": 7, "body": "not in the diff"},
    {"path": "cart.js", "line": 7, "end_line": 6, "body": "backwards"},
    {"path": "cart.js", "line": 7, "end_line": 30, "body": "runs past the hunk"},
])
def test_failure_comment_without_a_diff_line_goes_to_the_body(comment):
    placed, unplaced = inline_comments([comment], LINES)
    assert placed == [] and unplaced == [{"path": comment["path"], "line": comment["line"], "body": comment["body"]}]


def test_success_run_tests_prints_whole_lines_and_returns_the_exit_code():
    lines = []
    code, tail = run_tests(FakeSandbox(), "npm test", lines.append)
    assert code == 0
    assert lines == ["   | ✔ adds", "   | ✔ subtracts"]
    assert tail == "✔ adds\n✔ subtracts\n"


def test_failure_run_tests_records_a_stopped_command():
    sandbox = FakeSandbox(test_events=[{"type": "stdout", "data": "partial"}], stream_error=TimeoutError("exec timed out"))
    lines = []
    code, tail = run_tests(sandbox, "npm test", lines.append)
    assert code == 1
    assert "[stopped: TimeoutError: exec timed out]" in tail
    assert lines[-1] == "   | partial"


def test_success_tests_run_with_ci_set_in_the_checkout():
    sandbox = FakeSandbox()
    run_tests(sandbox, "npm ci && npm test", lambda s: None)
    assert sandbox.stream_calls == [("bash", ["-c", "npm ci && npm test"], review.REPO_DIR, {"CI": "true"})]


# --- the review body --------------------------------------------------------------------------

def test_success_review_body_leads_with_the_test_result():
    body = review_body("Adds discounts.", [], "npm test", 0, "ok\n")
    assert body.startswith(REVIEW_MARKER)
    assert "**Tests passed** in an isolated NeevCloud sandbox: `npm test`\n\nAdds discounts." in body


def test_success_review_body_lists_comments_without_a_line_and_failures():
    body = review_body("", [{"path": "a.js", "line": 2, "body": "b"}], "npm test", 1, "not ok\n")
    assert "**Tests failed (exit 1)**" in body and "- `a.js:2` b" in body and "not ok" in body


def test_success_review_only_body_has_no_test_result():
    body = review_body("Adds discounts.", [], "", 0, "")
    assert body == f"{REVIEW_MARKER}\n\nAdds discounts.\n"


def test_success_review_body_quiets_mentions_and_cannot_be_closed_by_output():
    body = review_body("ping @octocat", [], "npm test", 0, "````\nsneaky\n")
    assert "@octocat" not in body and "`````\n````\nsneaky" in body


# --- GitHub ------------------------------------------------------------------------------------

def test_success_github_reads_the_pull_request_with_the_token():
    api = FakeGitHubAPI()
    assert GitHub("o/r", "ghs_t", urlopen=api).pull_request(7) == PR
    assert api.requests[0][3]["Authorization"] == "Bearer ghs_t"


def test_failure_github_rejects_an_unexpected_commit_id():
    api = FakeGitHubAPI({("GET", "/repos/o/r/pulls/7"): {"base": {"sha": "main"}, "head": {"sha": HEAD}}})
    with pytest.raises(ReviewError, match="unexpected commit id"):
        GitHub("o/r", None, urlopen=api).pull_request(7)


def test_failure_github_error_is_one_line():
    with pytest.raises(ReviewError, match="GitHub GET /repos/o/r/pulls/8 returned 404"):
        GitHub("o/r", None, urlopen=FakeGitHubAPI()).pull_request(8)


REVIEWS = "/repos/o/r/pulls/7/reviews"


def test_success_post_review_sends_inline_comments_on_the_head_commit():
    api = FakeGitHubAPI({("POST", REVIEWS): {"html_url": "https://github.com/o/r/pull/7#r1"}})
    comments = [{"path": "cart.js", "line": 7, "side": "RIGHT", "body": "b"}]
    assert GitHub("o/r", "t", urlopen=api).post_review(7, HEAD, "body", comments) == "https://github.com/o/r/pull/7#r1"
    assert api.requests[-1][2] == {"commit_id": HEAD, "event": "COMMENT", "body": "body", "comments": comments}


def test_failure_post_review_folds_comments_into_the_body_when_github_refuses_a_line():
    class RefuseInline(FakeGitHubAPI):
        def __call__(self, request, timeout=None):
            if request.data and json.loads(request.data)["comments"]:
                self.requests.append(("POST", REVIEWS, json.loads(request.data), {}))
                raise review.urllib.error.HTTPError(request.full_url, 422, "Unprocessable", {}, None)
            return super().__call__(request, timeout)
    api = RefuseInline({("POST", REVIEWS): {"html_url": "u"}})
    GitHub("o/r", "t", urlopen=api).post_review(7, HEAD, "body", [{"path": "a.js", "line": 3, "body": "b"}])
    assert api.requests[-1][2]["comments"] == [] and api.requests[-1][2]["body"] == "body\n\n- `a.js:3` b"


BOT = {"login": "github-actions[bot]"}
REVIEW_PAGE = f"{REVIEWS}?per_page=100&page=1"
COMMENT_PAGE = "/repos/o/r/pulls/7/comments?per_page=100&page=1"


def test_success_history_counts_only_our_reviews_and_collects_their_comments():
    api = FakeGitHubAPI({
        ("GET", REVIEW_PAGE): [
            {"id": 1, "commit_id": BASE, "body": REVIEW_MARKER, "user": BOT},
            {"id": 2, "commit_id": HEAD, "body": f"{REVIEW_MARKER} quoted", "user": {"login": "attacker"}},
            {"id": 3, "commit_id": BASE, "body": "lgtm", "user": BOT}],
        ("GET", COMMENT_PAGE): [
            {"id": 10, "pull_request_review_id": 1, "path": "a.js", "line": 4, "body": "NaN here."},
            {"id": 11, "pull_request_review_id": 1, "in_reply_to_id": 10, "body": "Not reachable."},
            {"id": 12, "pull_request_review_id": 2, "path": "b.js", "line": 1, "body": "someone else's"}],
    })
    assert GitHub("o/r", "t", urlopen=api).history(7, HEAD) == {
        "passes": 1, "reviewed_head": False,
        "prior": [{"path": "a.js", "line": 4, "body": "NaN here.", "replies": ["Not reachable."]}]}


def test_success_history_pages_and_uses_the_tokens_login():
    api = FakeGitHubAPI({
        ("GET", "/user"): {"login": "maintainer"},
        ("GET", REVIEW_PAGE): [{"id": 0, "commit_id": BASE, "body": "x"}] * 100,
        ("GET", f"{REVIEWS}?per_page=100&page=2"): [{"id": 5, "commit_id": HEAD, "body": REVIEW_MARKER,
                                                     "user": {"login": "maintainer"}}],
        ("GET", COMMENT_PAGE): [],
    })
    assert GitHub("o/r", "t", urlopen=api).history(7, HEAD) == {"passes": 1, "reviewed_head": True, "prior": []}


def test_success_history_without_reviews_skips_the_comments():
    api = FakeGitHubAPI({("GET", REVIEW_PAGE): []})
    assert GitHub("o/r", "t", urlopen=api).history(7, HEAD) == {"passes": 0, "reviewed_head": False, "prior": []}
    assert ("GET", COMMENT_PAGE) not in [(r[0], r[1]) for r in api.requests]


def test_success_prior_note_tells_the_model_not_to_repeat():
    note = prior_note([{"path": "a.js", "line": 4, "body": "NaN\nhere.", "replies": ["Not reachable."]}])
    assert "Do not raise these again" in note and "- a.js:4 NaN here.\n  reply: Not reachable." in note
    assert prior_note([]) == ""


def test_success_guide_goes_into_the_system_prompt():
    model = FakeModel()
    review_diff(model, "m", "t", DIFF, guide="Wrap errors with %w.")
    system = model.calls[0]["messages"][0]["content"]
    assert "<conventions>\nWrap errors with %w.\n</conventions>" in system and "naming the convention" in system
    assert guide_note("  \n") == ""
    assert len(guide_note("x" * 50_000)) < review.MAX_GUIDE_CHARS + 400


def test_success_review_sends_earlier_comments_to_the_model():
    model = FakeModel()
    review_diff(model, "m", "t", DIFF, [{"path": "a.js", "line": 4, "body": "NaN here.", "replies": []}])
    assert "- a.js:4 NaN here." in model.calls[0]["messages"][1]["content"]


# --- the whole run -----------------------------------------------------------------------------

def test_success_run_prints_the_review_and_deletes_the_sandbox():
    code, client, api, lines = _run()
    assert code == 0
    assert client.sandbox.deleted
    params, allow = client.created[0]
    assert allow == ["github.com", "registry.npmjs.org"]
    assert params["name"].startswith("pr-review-") and params["lifecycle"]["on_idle"] == "delete"
    assert "   Review ready in 0s: 1 inline comments" in lines
    assert any(line.startswith(REVIEW_MARKER) for line in lines)
    assert "   cart.js:7\n      This returns NaN for an unknown code.\n      \n      ```suggestion\n" \
           "        return total;\n      ```" in lines
    assert [r[0] for r in api.requests] == ["GET"]  # read the pull request, posted nothing
    assert "   8 diff lines; the GitHub token was used for this step only and never stored" in lines


def test_success_github_access_is_removed_before_the_tests_run():
    sandbox = FakeSandbox()
    order = []
    sandbox.update = lambda params: order.append(("update", params))
    original = sandbox.exec_stream
    sandbox.exec_stream = lambda *a, **k: (order.append(("tests", None)), (yield from original(*a, **k)))[1]
    _run(sandbox=sandbox)
    assert order == [("update", {"egress_remove": {"allow": [{"host": "github.com"}]}}), ("tests", None)]


def test_success_run_posts_a_review_when_asked():
    api = FakeGitHubAPI({("GET", REVIEW_PAGE): [], ("POST", REVIEWS): {"html_url": "https://github.com/o/r/pull/7#r1"}})
    code, _, _, lines = _run(api=api, post=True)
    assert code == 0
    assert "6. Posted the review: https://github.com/o/r/pull/7#r1" in lines
    sent = api.requests[-1][2]
    assert sent["body"].startswith(REVIEW_MARKER) and sent["comments"][0]["path"] == "cart.js"


def _history(*reviews, comments=()):
    """GitHub routes for a pull request with the given reviews by the Actions bot and inline comments."""
    return {("GET", REVIEW_PAGE): [{"id": i, "commit_id": c, "body": REVIEW_MARKER, "user": BOT}
                                   for i, c in enumerate(reviews, 1)],
            ("GET", COMMENT_PAGE): list(comments), ("POST", REVIEWS): {"html_url": "u"}}


def test_success_run_does_not_review_one_commit_twice():
    api = FakeGitHubAPI(_history(HEAD))
    model = FakeModel()
    code, _, _, lines = _run(api=api, post=True, model=model)
    assert code == 0 and model.calls == []
    assert "4. Not reviewing: bbbbbbb already has this recipe's review; the tests still run." in lines
    assert "POST" not in [r[0] for r in api.requests]


def test_success_run_stops_reviewing_at_the_ceiling_but_still_tests():
    api = FakeGitHubAPI(_history(BASE, BASE, BASE))
    model = FakeModel()
    sandbox = FakeSandbox(test_events=[{"type": "exit", "exit_code": 1}])
    code, _, _, lines = _run(api=api, post=True, model=model, sandbox=sandbox)
    assert code == 1 and model.calls == [] and sandbox.stream_calls
    assert "6. Not posting: the pull request has had its 3 reviews." in lines


def test_success_last_review_says_it_is_the_last():
    api = FakeGitHubAPI(_history(BASE, BASE, comments=[
        {"id": 9, "pull_request_review_id": 1, "path": "cart.js", "line": 7, "body": "Old point."}]))
    model = FakeModel()
    code, _, _, lines = _run(api=api, post=True, model=model)
    assert code == 0
    assert "_Review 3 of 3: later pushes are tested but not reviewed._" in api.requests[-1][2]["body"]
    assert "- cart.js:7 Old point." in model.calls[0]["messages"][1]["content"]
    assert "   2 earlier review(s), 1 inline comment(s)" in lines


def test_success_no_limit_keeps_reviewing():
    api = FakeGitHubAPI(_history(*[BASE] * 5))
    code, _, _, _ = _run(api=api, post=True, max_reviews=0)
    assert code == 0 and api.requests[-1][0] == "POST" and "Review 6 of" not in api.requests[-1][2]["body"]


def test_success_review_only_run_creates_no_sandbox(monkeypatch):
    monkeypatch.setattr(review, "local_diff", lambda repo, pr, token, exclude: DIFF)
    client = FakeClient(create_error=AssertionError("a review-only run must not create a sandbox"))
    code, _, _, lines = _run(client=client, test_cmd="")
    assert code == 0 and client.created == []
    assert "2. Fetching the pull request's diff (review only: none of its code runs, so no sandbox)..." in lines
    assert "3. Reviewing the diff with glm-4-7..." in lines and "4. The review this run would post:\n" in lines
    assert not any("Tests passed" in line or "Sandbox deleted" in line for line in lines)


def test_failure_failing_tests_fail_the_run_but_still_review():
    sandbox = FakeSandbox(test_events=[{"type": "stdout", "data": "not ok\n"}, {"type": "exit", "exit_code": 1}])
    code, client, _, lines = _run(sandbox=sandbox)
    assert code == 1
    assert client.sandbox.deleted
    assert any("**Tests failed (exit 1)**" in line for line in lines)


def test_failure_fetch_error_is_one_line_and_cleans_up():
    code, client, _, lines = _run(sandbox=FakeSandbox(fetch_exit=128))
    assert code == 1
    assert client.sandbox.deleted
    assert any(line.startswith("Failed: ReviewError: git fetch failed") for line in lines)


def test_failure_model_error_fails_the_run_and_cleans_up():
    code, client, _, lines = _run(model=FakeModel(error=RuntimeError("401 unauthorized")))
    assert code == 1
    assert client.sandbox.deleted
    assert "Failed: RuntimeError: 401 unauthorized" in lines


def test_failure_ctrl_c_returns_130_and_cleans_up():
    code, client, _, _ = _run(model=FakeModel(error=KeyboardInterrupt()))
    assert code == 130
    assert client.sandbox.deleted


def test_failure_create_error_creates_nothing_to_delete():
    code, _, _, lines = _run(client=FakeClient(create_error=RuntimeError("quota exceeded")))
    assert code == 1
    assert "Failed: RuntimeError: quota exceeded" in lines


def test_failure_delete_error_is_a_warning_not_a_traceback():
    code, _, _, lines = _run(sandbox=FakeSandbox(delete_error=RuntimeError("gone")))
    assert code == 0
    assert any("Warning: could not delete sandbox pr-review-test" in line for line in lines)


# --- main --------------------------------------------------------------------------------------

def _clear(monkeypatch):
    for name in (*review.REQUIRED_ENV, "GITHUB_TOKEN", "GITHUB_ACTIONS", "GITHUB_EVENT_PATH"):
        monkeypatch.delenv(name, raising=False)


def test_success_review_only_needs_only_the_model_key():
    assert missing_env({"NEEV_MODEL_API_KEY": "m"}, review_only=True) == []
    assert missing_env({}, review_only=True) == ["NEEV_MODEL_API_KEY"]


def test_failure_main_names_missing_variables(monkeypatch, capsys):
    _clear(monkeypatch)
    assert review.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_failure_main_needs_a_github_token_to_post(monkeypatch, capsys):
    _clear(monkeypatch)
    for name, value in FULL_ENV.items():
        monkeypatch.setenv(name, value)
    assert review.main(["--repo", "o/r", "--pr", "7"]) == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().err


def test_failure_main_rejects_a_bad_repository_name(monkeypatch, capsys):
    _clear(monkeypatch)
    for name, value in FULL_ENV.items():
        monkeypatch.setenv(name, value)
    assert review.main(["--repo", "o/r/../x", "--pr", "7", "--dry-run"]) == 2
    assert "Not a repository name" in capsys.readouterr().err


def test_failure_main_wants_repo_and_pr_together(monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        review.main(["--repo", "o/r"])
    assert exc.value.code == 2


def test_failure_main_rejects_a_negative_review_ceiling(monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        review.main(["--max-reviews", "-1"])
    assert exc.value.code == 2


def test_failure_main_reports_an_unreadable_guide(monkeypatch, capsys):
    _clear(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        review.main(["--guide", "/nonexistent/AGENTS.md"])
    assert exc.value.code == 2 and "cannot read --guide" in capsys.readouterr().err


def test_success_review_streams_the_reply():
    model = FakeModel()
    review_diff(model, "m", "t", DIFF)
    assert model.calls[0]["stream"] is True


def test_failure_review_that_outlasts_its_budget_stops(monkeypatch):
    clock = iter([0.0, review.REVIEW_BUDGET_S + 1] + [review.REVIEW_BUDGET_S + 2] * 10)
    monkeypatch.setattr(review.time, "monotonic", lambda: next(clock))
    with pytest.raises(ReviewError, match="took longer than 300s"):
        review_diff(FakeModel(), "m", "t", DIFF)
