"""Unit tests for schema-layer helpers and Pydantic models in app.schemas."""

import pytest

from app.schemas import CutBoundsRequest, _parse_hhmmss


@pytest.mark.parametrize(
    "value,expected",
    [
        ("00:00:00", 0.0),
        ("00:00:01", 1.0),
        ("00:00:01.5", 1.5),
        ("00:01:00", 60.0),
        ("01:00:00", 3600.0),
        ("00:30:00", 1800.0),
        ("23:59:59", 86399.0),
        ("23:59:59.999", 86399.999),
    ],
)
def test_parse_hhmmss_valid(value: str, expected: float):
    assert float(_parse_hhmmss(value)) == pytest.approx(expected, abs=1e-6)


def test_parse_hhmmss_sub_second_less_than_next_whole_second():
    assert _parse_hhmmss("00:00:59.9999999999999999") < _parse_hhmmss("00:01:00")


@pytest.mark.parametrize(
    "value",
    [
        "24:00:00",  # HH == 24 — the reported bug
        "25:00:00",  # HH > 24
        "99:00:00",  # HH far out of range
        "00:60:00",  # MM == 60
        "00:61:00",  # MM > 60
        "00:00:60",  # SS == 60
        "00:00:61",  # SS > 60
        "99:99:99",  # all components out of range
    ],
)
def test_parse_hhmmss_out_of_range(value: str):
    with pytest.raises(ValueError, match="out of range"):
        _parse_hhmmss(value)


@pytest.mark.parametrize(
    "value",
    [
        "1:00:00",  # single-digit hour
        "ab:00:00",  # non-numeric hour
        "00:00",  # missing seconds segment
        "00:00:00:00",  # extra segment
        "",  # empty string
        "00:00:00\n",  # trailing newline
        "00:00:00\r\n",  # trailing CRLF
        " 00:00:00",  # leading whitespace
        "00:00:00 ",  # trailing whitespace
    ],
)
def test_parse_hhmmss_bad_format(value: str):
    with pytest.raises(ValueError, match="Invalid time format"):
        _parse_hhmmss(value)


def test_parse_hhmmss_non_string():
    with pytest.raises(ValueError, match="Invalid time format"):
        _parse_hhmmss(3600)  # type: ignore[arg-type]


def test_cut_bounds_valid():
    req = CutBoundsRequest(cut_start="00:10:00", cut_end="00:30:00")
    start_s, end_s = req.parsed_seconds()
    assert start_s == pytest.approx(600.0)
    assert end_s == pytest.approx(1800.0)


def test_cut_bounds_end_must_exceed_start():
    with pytest.raises(ValueError, match="cut_end must be greater than cut_start"):
        CutBoundsRequest(cut_start="00:30:00", cut_end="00:10:00")


def test_cut_bounds_equal_start_end_rejected():
    with pytest.raises(ValueError, match="cut_end must be greater than cut_start"):
        CutBoundsRequest(cut_start="00:10:00", cut_end="00:10:00")


@pytest.mark.parametrize(
    "cut_start,cut_end",
    [
        ("24:00:00", "25:00:00"),  # both out of range
        ("24:00:00", "00:30:00"),  # start out of range
        ("00:10:00", "24:00:00"),  # end out of range
        ("00:00:60", "00:01:00"),  # seconds == 60
    ],
)
def test_cut_bounds_out_of_range_rejected(cut_start: str, cut_end: str):
    """CutBoundsRequest must propagate _parse_hhmmss range errors as 422."""
    with pytest.raises(ValueError, match="out of range"):
        CutBoundsRequest(cut_start=cut_start, cut_end=cut_end)


def test_cut_bounds_newline_rejected():
    with pytest.raises(ValueError, match="Invalid time format"):
        CutBoundsRequest(cut_start="00:00:00\n", cut_end="00:01:00")


def test_cut_bounds_high_precision_seconds_accepted():
    req = CutBoundsRequest(cut_start="00:00:00", cut_end="00:00:59.999999")
    start, end = req.parsed_seconds()
    assert float(start) == 0.0
    assert float(end) == pytest.approx(60.0, abs=1e-5)


def test_cut_bounds_high_precision_ordering_preserved():
    req = CutBoundsRequest(cut_start="00:00:59.999999", cut_end="00:01:00")
    start, end = req.parsed_seconds()
    assert start < end


def test_cut_bounds_high_precision_ordering_lost_rejected():
    # Values that are ordered as Decimal but equal as float
    with pytest.raises(ValueError, match="lose ordering after float conversion"):
        CutBoundsRequest(cut_start="00:00:59.9999999999999999", cut_end="00:01:00")
