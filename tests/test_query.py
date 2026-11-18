import pytest

from dayfile.render.query import QueryError, parse_query


def q(*pairs):
    return parse_query(list(pairs))


def test_defaults():
    fq = q()
    assert (fq.tags, fq.tag_any) == ([], [])
    assert (fq.within_seconds, fq.since_seq, fq.limit) == (None, None, 50)


def test_repeated_params_collect():
    fq = q(("tag", "a"), ("tag", "b"), ("tag_any", "c"))
    assert fq.tags == ["a", "b"]
    assert fq.tag_any == ["c"]


@pytest.mark.parametrize(
    ("raw", "seconds"), [("30m", 1800), ("12h", 43200), ("7d", 604800), ("1d", 86400)]
)
def test_within_units(raw, seconds):
    assert q(("within", raw)).within_seconds == seconds


@pytest.mark.parametrize("bad", ["7", "d", "7w", "-1d", "0d", "1.5h", ""])
def test_bad_within(bad):
    with pytest.raises(QueryError):
        q(("within", bad))


def test_since_seq_zero_is_distinct_from_absent():
    assert q(("since_seq", "0")).since_seq == 0
    assert q().since_seq is None


@pytest.mark.parametrize("bad", ["-1", "x", "", "1.5"])
def test_bad_since_seq(bad):
    with pytest.raises(QueryError):
        q(("since_seq", bad))


def test_limit_is_clamped_to_200():
    assert q(("limit", "10")).limit == 10
    assert q(("limit", "9999")).limit == 200


@pytest.mark.parametrize("bad", ["0", "-5", "x", ""])
def test_bad_limit(bad):
    with pytest.raises(QueryError):
        q(("limit", bad))


@pytest.mark.parametrize("name", ["limit", "since_seq", "within"])
def test_scalar_params_cannot_repeat(name):
    value = "5d" if name == "within" else "5"
    with pytest.raises(QueryError):
        q((name, value), (name, value))


@pytest.mark.parametrize("name", ["audience", "foo"])
def test_unknown_params_rejected(name):
    with pytest.raises(QueryError):
        q((name, "x"))


def test_status_is_repeatable_and_defaults_to_empty():
    assert q().statuses == []
    assert q(("status", "open"), ("status", "closed")).statuses == ["open", "closed"]
    assert q(("status", "done")).statuses == ["closed"]  # the old name still works


@pytest.mark.parametrize("bad", ["", "OPEN", "retracted,done", "finished"])
def test_bad_status(bad):
    with pytest.raises(QueryError):
        q(("status", bad))
