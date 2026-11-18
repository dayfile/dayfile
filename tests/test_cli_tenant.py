import json

import pytest

from dayfile import cli
from dayfile.tenancy import DirectoryTenancy


@pytest.fixture
def root(tmp_path):
    return tmp_path / "tenants"


def run(root, *argv):
    return cli.main(["tenant", *argv, "--root", str(root)])


def test_create_prints_the_database_path_and_makes_a_tenant(root, capsys):
    assert run(root, "create", "acme") == 0
    path = capsys.readouterr().out.strip()
    assert path == str(root / "acme" / "dayfile.db")
    assert DirectoryTenancy(root, "x.test").list() == ["acme"]


def test_create_with_limits(root, capsys):
    assert run(root, "create", "acme", "--max-events", "50", "--write-limit", "10/60") == 0
    capsys.readouterr()
    run(root, "show", "acme")
    shown = json.loads(capsys.readouterr().out)
    assert shown["max_events"] == 50 and shown["write_limit"] == [10, 60]
    assert shown["suspended"] is False and shown["events"] == 0


def test_bad_names_and_duplicates_fail_with_a_message(root, capsys):
    assert run(root, "create", "Bad Name") == 1
    assert "invalid tenant name" in capsys.readouterr().err
    run(root, "create", "acme")
    capsys.readouterr()
    assert run(root, "create", "acme") == 1
    assert "exists" in capsys.readouterr().err


def test_list_one_name_per_line(root, capsys):
    for name in ("globex", "acme"):
        run(root, "create", name)
    capsys.readouterr()
    run(root, "list")
    assert capsys.readouterr().out.split() == ["acme", "globex"]


def test_suspend_and_resume(root, capsys):
    run(root, "create", "acme")
    assert run(root, "suspend", "acme") == 0
    assert DirectoryTenancy(root, "x.test").resolve("acme.x.test").suspended is True
    assert run(root, "resume", "acme") == 0
    assert DirectoryTenancy(root, "x.test").resolve("acme.x.test").suspended is False


def test_limit_can_be_changed_and_cleared(root, capsys):
    run(root, "create", "acme")
    assert run(root, "limit", "acme", "--max-events", "7", "--write-limit", "5/30") == 0
    t = DirectoryTenancy(root, "x.test").resolve("acme.x.test")
    assert t.max_events == 7 and t.write_limit == (5, 30)
    assert run(root, "limit", "acme", "--max-events", "none", "--write-limit", "none") == 0
    t = DirectoryTenancy(root, "x.test").resolve("acme.x.test")
    assert t.max_events is None and t.write_limit is None


@pytest.mark.parametrize(
    "bad", [["--write-limit", "ten"], ["--write-limit", "0/60"], ["--max-events", "-3"]]
)
def test_bad_limits_are_refused(root, capsys, bad):
    run(root, "create", "acme")
    capsys.readouterr()
    assert run(root, "limit", "acme", *bad) == 1
    assert capsys.readouterr().err.startswith("dayfile:")


def test_unknown_tenant_is_an_error(root, capsys):
    root.mkdir()
    for action in ("suspend", "resume", "show", "path"):
        assert run(root, action, "nobody") == 1
    assert "no such tenant" in capsys.readouterr().err


def test_delete_needs_confirmation_and_moves_to_trash(root, capsys):
    run(root, "create", "acme")
    capsys.readouterr()
    assert run(root, "delete", "acme") == 1
    assert "--yes" in capsys.readouterr().err
    assert DirectoryTenancy(root, "x.test").list() == ["acme"]
    assert run(root, "delete", "acme", "--yes") == 0
    assert ".trash" in capsys.readouterr().out
    assert DirectoryTenancy(root, "x.test").list() == []


def test_path_lets_existing_token_commands_work_on_a_tenant(root, capsys):
    run(root, "create", "acme")
    capsys.readouterr()
    run(root, "path", "acme")
    db_path = capsys.readouterr().out.strip()
    assert cli.main(["token", "create", "--labels", "self", "--db", db_path]) == 0
    assert "/feed.json" in capsys.readouterr().out


def test_root_defaults_to_the_environment(root, monkeypatch, capsys):
    monkeypatch.setenv("DAYFILE_TENANTS_DIR", str(root))
    assert cli.main(["tenant", "create", "acme"]) == 0
    assert (root / "acme" / "dayfile.db").is_file()


def test_no_root_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.delenv("DAYFILE_TENANTS_DIR", raising=False)
    assert cli.main(["tenant", "list"]) == 1
    assert "DAYFILE_TENANTS_DIR" in capsys.readouterr().err


def test_usage_lists_every_tenant_for_a_billing_job(root, capsys):
    run(root, "create", "acme", "--max-events", "9")
    run(root, "create", "globex")
    capsys.readouterr()
    assert run(root, "usage") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["acme", "globex"]
    assert rows[0]["max_events"] == 9 and rows[0]["events"] == 0 and rows[0]["bytes"] > 0


def test_verify_backup_accepts_a_good_backup_and_names_what_is_wrong(tmp_path, capsys):
    import gzip

    from dayfile.cli import main
    from dayfile.tenancy import DirectoryTenancy

    tenancy = DirectoryTenancy(tmp_path / "t", "example.test")
    tenancy.create("acme")
    out = tmp_path / "out"
    assert main(["tenant", "backup-all", str(out), "--root", str(tmp_path / "t")]) == 0
    assert main(["tenant", "verify-backup", str(out)]) == 0
    assert "acme: 0 events" in capsys.readouterr().out
    (out / "acme.tenant.json").unlink()
    assert main(["tenant", "verify-backup", str(out)]) == 1
    assert "tenant.json missing" in capsys.readouterr().out
    (out / "acme.db.gz").write_bytes(gzip.compress(b"this is not a database"))
    assert main(["tenant", "verify-backup", str(out)]) == 1
    (out / "acme.db.gz").write_bytes(b"garbage")
    assert main(["tenant", "verify-backup", str(out)]) == 1
    assert main(["tenant", "verify-backup", str(tmp_path / "empty")]) == 1


def test_heartbeat_check_writes_the_lapse_into_the_tenants_log(root, capsys):
    from dayfile import db, heartbeats
    from dayfile.cli import main
    from dayfile.tenancy import DirectoryTenancy

    tenancy = DirectoryTenancy(root, "example.test")
    tenancy.create("acme")
    conn = db.connect(tenancy.get("acme").db_path)
    heartbeats.save_heartbeat(conn, "ci", "2020-01-01T00:00:00Z", "1h", tag="ci")
    conn.close()
    assert main(["heartbeat", "check", "--root", str(root)]) == 0
    assert "acme: ci: nothing from tag ci" in capsys.readouterr().out
    assert main(["heartbeat", "check", "--root", str(root)]) == 0
    assert capsys.readouterr().out == ""  # the same lapse is not written twice
