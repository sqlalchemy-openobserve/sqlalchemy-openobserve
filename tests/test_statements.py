"""Tests for SQL rendering and statement inspection."""

from datetime import date, datetime, time, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from sqlalchemy_openobserve.statements import (
    is_read_only,
    output_columns,
    plain_columns,
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


@pytest.mark.parametrize(
    "value",
    [
        float("inf"),
        float("-inf"),
        float("nan"),
        Decimal("NaN"),
        Decimal("-Infinity"),
    ],
)
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
        "SELECT update, replace(message, 'a', 'b') FROM logs",
        "SHOW TABLES",
        "EXPLAIN SELECT * FROM logs;",
        "SELECT * FROM a UNION SELECT * FROM b",
        "SELECT * FROM a UNION ALL SELECT * FROM b EXCEPT SELECT * FROM c",
        "WITH a AS (WITH b AS (SELECT * FROM logs) SELECT * FROM b) "
        "SELECT * FROM a",
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
        "WITH recent AS (SELECT * FROM logs) "
        "INSERT INTO archive SELECT * FROM recent",
        "SELECT * FROM (DELETE FROM logs RETURNING *) AS removed",
        "WITH d AS (DELETE FROM b RETURNING *) "
        "SELECT * FROM a UNION SELECT * FROM d",
        "WITH a AS (WITH b AS (DELETE FROM logs RETURNING *) "
        "SELECT * FROM b) SELECT * FROM a",
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
        ("WITH x AS (SELECT * FROM logs) SELECT * FROM x", "logs"),
        (
            "WITH x AS (SELECT * FROM logs), y AS (SELECT * FROM x), "
            "z AS (SELECT * FROM y) SELECT * FROM z",
            "logs",
        ),
        ("SELECT * FROM logs JOIN users ON true", None),
        (
            "WITH x AS (SELECT * FROM logs JOIN users ON true) "
            "SELECT * FROM x",
            None,
        ),
        ("WITH x AS (SELECT * FROM missing) SELECT * FROM unknown", "unknown"),
        ("WITH RECURSIVE x AS (SELECT * FROM x) SELECT * FROM x", None),
        ("SELECT * FROM (SELECT * FROM logs) x", None),
        ('WITH "Logs" AS (SELECT * FROM secret) SELECT * FROM logs', "logs"),
        (
            'WITH "Logs" AS (SELECT * FROM secret) SELECT * FROM "Logs"',
            "secret",
        ),
        ("WITH Logs AS (SELECT * FROM secret) SELECT * FROM logs", "secret"),
        ("SELECT * FROM logs WHERE x IN (SELECT y FROM secret)", "logs"),
        ("SELECT * FROM logs UNION SELECT * FROM logs", None),
        ("(SELECT * FROM logs)", "logs"),
        ("WITH x AS (SELECT 1) SELECT * FROM x JOIN logs ON true", "logs"),
        ("SELECT 1", None),
        (
            "WITH a AS (WITH c AS (SELECT * FROM logs) SELECT * FROM c) "
            "SELECT * FROM a",
            "logs",
        ),
        (
            "WITH a AS (WITH c AS (SELECT * FROM logs) SELECT * FROM c), "
            "b AS (SELECT * FROM a) SELECT * FROM b",
            "logs",
        ),
        ("SELECT * FROM a EXCEPT SELECT * FROM b", None),
        (
            "WITH a AS (WITH c AS (SELECT * FROM secret) SELECT * FROM c) "
            "SELECT * FROM c",
            "c",
        ),
        ("SELECT * FROM a INTERSECT SELECT * FROM a", None),
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
        "SELECT DISTINCT _timestamp FROM logs",
        "SELECT DISTINCT * FROM logs",
        "SELECT ALL _timestamp FROM logs",
        "SELECT _timestamp AS _timestamp FROM logs",
        "SELECT _timestamp FROM a UNION ALL SELECT _timestamp FROM b",
        "SELECT * FROM a UNION SELECT * FROM b UNION SELECT * FROM c",
        "(SELECT _timestamp FROM logs)",
        "(SELECT _timestamp FROM a) UNION (SELECT _timestamp FROM b)",
        "SELECT _timestamp FROM a EXCEPT SELECT message FROM b",
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
        "SELECT _timestamp AS ts FROM logs",
        "SELECT DISTINCT max(_timestamp) FROM logs",
        "(SELECT message FROM logs)",
        "SELECT message FROM a UNION SELECT _timestamp FROM b",
        "SELECT message FROM a INTERSECT SELECT _timestamp FROM b",
    ],
)
def test_does_not_select_raw_timestamp(sql: str) -> None:
    assert not selects_timestamp(sql)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT level, status FROM logs", ["level", "status"]),
        ("SELECT logs.level AS l, status s FROM logs", ["l", "s"]),
        ("SELECT * FROM logs", ["a", "b"]),
        ("SELECT logs.*, 1 AS one FROM logs", ["a", "b", "one"]),
        ("SELECT count(*) FROM logs", ["count(*)"]),
        ("SELECT COUNT(*), Max(a) FROM logs", ["count(*)", "max(a)"]),
        ('SELECT a AS Lv, a AS "Lv" FROM logs', ["lv", "Lv"]),
        ("WITH c AS (SELECT a FROM x) SELECT * FROM c", None),
        ("SELECT * FROM (SELECT a FROM x) t", None),
        ("SELECT * FROM x JOIN y ON x.a = y.a", None),
        ("SELECT count(*) AS n FROM logs", ["n"]),
        ("SELECT a FROM x UNION ALL SELECT b FROM y", ["a"]),
        ("(SELECT a FROM x) UNION (SELECT b FROM y)", ["a"]),
        ("WITH c AS (SELECT a FROM x) SELECT a FROM c", ["a"]),
        ("SHOW STREAMS", None),
        ("DELETE FROM logs", None),
        ("not sql at all (", None),
    ],
)
def test_output_columns(sql: str, expected: list[str] | None) -> None:
    assert output_columns(sql, ["a", "b"]) == expected


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT level, logs.status FROM logs", {"level", "status"}),
        ("SELECT level AS level, status AS code FROM logs", {"level"}),
        ("SELECT status AS level, level AS status FROM logs", set()),
        ("SELECT cast(status AS VARCHAR) AS status FROM logs", set()),
        ("SELECT count(*), max(a) AS a FROM logs", set()),
        ("SELECT * FROM logs", None),
        ("SELECT a, * FROM logs", None),
        ("SELECT a FROM x UNION ALL SELECT b FROM y", {"a"}),
        ("SHOW STREAMS", set()),
        ("WITH c AS (SELECT a FROM x) SELECT a FROM c", set()),
        ("WITH c AS (SELECT a FROM x) SELECT * FROM c", set()),
    ],
)
def test_plain_columns(sql: str, expected: set[str] | None) -> None:
    assert plain_columns(sql) == expected
