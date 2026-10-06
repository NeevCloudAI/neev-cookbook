"""In-memory stand-ins for the sandbox's MCP session, the SDK sandbox and a model client."""
import copy
import io
import json
import zipfile
from types import SimpleNamespace

# Everything the real server lists, so tests can check the model only ever sees the tools it is given.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox", "rollback_sandbox", "expose_port")

PDF = (b"%PDF-1.4\n1 0 obj\n<</Type /Pages /Kids [3 0 R 4 0 R] /Count 2>>\nendobj\n"
       b"3 0 obj\n<</Type /Page /Parent 1 0 R>>\nendobj\n4 0 obj\n<</Type /Page /Parent 1 0 R>>\nendobj\n"
       b"5 0 obj\n<</Type /XObject /Subtype /Image /Width 10>>\nendobj\ntrailer\n<</Root 2 0 R>>\n%%EOF\n")


def xlsx(sheets=("Data", "Summary"), formulas=3):
    """Builds a minimal XLSX: the zip entries and XML a real workbook has, with `formulas` <f> cells."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", "<workbook><sheets>" + "".join(
            f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(sheets, 1)) + "</sheets></workbook>")
        for i, _ in enumerate(sheets, 1):
            cells = "".join(f'<c r="B{r}"><f>SUM(Data!E{r}:E{r})</f></c>' for r in range(formulas)) if i == len(sheets) else ""
            z.writestr(f"xl/worksheets/sheet{i}.xml", f"<worksheet><sheetData><row>{cells}</row></sheetData></worksheet>")
    return buf.getvalue()


def builds_report(session, script):
    """Default exec behaviour: a script writes a valid report.pdf and report.xlsx when it names them."""
    if "report.pdf" in script:
        session.files["report.pdf"] = PDF
    if "report.xlsx" in script:
        session.files["report.xlsx"] = xlsx()
    return {"exit_code": 0, "stdout": "ok\n", "stderr": ""}


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


class FakeSession:
    """Mimics an MCP session bound to one sandbox: fs_* backed by a dict, exec runs `on_exec(session, script)`."""

    def __init__(self, files=None, on_exec=builds_report):
        self.files = {} if files is None else files
        self.calls = []
        self.on_exec = on_exec
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
        if name == "fs_write":
            path = args["path"]
            if path.startswith("/") or ".." in path.split("/"):
                return _err(f'the sandbox refused this call: invalid_argument: path "{path}" escapes workspace root')
            self.files[path] = args["content"]
            return _ok({"bytes_written": len(args["content"])})
        if name == "fs_read":
            if args["path"] not in self.files:
                return _err(f'the sandbox refused this call: not_found: file not found: "{args["path"]}"')
            return _ok({"content": str(self.files[args["path"]]), "eof": True})
        if name == "fs_list":
            return _ok({"entries": [{"name": n, "type": "file"} for n in sorted(self.files)]})
        if name == "exec":
            script = self.files.get(next((a for a in args.get("args") or [] if a.endswith(".py")), ""), "")
            return _ok(self.on_exec(self, script))
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


SCRIPT = "import pandas\n# writes report.pdf and report.xlsx\n"


def building_model(findings="- Engineering cloud spend rose 38%"):
    """A model that writes the build script, runs it, then finishes."""
    return FakeModel([tool_call("fs_write", {"path": "build_report.py", "content": SCRIPT}),
                      tool_call("exec", {"program": "python3", "args": ["build_report.py"]}, "c2"),
                      tool_call("finish", {"findings": findings}, "c3")])
