"""Stand-ins for the sandbox (SDK and MCP session) and a model client, backed by a real local directory."""
import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

# Everything the real server lists, so tests can check each mode sees only its own tools.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox", "rollback_sandbox")


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


def _run(root: Path, program: str, args: list[str]):
    """Runs a program in the workspace directory; python3 is this interpreter so tests need no PATH setup."""
    argv = [sys.executable if program == "python3" else program, *args]
    done = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=30)
    return {"stdout": done.stdout, "stderr": done.stderr, "exit_code": done.returncode}


class FakeSession:
    """Mimics an MCP session bound to one sandbox whose workspace is the directory `root`."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.calls = []
        self.raise_on = {}

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(name=n, description=f"{n} from the server", input_schema={"type": "object", "properties": {"x": {"type": "string"}}})
            for n in SERVER_TOOLS])

    async def call_tool(self, name, arguments=None):
        args = arguments or {}
        self.calls.append((name, args))
        if name in self.raise_on:
            raise self.raise_on[name]
        path = args.get("path") or ""
        if path.startswith("/") or ".." in path.split("/"):
            return _err(f'the sandbox refused this call: path "{path}" leaves the workspace')
        target = self.root / path
        if name == "fs_write":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(args["content"])
            return _ok({"bytes_written": len(args["content"].encode())})
        if name == "fs_read":
            if not target.is_file():
                return _err(f"the sandbox refused this call: not found: {path}")
            return _ok({"content": target.read_text(), "size": target.stat().st_size, "eof": True})
        if name == "fs_list":
            if not target.is_dir():
                return _err(f"the sandbox refused this call: not found: {path}")
            return _ok({"entries": [{"name": p.name, "type": "directory" if p.is_dir() else "file", "path": str(p.relative_to(self.root))}
                                    for p in sorted(target.iterdir())]})
        if name == "exec":
            return _ok(_run(self.root, args["program"], args.get("args") or []))
        return _err(f"unexpected tool {name}")


class FakeSandbox:
    """Mimics the SDK sandbox handle: files.write/remove and exec act on the same directory as FakeSession."""

    def __init__(self, root: Path, name="code-mode-1"):
        self.root, self.name, self.deleted, self.execs = Path(root), name, False, []
        self.files = SimpleNamespace(write=self._write, remove=lambda path, **kw: (self.root / path).unlink())

    def _write(self, path, content, cwd=None):
        data = content.encode() if isinstance(content, str) else content
        (self.root / path).write_bytes(data)
        return {"bytes_written": len(data)}

    def wait_until_ready(self, timeout_ms=None):
        return self

    def exec(self, command, args=None, **kw):
        self.execs.append([command, *(args or [])])
        return SimpleNamespace(**_run(self.root, command, args or []))

    def delete(self):
        self.deleted = True


class FakeClient:
    """Mimics NeevAI().sandboxes.create and records the create params."""

    def __init__(self, sandbox):
        self.created = []

        def create(params):
            self.created.append(params)
            return sandbox

        self.sandboxes = SimpleNamespace(create=create)


class FakeModel:
    """Replays scripted assistant messages, one per call, with token usage, and records each request."""

    def __init__(self, replies, usage=(100, 10)):
        self.replies = list(replies)
        self.usage = usage
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))  # snapshot: the loop keeps mutating messages
        msg = self.replies.pop(0)
        usage = None if self.usage is None else SimpleNamespace(prompt_tokens=self.usage[0], completion_tokens=self.usage[1])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def tool_calls(*calls):
    """Builds an assistant message that calls one or more tools, each given as (name, arguments)."""
    built = [SimpleNamespace(id=f"c{i}", type="function", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
             for i, (name, args) in enumerate(calls)]
    return SimpleNamespace(content=None, tool_calls=built)


def tool_call(name, arguments):
    """Builds an assistant message that calls one tool."""
    return tool_calls((name, arguments))


def finish(top):
    """Builds an assistant message that calls finish with [(customer, count), ...]."""
    return tool_call("finish", {"top_customers": [{"customer": c, "failed_payments": n} for c, n in top]})


def text(content):
    """Builds an assistant message with text and no tool call."""
    return SimpleNamespace(content=content, tool_calls=None)
