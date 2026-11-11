"""Tests for SQL rendering and statement inspection."""

from datetime import date, datetime, time, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from sqlalchemy_openobserve.statements import (
    is_read_only,
    render,
    selects_timestamp,
    stream_ref,
)


def test_render_named_literals_and_escaping() -> None:
    sql = render(
        "SELECT * FROM logs WHERE message=%(message)s AND missing=%(missing)s",
        {"message": "x' OR 1=1 --", "missing": None},
    )
    assert sql == (
        "SELECT * FROM logs WHERE message='x'' OR 1=1 --' AND missing=NULL"
    )


def test_render_positional_and_doubled_percent() -> None:
    assert render("SELECT %s, '%%err%%'", (7,)) == "SELECT 7, '%err%'"


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        (True, "true"),
        (False, "false"),
        (3, "3"),
        (1.5, "1.5"),
        (Decimal("2.50"), "2.50"),
        (b"\x00\xff", "X'00ff'"),
        (date(2024, 1, 2), "'2024-01-02'"),
        (time(3, 4, 5), "'03:04:05'"),
        (
            datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            "'2024-01-02T03:04:05+00:00'",
        ),
        (
            UUID("12345678-1234-5678-1234-567812345678"),
            "'12345678-1234-5678-1234-567812345678'",
        ),
    ],
)
def test_render_supported_types(value: object, literal: str) -> None:
    assert render("SELECT %(value)s", {"value": value}) == f"SELECT {literal}"


@pytest.mark.parametrize("value", [float("inf"), Decimal("NaN")])
def test_render_rejects_non_finite_numbers(value: object) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        render("SELECT %(value)s", {"value": value})


def test_render_rejects_unsupported_type() -> None:
    with pytest.raises(TypeError, match="unsupported"):
        render("SELECT %(value)s", {"value": object()})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM logs",
        "/* harmless */ SELECT * FROM logs -- trailing",
        "WITH recent AS (SELECT * FROM logs) SELECT * FROM recent",
        "SELECT 'DROP TABLE logs' FROM logs",
        'SELECT "delete" FROM logs',
        "SHOW TABLES",
        "EXPLAIN SELECT * FROM logs;",
    ],
)
def test_read_only_queries(sql: str) -> None:
    assert is_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM logs",
        "/* SELECT */ DROP TABLE logs",
        "SELECT * FROM logs; DELETE FROM logs",
        "SELECT * FROM logs /* unterminated",
        "SELECT * FROM logs; /* hide */ UPDATE logs SET x=1",
        "WITH removed AS (DELETE FROM logs RETURNING *) SELECT * FROM removed",
        "",
    ],
)
def test_write_and_injection_attempts_are_rejected(sql: str) -> None:
    assert not is_read_only(sql)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM logs", "logs"),
        ('SELECT * FROM "Case Sensitive"', "Case Sensitive"),
        ("SELECT * FROM schema.logs AS l", "schema.logs"),
        ("WITH x AS (SELECT * FROM hidden) SELECT * FROM visible", "visible"),
        ("SELECT * FROM (SELECT * FROM logs) x", None),
        ("SELECT 1", None),
    ],
)
def test_stream_ref(sql: str, expected: str | None) -> None:
    assert stream_ref(sql) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT _timestamp, message FROM logs",
        'SELECT "_timestamp" FROM logs',
        "SELECT logs._timestamp FROM logs",
        "SELECT * FROM logs",
        "SELECT logs.* FROM logs",
    ],
)
def test_selects_timestamp(sql: str) -> None:
    assert selects_timestamp(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT message FROM logs WHERE _timestamp > 0",
        "SELECT max(_timestamp) FROM logs",
        "SELECT '_timestamp' FROM logs",
        "SELECT message AS _timestamp FROM logs",
    ],
)
def test_does_not_select_raw_timestamp(sql: str) -> None:
    assert not selects_timestamp(sql)
