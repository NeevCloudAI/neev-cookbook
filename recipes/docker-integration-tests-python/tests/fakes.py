"""In-memory stand-ins for a sandbox's files, exec and background processes."""
from types import SimpleNamespace

import compose_tests

UP_OUTPUT = [" cache Pulled \n api Pulled \n Container sample-app-cache-1  Creating\n",
             " db Pulled \n Container sample-app-db-1  Healthy\n Container sample-app-cache-1  Hea", "lthy\n",
             " Container sample-app-db-1  Healthy\n Container sample-app-api-1  Healthy\n"]
TESTS_PASS = ["bun test v1.4.3\n", "(pass) health check answers [3.93ms]\n(pass) creating a note [8ms]\n", "\n 2 pass\n 0 fail\n"]
TESTS_FAIL = ["(pass) health check answers [3.93ms]\n(fail) creating a note [8ms]\n", "\n 1 pass\n 1 fail\n"]


class FakeProcesses:
    """Plays back a scripted (chunks, exit code) per command, one page of output, then the exit; "hang" never exits."""

    def __init__(self, script):
        self.script = script
        self.started, self.killed = [], []

    def start(self, program, cwd=None):
        command = program[2]
        self.started.append((command, cwd))
        return SimpleNamespace(id=command)

    def logs(self, process_id, cursor=None):
        chunks, exit_code = self.script[process_id]
        if exit_code == "hang":
            return SimpleNamespace(entries=[], cursor=0, state="running")
        if cursor is None:
            entries = [SimpleNamespace(stream="stderr", data=c) for c in chunks]
            return SimpleNamespace(entries=entries, cursor=1, state="running")
        return SimpleNamespace(entries=[], cursor=1, state="exited")

    def get(self, process_id):
        return SimpleNamespace(exit_code=self.script[process_id][1])

    def kill(self, process_id):
        self.killed.append(process_id)
        return True


class FakeSandbox:
    def __init__(self, script, versions="29.1.3\n2.40.3+ds1-0ubuntu1\n"):
        self.name = "compose-tests-1"
        self.versions = versions
        self.urls = []
        self.files_written = {}
        self.deleted = False
        self.processes = FakeProcesses(script)
        self.files = SimpleNamespace(write=lambda path, data: self.files_written.__setitem__(path, data))

    def wait_until_ready(self, timeout_ms=None):
        return self

    def exec(self, argv):
        return SimpleNamespace(stdout=self.versions, exit_code=0 if self.versions else 1)

    def get_url(self, port):
        self.urls.append(port)
        return f"https://{port}-compose-tests-1.preview.example"

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


def script(up=(UP_OUTPUT, 0), tests=(TESTS_PASS, 0), down=([], 0)):
    return {compose_tests.COMPOSE_UP: up, compose_tests.RUN_TESTS: tests, compose_tests.COMPOSE_DOWN: down}
