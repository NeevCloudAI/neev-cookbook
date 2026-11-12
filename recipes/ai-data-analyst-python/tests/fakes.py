"""In-memory stand-ins for the sandbox's MCP session and a model client."""
import copy
import json
from types import SimpleNamespace

# Everything the real server lists, so tests can check the model only ever sees the tools it is given.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "interpreter_run", "process_start", "delete_sandbox",
                "rollback_sandbox", "expose_port")


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


def saves_chart(session, code):
    """Default kernel behaviour: a cell that calls savefig writes chart.png, and the cell's prints come back."""
    if "savefig" in code:
        session.files["chart.png"] = "\x89PNG..."
    return {"stdout": "ok\n", "stderr": "", "results": [], "end_reason": "ok", "generation": "g1"}


class FakeSession:
    """Mimics an MCP session bound to one sandbox: fs_* backed by a dict, interpreter_run runs `on_run(session, code)`."""

    def __init__(self, on_run=saves_chart):
        self.files = {}
        self.calls = []
        self.on_run = on_run
        self.raise_on = {}
        self.timeouts = []

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(name=n, description=f"{n} from the server", input_schema={"type": "object", "properties": {"x": {"type": "string"}}})
            for n in SERVER_TOOLS])

    async def call_tool(self, name, arguments=None, read_timeout_seconds=None):
        args = arguments or {}
        self.calls.append((name, args))
        self.timeouts.append(read_timeout_seconds)
        if name in self.raise_on:
            raise self.raise_on[name]
        if name == "fs_read":
            if args["path"] not in self.files:
                return _err(f'the sandbox refused this call: not_found: file not found: "{args["path"]}"')
            return _ok({"content": self.files[args["path"]], "size": len(self.files[args["path"]]), "eof": True})
        if name == "fs_list":
            return _ok({"entries": [{"name": n, "type": "file"} for n in sorted(self.files)]})
        if name == "interpreter_run":
            if set(args) - {"code", "context_id"}:  # the real schema sets additionalProperties: false
                return _err(f"invalid params: {sorted(args)}")
            return _ok(self.on_run(self, args["code"]))
        return _err(f"unexpected tool {name}")


class FakeModel:
    """Replays a scripted list of assistant messages, one per call, and records each request."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))  # snapshot: the loop keeps mutating messages
        msg = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def tool_call(name, arguments, call_id="c1"):
    """Builds an assistant message that calls one tool."""
    fn = SimpleNamespace(name=name, arguments=json.dumps(arguments))
    call = SimpleNamespace(id=call_id, type="function", function=fn)
    return SimpleNamespace(content=None, tool_calls=[call])


def text(content):
    """Builds an assistant message with text and no tool call."""
    return SimpleNamespace(content=content, tool_calls=None)
