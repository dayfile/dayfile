import re

import pytest

from dayfile import cli
from dayfile.auth.tokens import verify_token
from dayfile.db import connect


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_create_prints_the_url_once_and_the_token_works(capsys, tmp_path):
    db = str(tmp_path / "d.db")
    code, out, _ = run(
        capsys, "--url", "http://feeds.example", "token", "create", "--labels", "family,ops",
        "--expires", "30d", "--note", "grandma", "--db", db,
    )  # fmt: skip
    assert code == 0
    m = re.search(r"http://feeds\.example/t/(\S+)/feed\.json", out)
    assert m
    conn = connect(db)
    p = verify_token(conn, m[1], "2026-11-06T12:00:00Z")
    assert p and p.labels == frozenset({"family", "ops"})
    expires = conn.execute("SELECT expires_at FROM tokens").fetchone()[0]
    assert expires.endswith("Z")
    conn.close()
    _, listing, _ = run(capsys, "token", "list", "--db", db)
    assert m[1].split(".", 1)[1] not in listing
    assert "family" in listing and "ops" in listing and "grandma" in listing


def test_revoke(capsys, tmp_path):
    db = str(tmp_path / "d.db")
    _, out, _ = run(capsys, "token", "create", "--labels", "family", "--db", db)
    value = re.search(r"/t/(\S+)/feed\.json", out)[1]
    token_id = value.split(".", 1)[0]
    code, _, _ = run(capsys, "token", "revoke", token_id, "--db", db)
    assert code == 0
    conn = connect(db)
    assert verify_token(conn, value, "2099-01-01T00:00:00Z") is None
    conn.close()
    _, listing, _ = run(capsys, "token", "list", "--db", db)
    assert "revoked" in listing
    assert run(capsys, "token", "revoke", "nosuchtoken", "--db", db)[0] == 1


@pytest.mark.parametrize("labels", ["Family", "", "a b"])
def test_bad_labels_fail_cleanly(capsys, tmp_path, labels):
    code, _, err = run(
        capsys, "token", "create", "--labels", labels, "--db", str(tmp_path / "d.db")
    )
    assert code == 1 and err
