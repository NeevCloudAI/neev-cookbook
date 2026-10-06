"""Fix the failing test: an AI agent fixes a Python repo in a NeevCloud sandbox, and you get a verified fix.patch."""
from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from agent import fix_code

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
DEFAULT_TEST_CMD = "python3 -m unittest"
FIXTURE = Path(__file__).parent / "fixture"
MAX_FILES = 300
MAX_BYTES = 5_000_000
TEST_TIMEOUT_MS = 120_000
# Reads the tracked paths as JSON on stdin; prints their sha256 (null if gone) and any other files, skipping caches.
INSPECT_SCRIPT = (
    "import hashlib, json, os, sys\n"
    "tracked = json.load(sys.stdin)\n"
    "hashes = {}\n"
    "for p in tracked:\n"
    "    try:\n"
    "        hashes[p] = hashlib.sha256(open(p, 'rb').read()).hexdigest()\n"
    "    except OSError:\n"
    "        hashes[p] = None\n"
    "found = []\n"
    "for d, dirs, names in os.walk('.'):\n"
    "    dirs[:] = [x for x in dirs if x != '__pycache__' and not x.startswith('.')]\n"
    "    found += [os.path.relpath(os.path.join(d, n)) for n in names]\n"
    "print(json.dumps({'hashes': hashes, 'new': sorted(set(found) - set(tracked))}))\n"
)


class RepoError(Exception):
    """The repository cannot be used: not a git repository, too big, or without tests."""


class Repo(NamedTuple):
    """The tracked files of a repository (or a folder in one) and where they sit in it."""

    path: Path
    toplevel: str  # the git top level, where `git apply` runs
    prefix: str  # the folder's path inside the top level, "" or ending in "/"
    files: dict[str, bytes]  # path relative to the folder -> content


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def mcp_connect(api_key: str):
    """Returns connect(sandbox_name): an MCP session on the sandbox MCP server, bound to that one sandbox."""
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    def connect(sandbox_name: str):
        headers = {"Authorization": f"Bearer {api_key}", "x-sandbox-name": sandbox_name}
        return Client(streamable_http_client(MCP_URL, http_client=create_mcp_http_client(headers=headers)))

    return connect


def is_test_file(path: str) -> bool:
    """Reports whether a path belongs to the test suite: a test* or *tests folder, test*.py, *_test.py or conftest.py."""
    *dirs, name = path.split("/")
    return (any(d.startswith("test") or d.endswith("tests") for d in dirs) or name == "conftest.py"
            or (name.startswith("test") and name.endswith(".py")) or name.endswith("_test.py"))


def is_read_only(path: str) -> bool:
    """Reports whether the agent must leave a file alone: only Python source outside the tests may change.

    Non-Python files are read-only too, so test data, configs and scripts cannot be bent to make the tests pass.
    """
    return is_test_file(path) or not path.endswith(".py")


def read_repo(path: Path) -> Repo:
    """Reads the tracked regular files of a git repository folder, enforcing the size cap; symlinks are skipped."""
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(path), *args], capture_output=True, check=True, text=True).stdout
        except (OSError, subprocess.CalledProcessError):
            raise RepoError(f"{path} is not a git repository (or git is not installed)") from None

    toplevel, prefix = git("rev-parse", "--show-toplevel").strip(), git("rev-parse", "--show-prefix").strip()
    names = [n for n in git("ls-files", "-z").split("\0") if n]
    # A symlink could point anywhere on this machine, so only regular files are uploaded.
    regular = [n for n in names if (path / n).is_file() and not (path / n).is_symlink()]
    if len(regular) > MAX_FILES:
        raise RepoError(f"{path} has {len(regular)} tracked files; this recipe takes at most {MAX_FILES}")
    files = {n: (path / n).read_bytes() for n in regular}
    size = sum(map(len, files.values()))
    if size > MAX_BYTES:
        raise RepoError(f"{path} has {size} bytes of tracked files; this recipe takes at most {MAX_BYTES}")
    if not any(map(is_test_file, files)):
        raise RepoError(f"{path} has no test files (tests/, test*.py or *_test.py)")
    return Repo(path, toplevel, prefix, files)


def _lines(data: bytes) -> list[str]:
    """Splits file content into lines on "\\n" only, as git does, keeping the endings (str.splitlines splits on more)."""
    parts = data.decode("utf-8").split("\n")
    return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def make_patch(changes: dict[str, tuple[bytes, bytes]], prefix: str) -> str:
    """Builds a git-style unified diff of {path: (old, new)}, with paths relative to the git top level."""
    out = []
    for path, (old, new) in sorted(changes.items()):
        full = prefix + path
        out.append(f"diff --git a/{full} b/{full}\n")
        for line in difflib.unified_diff(_lines(old), _lines(new), f"a/{full}", f"b/{full}"):
            # A last line without a newline needs git's marker, or the patch will not apply.
            out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def check_patch(patch: str, changes: dict[str, tuple[bytes, bytes]], prefix: str) -> None:
    """Applies the patch with git to a temporary copy of the original files and checks it yields the fixed ones."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for path, (old, _) in changes.items():
            (root / prefix / path).parent.mkdir(parents=True, exist_ok=True)
            (root / prefix / path).write_bytes(old)
        (root / "fix.patch").write_bytes(patch.encode())
        # Stop git from looking for a repository above the temporary folder.
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(root.parent)}
        for args in (["apply", "--check", "fix.patch"], ["apply", "fix.patch"]):
            done = subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, text=True)
            if done.returncode != 0:
                raise RuntimeError(f"fix.patch does not apply to the original files: {done.stderr.strip()}")
        for path, (_, new) in changes.items():
            if (root / prefix / path).read_bytes() != new:
                raise RuntimeError(f"fix.patch does not reproduce the fixed {path}")


def _count(n: int, noun: str) -> str:
    """Formats a count with its noun, plural unless n is 1."""
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _tail(text: str, lines: int = 8) -> str:
    """The last few lines of test output, indented for the progress log."""
    return "\n".join(f"   | {l}" for l in text.strip().splitlines()[-lines:])


def run_tests(sandbox, test_cmd: str, cwd: str) -> tuple[bool, str]:
    """Runs the test command in the sandbox and returns (passed, combined output)."""
    result = sandbox.exec(["sh", "-c", test_cmd], cwd=cwd, timeout_ms=TEST_TIMEOUT_MS)
    return result.exit_code == 0, result.stdout + result.stderr


def upload(sandbox, files: dict[str, bytes]) -> None:
    """Writes the files into the sandbox's workspace."""
    for path, data in files.items():
        sandbox.files.write(path, data)


def inspect_workspace(sandbox, files: dict[str, bytes]) -> tuple[dict[str, str | None], list[str]]:
    """Returns ({path: new sha256, or None if deleted} for tracked files that differ, [files the agent added])."""
    result = sandbox.exec(["python3", "-c", INSPECT_SCRIPT], cwd="/workspace", stdin=json.dumps(sorted(files)))
    if result.exit_code != 0:
        raise RuntimeError(f"could not read back the workspace: {result.stderr.strip()}")
    now = json.loads(result.stdout)
    changed = {p: h for p, h in now["hashes"].items() if h != hashlib.sha256(files[p]).hexdigest()}
    return changed, now["new"]


async def _fix(connect, sandbox_name: str, model_client, model: str, task: str, test_cmd: str,
               log) -> str:
    """Opens the MCP session for the sandbox and runs the agent loop over it."""
    try:
        async with connect(sandbox_name) as session:
            return await fix_code(session, model_client, model, task, test_cmd, is_read_only, log=log)
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


def new_sandbox(client, sandboxes: list):
    """Creates a sandbox with no internet access and records it in sandboxes before waiting, so cleanup always sees it."""
    sandbox = client.sandboxes.create({"name": f"fix-test-{secrets.token_hex(4)}", "egress": {"mode": "deny_all"}})
    sandboxes.append(sandbox)
    sandbox.wait_until_ready(timeout_ms=300_000)
    return sandbox


def delete(sandbox, log) -> None:
    """Deletes a sandbox; a failure is reported with what to clean up by hand, never raised."""
    try:
        sandbox.delete()
        log("   Sandbox deleted.")
    except Exception as e:  # keep the run's exit code
        log(f"   Could not delete sandbox {sandbox.name} ({type(e).__name__}: {e}); delete it from the console.")


def run(repo: Repo, test_cmd: str, out: Path, client, model_client, model: str, connect, log=print) -> int:
    """Has the agent fix the repo in a sandbox, verifies the result, writes the patch, and always deletes the sandbox.

    Returns 0 only when the tests pass in a fresh sandbox holding the original files plus the patch,
    every read-only file is byte-identical to the original, and the patch applies to the original files.
    """
    sandboxes = []
    read_only = sorted(p for p in repo.files if is_read_only(p))
    try:
        log("1. Creating a sandbox (no internet access)...")
        sandbox = new_sandbox(client, sandboxes)
        size = sum(map(len, repo.files.values()))
        log(f"2. Uploading {_count(len(repo.files), 'tracked file')} ({size / 1000:.1f} KB) from {repo.path}...")
        upload(sandbox, repo.files)
        log(f"3. Running the tests: {test_cmd}")
        passed, output = run_tests(sandbox, test_cmd, "/workspace")
        log(_tail(output))
        if passed:
            log("The tests already pass; there is nothing to fix.")
            return 1
        log(f"4. Asking {model} to fix the code ({_count(len(read_only), 'file')} read-only: tests and non-Python files)...")
        task = f"The tests fail. Running `{test_cmd}` printed:\n\n{output[-3000:]}\n\nFix the source code so they pass."
        summary = asyncio.run(_fix(connect, sandbox.name, model_client, model, task, test_cmd, log))
        log(f"   agent's summary: {summary}")
        log("5. Checking the agent's work...")
        changed, added = inspect_workspace(sandbox, repo.files)
        for path in added:  # never part of the patch, so the clean copy shows whether the fix needed them
            log(f"   ignored new file: {path}")
        touched = [p for p in changed if is_read_only(p)]
        if touched:
            log(f"   changed read-only files: {', '.join(touched)}")
            log("The agent edited files it had to leave alone, so its fix does not count.")
            return 1
        log(f"   unchanged: {_count(len(read_only), 'read-only file')}, byte for byte")
        for path in [p for p, h in changed.items() if h is None]:
            log(f"   ignored deleted file: {path}")
        fixes = {p: (repo.files[p], sandbox.files.read(p)) for p, h in changed.items() if h is not None}
        if not fixes:
            log("The agent did not change any source file.")
            return 1
        for path in fixes:
            log(f"   changed: {path}")
        patch = make_patch(fixes, repo.prefix)
        check_patch(patch, fixes, repo.prefix)  # before the second sandbox, so a patch that cannot apply fails fast
        # Forget the sandbox only after the delete call returns, so a Ctrl+C mid-delete still leaves it to finally.
        delete(sandbox, log)
        sandboxes.remove(sandbox)
        # A sandbox the agent never touched: nothing it changed or left running can sway this run.
        log("6. Running the tests in a fresh sandbox: the original files plus only the changed source files...")
        verify = new_sandbox(client, sandboxes)
        upload(verify, {**repo.files, **{p: new for p, (_, new) in fixes.items()}})
        passed, output = run_tests(verify, test_cmd, "/workspace")
        log(_tail(output))
        if not passed:
            log("The tests still fail in a fresh sandbox with the fix applied.")
            return 1
        out.write_bytes(patch.encode())
        log(f"7. Wrote {out}; it applies cleanly to the original files:\n")
        log(patch)
        log(f"Apply it with:\n   cd {repo.toplevel} && git apply {out.resolve()}")
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # e.g. a rejected model key: one line instead of a traceback
        cause = _root_cause(e)
        log(f"Failed: {type(cause).__name__}: {cause}")
        return 1
    finally:
        for sandbox in sandboxes:
            delete(sandbox, log)


def main(argv=None) -> int:
    """Parses arguments, checks the environment and the repository, and runs the recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=FIXTURE, help="a small Python git repository (default: the bundled fixture)")
    parser.add_argument("--test-cmd", default=DEFAULT_TEST_CMD, help=f"how to run the tests (default: {DEFAULT_TEST_CMD})")
    parser.add_argument("--out", type=Path, default=Path("fix.patch"), help="where to write the patch (default: ./fix.patch)")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    try:
        repo = read_repo(args.repo)
    except RepoError as e:
        print(e, file=sys.stderr)
        return 2
    if not args.out.parent.is_dir():
        print(f"The folder for --out does not exist: {args.out.parent}", file=sys.stderr)
        return 2
    if args.out.resolve().is_relative_to(repo.path.resolve()):
        print(f"Write the patch outside the repository folder so it stays untouched: --out {args.out} is inside {repo.path}.",
              file=sys.stderr)
        return 2
    from neevai import NeevAI
    from openai import AsyncOpenAI

    model_client = AsyncOpenAI(base_url=MODEL_BASE_URL, api_key=os.environ["NEEV_MODEL_API_KEY"])
    connect = mcp_connect(os.environ["NEEV_API_KEY"])
    with NeevAI() as client:
        return run(repo, args.test_cmd, args.out, client, model_client, os.environ.get("MODEL", DEFAULT_MODEL), connect)


if __name__ == "__main__":
    sys.exit(main())
