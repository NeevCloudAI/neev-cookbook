from pathlib import Path

import pytest

import browser_qa
from tests.fakes import FakeClient, FakeSandbox, report

JS_SANDBOX_DIR = Path(__file__).parents[2] / "browser-qa-agent-js" / "sandbox"


def run(sandbox, tmp_path, url=None, allow=(), lines=None):
    client = FakeClient(sandbox)
    code = browser_qa.run(url, list(allow), tmp_path / "out", client, log=(lines if lines is not None else []).append)
    return code, client


def test_missing_env_names_every_missing_variable():
    assert browser_qa.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID"]


def test_demo_site_runs_with_no_internet_and_a_public_url_allows_only_its_hosts():
    assert browser_qa.egress_for(None, []) == {"mode": "deny_all"}
    assert browser_qa.egress_for("https://shop.example.com/cart", ["cdn.example.net", "shop.example.com"]) == {
        "mode": "allow_list", "allow": [{"host": "shop.example.com"}, {"host": "cdn.example.net"}]}


def test_stream_logs_whole_lines_across_chunks_and_returns_the_exit_code():
    lines = []
    events = [{"type": "stdout", "data": "phone 2"}, {"type": "stdout", "data": "00\ntablet"},
              {"type": "stderr", "data": " 404"}, {"type": "exit", "exit_code": 3}]
    assert browser_qa.stream(events, lines.append) == 3
    assert lines == ["   phone 200", "   tablet 404"]


def test_stream_without_an_exit_event_is_an_error():
    with pytest.raises(RuntimeError, match="exit code"):
        browser_qa.stream([{"type": "stdout", "data": "phone\n"}], lambda _: None)


def test_summary_has_one_row_per_page_with_each_viewport_and_the_broken_links():
    long_error = "Failed to load resource: the server responded with a status of 404 (File not found)"
    lines = browser_qa.summary(report(
        ("phone", "http://h/", 200, 300, []), ("tablet", "http://h/", 200, 290, []), ("desktop", "http://h/", 200, 280, []),
        ("phone", "http://h/menu.html", 404, 10, [long_error]), ("tablet", "http://h/menu.html", 404, 11, [long_error]),
        ("desktop", "http://h/menu.html", 0, 12, ["Timeout 15000ms exceeded."]),
        broken=[{"url": "http://h/menu.html", "status": 404}, {"url": "http://h/gone", "status": 0}], checked=4, not_checked=2))
    assert lines[1].split() == ["/", "200", "300", "ms", "290", "ms", "280", "ms", "0"]
    assert lines[2].split()[:3] == ["/menu.html", "404/failed", "10"]
    assert "  2  Failed to load resource: the server responded with a stat..." in lines[2]
    assert lines[3:] == ["   Broken links: 2 of 4 checked, 2 not checked",
                         "     http://h/menu.html (404)", "     http://h/gone (unreachable)"]


def test_summary_puts_each_load_time_under_its_viewport_and_keeps_query_pages_apart():
    lines = browser_qa.summary(report(
        ("phone", "http://h/", 0, 15000, ["Timeout 15000ms exceeded."]), ("tablet", "http://h/", 200, 290, []),
        ("tablet", "http://h/item?id=1", 200, 111, []), ("tablet", "http://h/item?id=2", 200, 112, []),
        ("desktop", "http://h/item?id=1", 200, 221, [])))
    assert lines[0] == "   Page            Status        Phone    Tablet   Desktop  Console errors"
    assert lines[1] == "   /               failed/200 15000 ms    290 ms         -  1  Timeout 15000ms exceeded."
    assert lines[2] == "   /item?id=1      200               -    111 ms    221 ms  0"
    assert lines[3] == "   /item?id=2      200               -    112 ms         -  0"


def test_a_screenshot_qa_js_could_not_take_is_not_downloaded(tmp_path):
    sb = FakeSandbox(qa_report=report(("phone", "http://127.0.0.1:8080/", 200, 300, []), screenshots=["tablet.png"]))
    assert run(sb, tmp_path)[0] == 0
    assert sb.downloaded == ["qa-out/tablet.png", "qa-out/report.md", "qa-out/report.json"]


def test_demo_run_serves_the_bundled_site_on_loopback_runs_qa_downloads_and_deletes(tmp_path):
    sb, lines = FakeSandbox(), []
    code, client = run(sb, tmp_path, lines=lines)
    assert code == 0
    params = client.created[0]
    assert params["sandbox_template_id"] == "sb-ubuntu-26-04-playwright"
    assert params["egress"] == {"mode": "deny_all"}
    assert params["name"].startswith("browser-qa-")
    assert sorted(sb.files_written) == ["qa.js", "site/about.html", "site/index.html", "site/style.css"]
    assert sb.files_written["qa.js"] == (browser_qa.SANDBOX_DIR / "qa.js").read_text()
    assert sb.started == [["python3", "-m", "http.server", "8080", "--bind", "127.0.0.1", "--directory", "site"]]
    assert sb.streamed == [["node", "qa.js", "http://127.0.0.1:8080/"]]
    results = ["phone.png", "tablet.png", "desktop.png", "report.md", "report.json"]
    assert sb.downloaded == [f"qa-out/{n}" for n in results]
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(results)
    assert "   phone   200   300 ms" in lines
    assert any("/about.html" in l and "loadTeamPhotos is not defined" in l for l in lines)
    assert sb.deleted


def test_public_url_run_allows_its_hosts_uploads_no_site_and_checks_links_on_them(tmp_path):
    sb = FakeSandbox(qa_report=report(("phone", "https://example.com/", 200, 900, [])))
    code, client = run(sb, tmp_path, url="https://example.com", allow=["cdn.example.net"])
    assert code == 0
    assert client.created[0]["egress"] == {"mode": "allow_list", "allow": [{"host": "example.com"}, {"host": "cdn.example.net"}]}
    assert list(sb.files_written) == ["qa.js"]
    assert sb.started == []
    assert sb.streamed == [["node", "qa.js", "https://example.com", "cdn.example.net"]]


@pytest.mark.parametrize("sandbox, message", [
    (FakeSandbox(site_up=False), "Failed: the demo site did not start"),
    (FakeSandbox(qa_events=[{"type": "stderr", "data": "boom\n"}, {"type": "exit", "exit_code": 1}]), "Failed: the QA script failed"),
    (FakeSandbox(qa_report=report(("phone", "http://127.0.0.1:8080/", 0, 15000, ["net::ERR"]))),
     "Failed: http://127.0.0.1:8080/ did not load on any viewport"),
])
def test_a_failed_run_is_one_line_exits_1_downloads_nothing_and_deletes(tmp_path, sandbox, message):
    lines = []
    code, _ = run(sandbox, tmp_path, lines=lines)
    assert code == 1 and message in lines
    assert sandbox.downloaded == [] and sandbox.deleted


def test_ctrl_c_mid_run_deletes_the_sandbox_and_exits_130(tmp_path):
    sb = FakeSandbox(qa_events=[{"type": "stdout", "data": "phone\n"}, KeyboardInterrupt()])
    code, _ = run(sb, tmp_path)
    assert code == 130 and sb.deleted


def test_runs_get_different_sandbox_names(tmp_path):
    client = FakeClient(FakeSandbox())
    for _ in range(2):
        browser_qa.run(None, [], tmp_path, client, log=lambda _: None)
    assert client.created[0]["name"] != client.created[1]["name"]


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in browser_qa.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert browser_qa.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--url", "example.com"], ["--url", "ftp://example.com"], ["--allow", "cdn.example.net"]])
def test_main_rejects_a_bad_url_or_allow_without_url(argv):
    with pytest.raises(SystemExit) as exit_info:
        browser_qa.main(argv)
    assert exit_info.value.code == 2


@pytest.mark.skipif(not JS_SANDBOX_DIR.exists(), reason="the TypeScript recipe is not next to this one")
def test_the_typescript_recipe_uploads_the_same_qa_script_and_site():
    for path in ["qa.js", "site/index.html", "site/about.html", "site/style.css"]:
        assert (browser_qa.SANDBOX_DIR / path).read_text() == (JS_SANDBOX_DIR / path).read_text(), path
