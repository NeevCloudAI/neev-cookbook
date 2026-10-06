import contextlib
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import fix_test
from tests.fakes import TEST_CMD, FakeClient, FakeModel, FakeSandbox, FakeSession, shell, text, tool_call

BUGGY = b'def tier(q):\n    return 5 if q >= 10 else 0\n'
FILES = {"shop/__init__.py": b"", "shop/cart.py": BUGGY, "tests/__init__.py": b"",
         "tests/test_cart.py": b"import unittest\n", "README.md": b"# shop\n"}
FIX = {"path": "shop/cart.py", "content": "# FIXED\n" + BUGGY.decode()}


def git_repo(root: Path, files=FILES, subdir="") -> Path:
    """Creates a git repository at root with files staged under subdir, and returns the subdir's path."""
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for path, data in files.items():
        target = root / subdir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root / subdir


def connector(sandbox):
    """Returns a connect(sandbox_name) factory yielding an MCP session on the fake sandbox, recording the name."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        yield FakeSession(sandbox)

    connect.names = names
    return connect


def fixing_model():
    return FakeModel([tool_call("fs_read", {"path": "shop/cart.py"}), tool_call("fs_write", FIX, "c2"),
                      tool_call("finish", {"summary": "fixed the tier order"}, "c3")])


def run(tmp_path, sandbox, model, lines=None, repo=None, **kw):
    repo = repo or fix_test.read_repo(git_repo(tmp_path / "repo"))
    out = tmp_path / "fix.patch"
    code = fix_test.run(repo, TEST_CMD, out, kw.pop("client", None) or FakeClient(sandbox), model, "m",
                        kw.pop("connect", None) or connector(sandbox),
                        log=lines.append if lines is not None else (lambda *_: None))
    return code, out


def test_missing_env_names_every_missing_variable():
    assert fix_test.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in fix_test.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert fix_test.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


@pytest.fixture
def env(monkeypatch):
    for name in fix_test.REQUIRED_ENV:
        monkeypatch.setenv(name, "x")


def test_main_rejects_a_directory_that_is_not_a_git_repository(env, tmp_path, capsys):
    assert fix_test.main(["--repo", str(tmp_path)]) == 2
    assert "not a git repository" in capsys.readouterr().err


def test_main_refuses_to_write_the_patch_inside_the_repository(env, tmp_path, capsys):
    repo = git_repo(tmp_path / "repo")
    assert fix_test.main(["--repo", str(repo), "--out", str(repo / "fix.patch")]) == 2
    assert "outside the repository" in capsys.readouterr().err


def test_main_rejects_an_output_folder_that_does_not_exist(env, tmp_path, capsys):
    repo = git_repo(tmp_path / "repo")
    assert fix_test.main(["--repo", str(repo), "--out", str(tmp_path / "missing" / "fix.patch")]) == 2
    assert "does not exist" in capsys.readouterr().err


@pytest.mark.parametrize("path,expected", [
    ("tests/test_cart.py", True), ("tests/__init__.py", True), ("unit_tests/check.py", True), ("testdata/x.py", True),
    ("test_cart.py", True), ("test.py", True), ("testcart.py", True), ("cart_test.py", True), ("conftest.py", True),
    ("shop/cart.py", False), ("latest.py", False), ("contest.py", False), ("README.md", False),
])
def test_is_test_file_matches_what_test_runners_collect(path, expected):
    assert fix_test.is_test_file(path) is expected


@pytest.mark.parametrize("path,expected", [
    ("shop/cart.py", False), ("tests/test_cart.py", True), ("README.md", True), ("run_tests.sh", True),
    ("pyproject.toml", True), ("setup.cfg", True), ("data/expected.json", True),
])
def test_only_python_source_outside_the_tests_may_change(path, expected):
    assert fix_test.is_read_only(path) is expected


def test_inspect_script_reports_changed_deleted_and_new_files_and_skips_caches(tmp_path):
    for path, data in {"a.py": b"new", "b.py": b"same", "pkg/__pycache__/a.pyc": b"x", ".git/HEAD": b"x",
                       ".env": b"x", "pkg/helper.py": b"x"}.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_bytes(data)
    done = subprocess.run([sys.executable, "-c", fix_test.INSPECT_SCRIPT], cwd=tmp_path, capture_output=True,
                          text=True, input=json.dumps(["a.py", "b.py", "gone.py"]), check=True)
    out = json.loads(done.stdout)
    assert out["hashes"] == {"a.py": hashlib.sha256(b"new").hexdigest(), "b.py": hashlib.sha256(b"same").hexdigest(),
                             "gone.py": None}
    assert out["new"] == [".env", "pkg/helper.py"]


def test_read_repo_reads_tracked_files_only_and_skips_symlinks(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "untracked.py").write_text("secret = 1\n")
    (repo / "link.py").symlink_to("/etc/hosts")
    subprocess.run(["git", "-C", str(repo), "add", "link.py"], check=True)
    read = fix_test.read_repo(repo)
    assert read.files == FILES
    assert read.prefix == "" and Path(read.toplevel) == repo.resolve()


def test_read_repo_of_a_subdirectory_keeps_its_place_in_the_repository(tmp_path):
    read = fix_test.read_repo(git_repo(tmp_path / "mono", subdir="services/shop"))
    assert read.prefix == "services/shop/" and read.files == FILES


def test_read_repo_enforces_the_size_cap(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    monkeypatch.setattr(fix_test, "MAX_FILES", 3)
    with pytest.raises(fix_test.RepoError, match="5 tracked files"):
        fix_test.read_repo(repo)
    monkeypatch.setattr(fix_test, "MAX_FILES", 100)
    monkeypatch.setattr(fix_test, "MAX_BYTES", 10)
    with pytest.raises(fix_test.RepoError, match="bytes"):
        fix_test.read_repo(repo)


def test_read_repo_needs_at_least_one_test_file(tmp_path):
    with pytest.raises(fix_test.RepoError, match="no test files"):
        fix_test.read_repo(git_repo(tmp_path / "repo", files={"app.py": b"x = 1\n"}))


@pytest.mark.parametrize("prefix", ["", "services/shop/"])
def test_patch_round_trips_through_git_apply_even_without_a_final_newline(prefix):
    changes = {"shop/cart.py": (b"a\nb\nc", b"a\nB\nc"), "shop/z.py": (b"x\n", b"x\ny\n"),
               # characters str.splitlines would split on, though git does not
               "shop/odd.py": (b"a\x0cb\nc\rd\n", b"a\x0cb\nC\rd\ns = '\xe2\x80\xa8'\n")}
    patch = fix_test.make_patch(changes, prefix)
    assert f"--- a/{prefix}shop/cart.py" in patch and "\\ No newline at end of file" in patch
    fix_test.check_patch(patch, changes, prefix)


def test_check_patch_rejects_a_patch_that_does_not_apply():
    patch = fix_test.make_patch({"x.py": (b"one\n", b"two\n")}, "")
    with pytest.raises(RuntimeError, match="does not apply"):
        fix_test.check_patch(patch, {"x.py": (b"something else\n", b"two\n")}, "")


def test_happy_path_verifies_the_fix_in_a_fresh_sandbox_and_writes_the_patch(tmp_path):
    sb, lines = FakeSandbox(), []
    client, connect = FakeClient(sb), connector(sb)
    code, out = run(tmp_path, sb, fixing_model(), lines, client=client, connect=connect)
    assert code == 0
    assert [p["egress"] for p in client.created] == [{"mode": "deny_all"}] * 2
    assert all(p["name"].startswith("fix-test-") for p in client.created)
    assert client.created[0]["name"] != client.created[1]["name"]
    assert connect.names == [sb.name]
    # failed in the agent's sandbox; passed in a fresh one holding the originals plus only the patched file
    verify = client.sandboxes_made[1]
    assert sb.test_runs == [("/workspace", False)] and verify.test_runs == [("/workspace", True)]
    assert verify.workspace == {**FILES, "shop/cart.py": FIX["content"].encode()}
    patch = out.read_text()
    assert "+++ b/shop/cart.py" in patch and "+# FIXED" in patch and "test_cart" not in patch
    assert sb.deleted and verify.deleted
    log = "\n".join(lines)
    assert "unchanged: 3 read-only files" in log and "git apply" in log and str(out) in log
    # the agent's sandbox is gone before the fresh one is created, so a run holds one at a time
    assert log.index("Sandbox deleted") < log.index("6. ")


def test_tests_that_already_pass_are_not_a_success(tmp_path):
    sb, lines = FakeSandbox(passes=lambda files: True), []
    code, out = run(tmp_path, sb, FakeModel([]), lines)
    assert code == 1 and sb.deleted and not out.exists()
    assert any("already pass" in l for l in lines)


def test_an_agent_that_edits_a_test_file_fails_the_run(tmp_path):
    sb, lines = FakeSandbox(), []
    model = FakeModel([shell("echo pass >> tests/test_cart.py"), tool_call("fs_write", FIX, "c2"),
                       tool_call("finish", {"summary": "ok"}, "c3")])
    code, out = run(tmp_path, sb, model, lines)
    assert code == 1 and sb.deleted and not out.exists()
    assert any("changed read-only files: tests/test_cart.py" in l for l in lines)


def test_a_deleted_test_file_counts_as_changed(tmp_path):
    sb, lines = FakeSandbox(), []
    model = FakeModel([shell("rm tests/test_cart.py"), tool_call("fs_write", FIX, "c2"), tool_call("finish", {"summary": "ok"}, "c3")])
    code, _ = run(tmp_path, sb, model, lines)
    assert code == 1 and any("tests/test_cart.py" in l for l in lines)


def test_an_agent_that_changes_nothing_fails_the_run(tmp_path):
    sb, lines = FakeSandbox(), []
    code, out = run(tmp_path, sb, FakeModel([text("I could not find the bug.")] * 3), lines)
    assert code == 1 and sb.deleted and not out.exists()
    assert any("did not change any source file" in l for l in lines)


def test_a_fix_that_depends_on_a_new_file_fails_in_the_fresh_sandbox(tmp_path):
    sb = FakeSandbox(passes=lambda f: b"FIXED" in f.get("shop/cart.py", b"") and "shop/helper.py" in f)
    lines = []
    model = FakeModel([tool_call("fs_write", {"path": "shop/helper.py", "content": "x = 1\n"}),
                       tool_call("fs_write", FIX, "c2"), tool_call("finish", {"summary": "ok"}, "c3")])
    code, out = run(tmp_path, sb, model, lines)
    assert code == 1 and not out.exists()
    log = "\n".join(lines)
    assert "ignored new file: shop/helper.py" in log and "still fail in a fresh sandbox" in log


def test_ctrl_c_while_deleting_the_agent_sandbox_still_deletes_it(tmp_path):
    sb, calls = FakeSandbox(), []

    def interrupted_once():
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt
        sb.deleted = True

    sb.delete = interrupted_once
    code, _ = run(tmp_path, sb, fixing_model())
    assert code == 130 and sb.deleted and len(calls) == 2


def test_ctrl_c_deletes_the_sandbox(tmp_path):
    sb = FakeSandbox()

    def interrupt(path, content):
        raise KeyboardInterrupt

    sb.files.write = interrupt
    code, _ = run(tmp_path, sb, FakeModel([]))
    assert code == 130 and sb.deleted


def test_an_error_wrapped_in_an_exception_group_is_one_line_and_deletes(tmp_path):
    sb, lines = FakeSandbox(), []

    async def broken(**kwargs):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("MCP stream closed")])

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=broken)))
    code, _ = run(tmp_path, sb, model, lines)
    assert code == 1 and sb.deleted
    assert any(l == "Failed: ConnectionError: MCP stream closed" for l in lines)


def test_a_failed_delete_keeps_the_exit_code_and_says_what_to_clean_up(tmp_path):
    sb, lines = FakeSandbox(), []

    def refuse():
        raise RuntimeError("503")

    sb.delete = refuse
    code, _ = run(tmp_path, sb, fixing_model(), lines)
    assert code == 0 and any("delete it from the console" in l for l in lines)


def test_runs_started_in_the_same_second_get_different_sandbox_names(tmp_path):
    repo = fix_test.read_repo(git_repo(tmp_path / "repo"))
    names = []
    for _ in range(2):
        sb = FakeSandbox()
        client = FakeClient(sb)
        run(tmp_path, sb, fixing_model(), repo=repo, client=client)
        names += [p["name"] for p in client.created]
    assert len(set(names)) == 4
