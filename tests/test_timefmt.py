import pytest

from dayfile.timefmt import normalize


def test_utc_z_passes_through():
    assert normalize("2026-11-06T13:00:00Z") == "2026-11-06T13:00:00Z"


def test_positive_offset_converts_to_utc():
    assert normalize("2026-11-06T15:00:00+02:00") == "2026-11-06T13:00:00Z"


def test_negative_offset_rolls_over_day():
    assert normalize("2026-11-06T22:30:00-05:00") == "2026-11-07T03:30:00Z"


def test_fractional_seconds_are_dropped():
    assert normalize("2026-11-06T13:00:00.987Z") == "2026-11-06T13:00:00Z"


def test_zero_offset_equals_z():
    assert normalize("2026-11-06T13:00:00+00:00") == "2026-11-06T13:00:00Z"


@pytest.mark.parametrize(
    "bad",
    ["2026-11-06T13:00:00", "2026-11-06", "not a time", "", "2026-13-06T13:00:00Z"],
)
def test_naive_or_malformed_is_rejected(bad):
    with pytest.raises(ValueError):
        normalize(bad)


def test_minus_subtracts_seconds():
    from dayfile.timefmt import minus

    assert minus("2026-11-06T00:00:30Z", 60) == "2026-11-05T23:59:30Z"
