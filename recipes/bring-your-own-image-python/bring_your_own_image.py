"""Bring your own image: run your toolchain image in a NeevCloud sandbox, then snapshot and fork it."""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import secrets
import sys
import time

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID")
DEFAULT_IMAGE = "docker.io/library/node:22-bookworm"
PROJECT_DIR = pathlib.Path(__file__).parent / "project"
# Commands start in the image's working directory (/ for node), and the files API only reaches /workspace.
WORKDIR = "/workspace/cart"
TEST_COMMAND = ["node", "--test", "--test-reporter=tap", "cart.test.js"]
BUG = "subtotal - discountPercent"
FIX = "subtotal * (1 - discountPercent / 100)"
RESULT = re.compile(r"^(ok|not ok) \d+ - (.+)$")
DETAIL = re.compile(r"^  (expected|actual): (.+)$")


class RunFailed(Exception):
    """The run cannot go on; the message says why."""


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def image_problem(image: str) -> str | None:
    """Says what is wrong with an image reference the platform would reject, or None if it looks usable."""
    if "/" not in image:
        return f"{image}: use a full reference with a registry or namespace, for example {DEFAULT_IMAGE}"
    if "@" not in image and ":" not in image.rsplit("/", 1)[1]:
        return f"{image}: add an explicit tag or digest, for example {image}:<tag>"
    return None


def load_project(root: pathlib.Path) -> dict[str, str]:
    """Reads the bundled project as {file name: text}."""
    return {p.name: p.read_text() for p in sorted(root.glob("*.js"))}


def test_results(tap: str) -> list[tuple[bool, str, str]]:
    """Reads each test from node's TAP output as (passed, name, why it failed)."""
    results = []
    for line in tap.splitlines():
        if found := RESULT.match(line):
            results.append((found[1] == "ok", found[2], {}))
        elif results and (found := DETAIL.match(line)):
            results[-1][2][found[1]] = found[2]
    return [(ok, name, f"expected {d['expected']}, got {d['actual']}" if len(d) == 2 else "")
            for ok, name, d in results]


def run_tests(sandbox, log) -> bool:
    """Runs the project's tests in the sandbox, prints one line per test, and says whether all passed."""
    result = sandbox.exec(TEST_COMMAND, cwd=WORKDIR, timeout_ms=60_000)
    results = test_results(result.stdout)
    if not results:
        raise RunFailed(f"the test run printed no results: {(result.stdout + result.stderr).strip()[-300:]}")
    for ok, name, why in results:
        log(f"   {'pass' if ok else 'FAIL'}  {name}" + (f" ({why})" if why else ""))
    failed = sum(not ok for ok, _, _ in results)
    log(f"   {len(results) - failed} passed, {failed} failed")
    return result.exit_code == 0 and failed == 0


def check_node(sandbox, image: str) -> str:
    """Returns the image's Node.js version, or fails naming what the default flow needs."""
    result = sandbox.exec(["node", "--version"])
    found = re.match(r"v(\d+)\.", result.stdout.strip())
    if result.exit_code != 0 or not found or int(found[1]) < 20:
        raise RunFailed(f"{image} has no Node.js 20 or newer on its PATH, which this demo needs for `node --test`."
                        f" Try {DEFAULT_IMAGE}.")
    return result.stdout.strip()


def _lookup(client, name: str, log) -> list:
    """Finds the sandbox with exactly this name, if one exists, so it can be deleted too."""
    try:
        return [sb for sb in client.sandboxes.list(name=name, limit=100).items if sb.name == name]
    except Exception as e:
        log(f"   Could not check for a sandbox named {name}: {e}. Look for it in the console.")
        return []


def run(image: str, client, project: dict[str, str], log=print, clock=time.monotonic) -> int:
    """Runs the tests in a sandbox from image, forks it, fixes the bug in the fork only, and deletes both."""
    made = []
    name = f"byoi-{secrets.token_hex(4)}"
    # A create or fork whose reply was lost may still have made the sandbox; finally looks this name up.
    asked = name
    try:
        log(f"1. Creating a sandbox from {image}...")
        start = clock()
        original = client.sandboxes.create({"name": name, "image": image})
        made.append(original)
        original.wait_until_ready(timeout_ms=300_000)
        log(f"   Ready in {clock() - start:.1f} s, with Node.js {check_node(original, image)}")
        log(f"2. Uploading {', '.join(project)} and running the tests...")
        for file, text in project.items():
            original.files.write(f"{WORKDIR}/{file}", text)
        if run_tests(original, log):
            raise RunFailed("the tests passed before the fix; the planted bug is missing from cart.js")
        log("3. Taking a snapshot of the failing state...")
        start = clock()
        # A fork while a snapshot is still capturing is refused (409), so wait for Ready.
        original.snapshot({"name": "before-fix"}, wait_until_ready=True)
        log(f"   Snapshot Ready in {clock() - start:.1f} s")
        log("4. Forking the sandbox...")
        start = clock()
        asked = f"{name}-fork"
        fork = original.fork(asked)
        made.append(fork)
        fork.wait_until_ready(timeout_ms=300_000)
        log(f"   Fork {fork.name} Ready in {clock() - start:.1f} s")
        log("5. Fixing the bug in the fork only, then running the tests there...")
        source = fork.files.read_text(f"{WORKDIR}/cart.js")
        fork.files.write(f"{WORKDIR}/cart.js", source.replace(BUG, FIX))
        fixed = run_tests(fork, log)
        log(f"6. Running the tests again in the original, {original.name}...")
        still_broken = not run_tests(original, log)
        if not (fixed and still_broken):
            raise RunFailed("expected the fork to pass and the original to still fail")
        log("   Two independent copies of one image: the fork is fixed, the original still has the bug.")
        return 0
    except KeyboardInterrupt:
        return 130
    except RunFailed as e:
        log(f"Failed: {e}")
        return 1
    except Exception as e:  # e.g. an image the platform cannot pull: one line instead of a traceback
        log(f"Failed: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")
        return 1
    finally:
        if asked not in {sb.name for sb in made}:
            made += _lookup(client, asked, log)
        for sandbox in reversed(made):
            try:
                sandbox.delete()
                log(f"   Deleted {sandbox.name}.")
            except (Exception, KeyboardInterrupt) as e:  # a second Ctrl+C must not skip the other sandbox
                log(f"   Could not delete {sandbox.name}: {e}. Delete it from the console.")


def main(argv=None) -> int:
    """Parses arguments, checks the environment and the image reference, and runs the recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE, help=f"a public image with Node.js 20+ (default {DEFAULT_IMAGE})")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    problem = image_problem(args.image)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    from neevai import NeevAI

    with NeevAI() as client:
        return run(args.image, client, load_project(PROJECT_DIR))


if __name__ == "__main__":
    sys.exit(main())
