import pytest

import bring_your_own_image as byoi
from tests.fakes import FakeClient, tap

PROJECT = byoi.load_project(byoi.PROJECT_DIR)


def run(client, **kw):
    lines = []
    code = byoi.run(byoi.DEFAULT_IMAGE, client, kw.get("project", PROJECT), log=lines.append)
    return code, lines


def test_missing_env_names_every_missing_variable():
    assert byoi.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID"]


@pytest.mark.parametrize("image, ok", [
    (byoi.DEFAULT_IMAGE, True),
    ("ghcr.io/acme/toolchain@sha256:" + "a" * 64, True),
    ("registry.example.com:5000/team/node:22", True),
    ("node:22", False),
    ("docker.io/library/node", False),
    ("registry.example.com:5000/team/node", False),
])
def test_image_problem_wants_a_slash_and_an_explicit_tag_or_digest(image, ok):
    assert (byoi.image_problem(image) is None) == ok


def test_bundled_project_has_the_planted_bug_and_its_tests():
    assert sorted(PROJECT) == ["cart.js", "cart.test.js"]
    assert byoi.BUG in PROJECT["cart.js"]


def test_test_results_reads_each_test_and_why_it_failed():
    assert byoi.test_results(tap(byoi.BUG)[1]) == [
        (True, "adds up price times quantity", ""),
        (True, "an empty cart costs nothing", ""),
        (False, "takes 10% off", "expected 1800, got 1990"),
    ]


def test_happy_path_fixes_only_the_fork_and_deletes_both():
    client = FakeClient()
    code, lines = run(client)
    assert code == 0, lines
    original, fork = client.made
    assert client.created == [{"name": original.name, "image": byoi.DEFAULT_IMAGE}]
    assert original.name.startswith("byoi-") and fork.name == f"{original.name}-fork"
    assert original.snapshots == [({"name": "before-fix"}, True)]
    assert byoi.BUG in original.store[f"{byoi.WORKDIR}/cart.js"]
    assert byoi.FIX in fork.store[f"{byoi.WORKDIR}/cart.js"]
    assert all(cwd == byoi.WORKDIR for cmd, cwd in original.execs + fork.execs if cmd[:2] == ["node", "--test"])
    assert any("FAIL  takes 10% off (expected 1800, got 1990)" in l for l in lines)
    assert [l for l in lines if "Ready in" in l][2].startswith(f"   Fork {fork.name} Ready in")
    assert original.deleted and fork.deleted


def test_an_image_without_node_fails_with_what_the_demo_needs():
    client = FakeClient(node=None)
    code, lines = run(client)
    assert code == 1 and client.made[0].deleted
    assert "Node.js 20 or newer" in lines[-2] and byoi.DEFAULT_IMAGE in lines[-2]


def test_an_old_node_is_refused():
    client = FakeClient(node="v18.12.0")
    assert run(client)[0] == 1 and client.made[0].deleted


def test_a_rejected_image_is_one_line_and_leaves_nothing_to_delete():
    client = FakeClient(create_error=RuntimeError("HTTP 400 Unable to access the container image.\nThis registry..."))
    code, lines = run(client)
    assert code == 1 and client.made == []
    assert lines[-1] == "Failed: RuntimeError: HTTP 400 Unable to access the container image."


def test_a_failed_fork_still_deletes_the_original():
    client = FakeClient(fork_error=RuntimeError("HTTP 409 A snapshot is already in progress"))
    code, lines = run(client)
    assert code == 1 and [s.deleted for s in client.made] == [True]


def test_a_create_whose_reply_was_lost_is_found_by_name_and_deleted():
    client = FakeClient(create_error=TimeoutError("read timed out"), lost_reply=True)
    code, lines = run(client)
    assert code == 1 and len(client.made) == 1 and client.made[0].deleted


def test_a_fork_whose_reply_was_lost_is_found_by_name_and_deleted():
    client = FakeClient(fork_error=TimeoutError("read timed out"), lost_reply=True)
    code, lines = run(client)
    original, fork = client.made
    assert code == 1 and fork.name == f"{original.name}-fork"
    assert original.deleted and fork.deleted


def test_a_project_without_the_bug_is_reported_and_deleted():
    client = FakeClient()
    project = {**PROJECT, "cart.js": PROJECT["cart.js"].replace(byoi.BUG, byoi.FIX)}
    code, lines = run(client, project=project)
    assert code == 1 and client.made[0].deleted and len(client.made) == 1
    assert "planted bug is missing" in lines[-2]


def test_ctrl_c_deletes_every_sandbox_and_exits_130():
    client = FakeClient()

    def interrupt(*_a, **_k):
        raise KeyboardInterrupt

    original_create = client.sandboxes.create

    def create(params):
        sandbox = original_create(params)
        sandbox.snapshot = interrupt
        return sandbox

    client.sandboxes.create = create
    assert run(client)[0] == 130 and client.made[0].deleted


def test_main_exits_2_when_env_is_missing(monkeypatch, capsys):
    for name in byoi.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert byoi.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_main_exits_2_on_an_image_without_a_tag(monkeypatch, capsys):
    for name in byoi.REQUIRED_ENV:
        monkeypatch.setenv(name, "x")
    assert byoi.main(["--image", "docker.io/library/node"]) == 2
    assert "explicit tag or digest" in capsys.readouterr().err


def test_runs_get_different_sandbox_names():
    client = FakeClient()
    run(client)
    run(client)
    assert client.created[0]["name"] != client.created[1]["name"]
