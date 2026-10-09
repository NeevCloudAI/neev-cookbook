import base64
import json

import pytest

import review
from review import (
    COMMENT_MARKER, GitHub, ReviewError, comment_body, fetch_pull_request, git_auth_env, missing_env,
    pr_from_actions, review_diff, run, run_tests, wait_for_host,
)
from tests.fakes import BASE, HEAD, FakeClient, FakeGitHubAPI, FakeModel, FakeSandbox

PR = {"title": "Add discounts", "base": BASE, "head": HEAD}
FULL_ENV = {"NEEV_API_KEY": "k", "NEEV_ORG_ID": "o", "NEEV_PROJECT_ID": "p", "NEEV_MODEL_API_KEY": "m"}


def _run(sandbox=None, client=None, model=None, api=None, post=False, test_cmd="npm test", lines=None):
    """Runs the recipe against fakes and returns (exit code, client, api, printed lines)."""
    client = client or FakeClient(sandbox or FakeSandbox())
    api = api or FakeGitHubAPI()
    lines = [] if lines is None else lines
    code = run("o/r", 7, test_cmd, ["registry.npmjs.org"], post, client, model or FakeModel(), "glm-4-7",
               GitHub("o/r", "ghs_token", urlopen=api), log=lines.append)
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
    assert ["diff", "--no-color", "--no-ext-diff", f"{BASE}...{HEAD}"] == next(
        args for args, _ in git_calls if "diff" in args)[2:]


def test_success_fetch_waits_for_the_network(monkeypatch):
    monkeypatch.setattr(review.time, "sleep", lambda s: None)
    sandbox = FakeSandbox(dns_failures=3)
    fetch_pull_request(sandbox, "o/r", PR, None)
    assert [c for c, _, _ in sandbox.execs].count("getent") == 4


def test_failure_wait_for_host_gives_up_after_its_budget():
    with pytest.raises(ReviewError, match="could not resolve github.com"):
        wait_for_host(FakeSandbox(dns_failures=99), "github.com", timeout_s=0, wait=lambda s: None)


def test_failure_fetch_reports_gits_error():
    with pytest.raises(ReviewError, match="git fetch failed: fatal: repository not found"):
        fetch_pull_request(FakeSandbox(fetch_exit=128), "o/r", PR, None)


# --- review and tests --------------------------------------------------------------------------

def test_success_review_sends_the_diff_and_strips_reasoning():
    model = FakeModel(reply="<think>hmm</think>- looks fine")
    assert review_diff(model, "glm-4-7", "Add discounts", "+x\n") == "- looks fine"
    user = model.calls[0]["messages"][1]["content"]
    assert "Add discounts" in user and "<diff>\n+x\n\n</diff>" in user
    assert "untrusted" in model.calls[0]["messages"][0]["content"]


def test_success_review_cuts_a_large_diff_and_says_so():
    model = FakeModel(reply="- ok")
    text = review_diff(model, "m", "t", "+" * (review.MAX_DIFF_CHARS + 10))
    assert text.endswith("characters of the diff were reviewed._")
    assert len(model.calls[0]["messages"][1]["content"]) < review.MAX_DIFF_CHARS + 100


def test_success_review_of_an_empty_diff_skips_the_model():
    model = FakeModel()
    assert "no changes" in review_diff(model, "m", "t", "  \n")
    assert model.calls == []


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


# --- the comment -------------------------------------------------------------------------------

def test_success_comment_carries_marker_status_and_output():
    body = comment_body("- bug", "npm test", 1, "not ok 1\n", HEAD)
    assert body.startswith(COMMENT_MARKER)
    assert "### Review of bbbbbbb" in body and "### Tests failed (exit 1)" in body and "not ok 1" in body


def test_success_comment_quiets_mentions_and_cannot_be_closed_by_output():
    body = comment_body("ping @octocat", "npm test", 0, "````\nsneaky\n", HEAD)
    assert "@octocat" not in body and "@​octocat" in body
    assert "`````\n````\nsneaky" in body


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


def test_success_upsert_updates_this_recipes_earlier_comment():
    api = FakeGitHubAPI({
        ("GET", "/repos/o/r/issues/7/comments?per_page=100&page=1"): [{"id": 1, "body": "lgtm"},
                                                                       {"id": 2, "body": f"{COMMENT_MARKER}\nold"}],
        ("PATCH", "/repos/o/r/issues/comments/2"): {"html_url": "https://github.com/o/r/pull/7#c2"},
    })
    assert GitHub("o/r", "t", urlopen=api).upsert_comment(7, "new") == "https://github.com/o/r/pull/7#c2"
    assert api.requests[-1][:3] == ("PATCH", "/repos/o/r/issues/comments/2", {"body": "new"})


def test_success_upsert_pages_then_posts_when_there_is_no_earlier_comment():
    api = FakeGitHubAPI({
        ("GET", "/repos/o/r/issues/7/comments?per_page=100&page=1"): [{"id": i, "body": "x"} for i in range(100)],
        ("GET", "/repos/o/r/issues/7/comments?per_page=100&page=2"): [],
        ("POST", "/repos/o/r/issues/7/comments"): {"html_url": "https://github.com/o/r/pull/7#c9"},
    })
    assert GitHub("o/r", "t", urlopen=api).upsert_comment(7, "new") == "https://github.com/o/r/pull/7#c9"
    assert [r[0] for r in api.requests] == ["GET", "GET", "POST"]


# --- the whole run -----------------------------------------------------------------------------

def test_success_run_prints_the_comment_and_deletes_the_sandbox():
    code, client, api, lines = _run()
    assert code == 0
    assert client.sandbox.deleted
    params, allow = client.created[0]
    assert allow == ["github.com", "registry.npmjs.org"]
    assert params["name"].startswith("pr-review-") and params["lifecycle"]["on_idle"] == "delete"
    assert any(line.startswith(COMMENT_MARKER) for line in lines)
    assert [r[0] for r in api.requests] == ["GET"]  # read the pull request, posted nothing


def test_success_github_access_is_removed_before_the_tests_run():
    sandbox = FakeSandbox()
    order = []
    sandbox.update = lambda params: order.append(("update", params))
    original = sandbox.exec_stream
    sandbox.exec_stream = lambda *a, **k: (order.append(("tests", None)), (yield from original(*a, **k)))[1]
    _run(sandbox=sandbox)
    assert order == [("update", {"egress_remove": {"allow": [{"host": "github.com"}]}}), ("tests", None)]


def test_success_run_posts_when_asked():
    api = FakeGitHubAPI({
        ("GET", "/repos/o/r/issues/7/comments?per_page=100&page=1"): [],
        ("POST", "/repos/o/r/issues/7/comments"): {"html_url": "https://github.com/o/r/pull/7#c1"},
    })
    code, _, _, lines = _run(api=api, post=True)
    assert code == 0
    assert "6. Posted the review: https://github.com/o/r/pull/7#c1" in lines
    assert api.requests[-1][2]["body"].startswith(COMMENT_MARKER)


def test_failure_failing_tests_fail_the_run_but_still_comment():
    sandbox = FakeSandbox(test_events=[{"type": "stdout", "data": "not ok\n"}, {"type": "exit", "exit_code": 1}])
    code, client, _, lines = _run(sandbox=sandbox)
    assert code == 1
    assert client.sandbox.deleted
    assert any("### Tests failed (exit 1)" in line for line in lines)


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
