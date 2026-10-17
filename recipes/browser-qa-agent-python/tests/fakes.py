"""In-memory stand-ins for the NeevAI client and a Playwright sandbox."""
import json
from types import SimpleNamespace


def report(*visits, broken=(), checked=3, not_checked=0, screenshots=("phone.png", "tablet.png", "desktop.png")):
    """Builds a qa.js report from (viewport, url, status, ms, errors) tuples."""
    pages = [{"viewport": v, "url": u, "status": s, "ms": ms, "errors": list(e)} for v, u, s, ms, e in visits]
    return {"url": pages[0]["url"] if pages else "", "pages": pages, "screenshots": list(screenshots),
            "links": {"checked": checked, "not_checked": not_checked, "broken": list(broken)}}


DEMO_REPORT = report(
    ("phone", "http://127.0.0.1:8080/", 200, 300, []),
    ("phone", "http://127.0.0.1:8080/about.html", 200, 310, ["loadTeamPhotos is not defined"]),
    ("tablet", "http://127.0.0.1:8080/", 200, 290, []),
    ("tablet", "http://127.0.0.1:8080/about.html", 200, 295, ["loadTeamPhotos is not defined"]),
    ("desktop", "http://127.0.0.1:8080/", 200, 280, []),
    ("desktop", "http://127.0.0.1:8080/about.html", 200, 285, ["loadTeamPhotos is not defined"]),
    broken=[{"url": "http://127.0.0.1:8080/menu.html", "status": 404}],
)


class FakeSandbox:
    """Records uploads, processes and commands; the QA run replays scripted output and a report."""

    def __init__(self, qa_report=DEMO_REPORT, qa_events=None, site_up=True):
        self.files_written = {}
        self.started = []
        self.streamed = []
        self.downloaded = []
        self.deleted = False
        self.site_up = site_up
        self.qa_report = qa_report
        self.qa_events = qa_events if qa_events is not None else [
            {"type": "stdout", "data": "phone   200   300 ms\n"}, {"type": "exit", "exit_code": 0}]
        self.files = SimpleNamespace(write=self._write, read_text=self._read_text, download_file=self._download)
        self.processes = SimpleNamespace(start=lambda argv: self.started.append(argv))

    def wait_until_ready(self, timeout_ms=None):
        return self

    def exec(self, argv, timeout_ms=None):
        return SimpleNamespace(exit_code=0 if self.site_up else 1, stdout="", stderr="")

    def exec_stream(self, argv, timeout_ms=None):
        self.streamed.append(argv)
        for event in self.qa_events:
            if isinstance(event, BaseException):
                raise event
            yield event

    def delete(self):
        self.deleted = True

    def _write(self, path, content):
        self.files_written[path] = content

    def _read_text(self, path):
        assert path == "qa-out/report.json"
        return json.dumps(self.qa_report)

    def _download(self, remote, local):
        self.downloaded.append(remote)
        local.write_bytes(b"x")


class FakeClient:
    """Mimics NeevAI().sandboxes.create and records the create params."""

    def __init__(self, sandbox):
        self.created = []

        def create(params):
            self.created.append(params)
            return sandbox

        self.sandboxes = SimpleNamespace(create=create)
