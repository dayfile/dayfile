import asyncio

import pytest
from fastapi.testclient import TestClient

from dayfile import mcp_server as m
from dayfile.app import create_app
from dayfile.auth.tokens import create_token
from dayfile.db import connect


@pytest.fixture
def request_fn(tmp_path):
    db = tmp_path / "d.db"
    client = TestClient(create_app(db))
    conn = connect(db)
    write = create_token(conn, ["self"], write_sources=["adapter:mcp"]).value
    read = create_token(conn, ["self"]).value
    conn.close()

    async def call(method, path, body=None):
        headers = {"Authorization": f"Bearer {read if method == 'GET' else write}"}
        r = client.request(method, path, json=body, headers=headers)
        return r.status_code, r.json()

    return call


def run(coro):
    return asyncio.run(coro)


def test_add_then_list_and_get(request_fn):
    stored = run(m.add_event(request_fn, "Buy milk", None, ["home"], True, 0, "2026-12-01"))
    events = run(m.list_events(request_fn, ["todo"], "open", None, 20))
    assert [e["title"] for e in events] == ["Buy milk"]
    assert "todo" in events[0]["tags"] and events[0]["payload"] == {"due": "2026-12-01"}
    assert run(m.get_event(request_fn, stored["id"]))["title"] == "Buy milk"


def test_close_todo_appends_a_closing_event_and_keeps_the_original(request_fn):
    stored = run(m.add_event(request_fn, "Renew domain", None, [], True, 0, None))
    run(m.close_todo(request_fn, stored["id"]))
    assert run(m.list_events(request_fn, [], "open", None, 20)) == []
    closed = run(m.list_events(request_fn, [], "closed", None, 20))
    assert [e["title"] for e in closed] == ["Renew domain"]


def test_errors_come_back_as_dayfile_errors(request_fn):
    with pytest.raises(m.DayfileError, match="404"):
        run(m.get_event(request_fn, "01ARZ3NDEKTSV4RRFFQ69G5FAV"))
    with pytest.raises(m.DayfileError, match="400"):
        run(m.list_events(request_fn, [], "bogus", None, 20))


def test_server_exposes_five_tools_and_read_tools_are_marked_read_only(request_fn):
    server = m.build_server(request_fn)
    tools = {t.name: t for t in run(server.list_tools())}
    assert set(tools) == {
        "dayfile_list_events",
        "dayfile_get_event",
        "dayfile_add_event",
        "dayfile_close_todo",
        "dayfile_retract_event",
    }
    assert tools["dayfile_list_events"].annotations.read_only_hint is True
    write = tools["dayfile_add_event"].annotations
    assert not (write and write.read_only_hint)


def test_calling_a_tool_through_the_server(request_fn):
    server = m.build_server(request_fn)
    run(server.call_tool("dayfile_add_event", {"title": "hello", "tags": ["x"]}))
    result = run(server.call_tool("dayfile_list_events", {"tags": ["x"]}))
    assert "hello" in str(result)


def test_retract_hides_an_event_and_retracting_a_close_reopens_the_todo(request_fn):
    note = run(m.add_event(request_fn, "Typo note", None, [], False, 0, None))
    run(m.retract_event(request_fn, note["id"]))
    assert run(m.list_events(request_fn, [], "retracted", None, 20))[0]["id"] == note["id"]

    todo = run(m.add_event(request_fn, "Pay rent", None, [], True, 0, None))
    closing = run(m.close_todo(request_fn, todo["id"]))
    run(m.retract_event(request_fn, closing["id"]))
    reopened = run(m.list_events(request_fn, [], "open", None, 20))
    assert [e["title"] for e in reopened] == ["Pay rent"]


def test_server_reports_the_installed_version(request_fn):
    from importlib.metadata import version

    assert m._version() == version("dayfile") != ""
    server = m.build_server(request_fn)
    assert server._lowlevel_server.version == version("dayfile")


def test_close_todo_can_say_why(request_fn):
    stored = run(m.add_event(request_fn, "Renew domain", None, [], True, 0, None))
    closing = run(m.close_todo(request_fn, stored["id"], "Renewed through 2027."))
    assert closing["body"] == "Renewed through 2027."
    assert run(m.get_event(request_fn, stored["id"]))["status"] == "closed"


def test_retract_can_say_why(request_fn):
    stored = run(m.add_event(request_fn, "Typo", None, [], False, 0, None))
    retraction = run(m.retract_event(request_fn, stored["id"], "posted by mistake"))
    assert retraction["body"] == "posted by mistake"
