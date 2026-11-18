"""The DBAPI cursor against a live OpenObserve"""

import time
from typing import Any

import pytest

from sqlalchemy_openobserve import dbapi

from .conftest import EMAIL, HOST, LEVELS, PASSWORD, PORT, ROWS


def run(
    cursor: dbapi.Cursor, sql: str, parameters: Any = None
) -> tuple[list[tuple[str, Any]], list[tuple[Any, ...]]]:
    cursor.execute(sql, parameters)
    assert cursor.description is not None
    described = [(column[0], column[1]) for column in cursor.description]
    return described, cursor.fetchall()


def test_selected_columns_are_typed_and_the_timestamp_is_not_added(
    cursor: dbapi.Cursor, stream: str
) -> None:
    described, rows = run(
        cursor, f"select level, status from {stream} order by status limit 2"
    )

    assert described == [("level", "Utf8"), ("status", "Int64")]
    assert len(rows) == 2
    assert all(len(row) == 2 for row in rows)


def test_star_includes_the_timestamp_first(
    cursor: dbapi.Cursor, stream: str
) -> None:
    described, rows = run(cursor, f"select * from {stream} limit 1")

    assert described == [
        ("_timestamp", "Int64"),
        ("duration_ms", "Float64"),
        ("level", "Utf8"),
        ("message", "Utf8"),
        ("service", "Utf8"),
        ("status", "Int64"),
        ("user_id", "Utf8"),
    ]
    assert len(rows[0]) == 7


def test_a_selected_timestamp_is_kept(
    cursor: dbapi.Cursor, stream: str
) -> None:
    described, rows = run(
        cursor, f"select _timestamp, level from {stream} limit 1"
    )

    assert described == [("_timestamp", "Int64"), ("level", "Utf8")]
    assert isinstance(rows[0][0], int)
    assert rows[0][1] in LEVELS


def test_an_aggregate_aliased_timestamp_is_not_dropped(
    cursor: dbapi.Cursor, stream: str
) -> None:
    described, rows = run(
        cursor, f"select count(*) as _timestamp from {stream}"
    )

    assert described == [("_timestamp", "Int64")]
    assert rows == [(ROWS,)]


def test_group_by_counts_every_row(cursor: dbapi.Cursor, stream: str) -> None:
    described, rows = run(
        cursor,
        f"select level, count(*) as n from {stream} "
        "group by level order by level",
    )

    assert described == [("level", "Utf8"), ("n", "Int64")]
    assert rows == [("error", 375), ("info", 750), ("warn", 375)]


def test_a_limit_above_1000_is_honoured(
    cursor: dbapi.Cursor, stream: str
) -> None:
    _, limited = run(cursor, f"select message from {stream} limit 1200")
    _, unlimited = run(cursor, f"select message from {stream}")

    assert len(limited) == 1200
    assert len(unlimited) == ROWS


def test_a_result_with_no_rows_still_describes_its_columns(
    cursor: dbapi.Cursor, stream: str
) -> None:
    described, rows = run(
        cursor, f"select level, status from {stream} where false limit 0"
    )

    assert described == [("level", "Utf8"), ("status", "Int64")]
    assert rows == []


def test_star_with_no_rows_describes_every_field(
    cursor: dbapi.Cursor, stream: str
) -> None:
    described, rows = run(cursor, f"select * from {stream} where status = -1")

    assert [name for name, _ in described] == [
        "_timestamp",
        "duration_ms",
        "level",
        "message",
        "service",
        "status",
        "user_id",
    ]
    assert rows == []


def test_an_expression_is_typed_from_its_values(
    cursor: dbapi.Cursor, stream: str
) -> None:
    described, _ = run(
        cursor,
        f"select cast(status as varchar) as status, max(duration_ms) as d "
        f"from {stream} group by cast(status as varchar)",
    )

    assert described == [("status", "Utf8"), ("d", "Float64")]


def test_a_missing_field_comes_back_as_none(
    cursor: dbapi.Cursor, stream: str
) -> None:
    _, rows = run(
        cursor, f"select user_id from {stream} where user_id is null limit 1"
    )
    _, counts = run(cursor, f"select count(user_id), count(*) from {stream}")

    assert rows == [(None,)]
    assert counts == [(ROWS // 5, ROWS)]


def test_quotes_and_percent_signs_in_parameters_are_escaped(
    cursor: dbapi.Cursor, stream: str
) -> None:
    odd = 'it\'s 100% "odd"'

    cursor.execute(
        f"select count(*) from {stream} where service = %(service)s",
        {"service": odd},
    )

    assert cursor.fetchone() == (ROWS // 3,)


def test_a_percent_sign_without_parameters_is_left_alone(
    cursor: dbapi.Cursor, stream: str
) -> None:
    cursor.execute(
        f"select count(*) from {stream} where message like 'request 1%'"
    )

    assert cursor.fetchone() == (611,)


def test_an_unknown_stream_is_a_programming_error(
    cursor: dbapi.Cursor,
) -> None:
    with pytest.raises(dbapi.ProgrammingError):
        cursor.execute("select * from no_such_stream_here")


def test_an_unknown_column_is_a_programming_error(
    cursor: dbapi.Cursor, stream: str
) -> None:
    with pytest.raises(dbapi.ProgrammingError):
        cursor.execute(f"select no_such_column from {stream}")


def test_openobserve_functions_are_accepted(
    cursor: dbapi.Cursor, stream: str
) -> None:
    _, matched = run(
        cursor, f"select count(*) from {stream} where str_match(level, 'err')"
    )
    _, buckets = run(
        cursor,
        f"select histogram(_timestamp, '1 hour') as h, count(*) from {stream}"
        " group by h order by h",
    )

    assert matched == [(375,)]
    assert sum(count for _, count in buckets) == ROWS


def test_writes_are_refused_before_reaching_the_server(
    cursor: dbapi.Cursor, stream: str
) -> None:
    with pytest.raises(dbapi.NotSupportedError):
        cursor.execute(f"delete from {stream}")


def test_a_wrong_password_is_an_operational_error(stream: str) -> None:
    connection = dbapi.connect(
        host=HOST,
        port=PORT,
        organization="default",
        email=EMAIL,
        password=PASSWORD + "x",
        ssl=False,
    )
    cursor = connection.cursor()

    try:
        with pytest.raises(dbapi.OperationalError):
            cursor.execute(f"select level from {stream}")
    finally:
        connection.close()


INJECTIONS = [
    "\\' OR 1=1 --",
    "\\\\' OR 1=1 --",
    "\\' OR 1=1 OR '",
    "\\' OR level = 'info' --",
    "x\\",
]


@pytest.mark.parametrize("value", INJECTIONS)
def test_a_backslash_value_matches_nothing_and_round_trips(
    cursor: dbapi.Cursor, stream: str, value: str
) -> None:
    cursor.execute(
        f"select count(*) from {stream} where service = %(v)s", {"v": value}
    )
    assert cursor.fetchone() == (0,)

    _, rows = run(
        cursor, f"select %(v)s as v from {stream} limit 1", {"v": value}
    )
    assert rows == [(value,)]


def test_backslash_values_work_in_other_positions(
    cursor: dbapi.Cursor, stream: str
) -> None:
    cursor.execute(
        f"select count(*) from {stream} where service in (%s, %s)",
        ("x\\", INJECTIONS[0]),
    )
    assert cursor.fetchone() == (0,)

    cursor.execute(
        f"select count(*) from {stream} where service = %s and level = %s",
        ("x\\", "info"),
    )
    assert cursor.fetchone() == (0,)


def test_a_long_run_of_backslashes_stays_fast_and_exact(
    cursor: dbapi.Cursor, stream: str
) -> None:
    value = "\\" * 2000
    started = time.monotonic()

    _, rows = run(
        cursor, f"select %(v)s as v from {stream} limit 1", {"v": value}
    )

    assert rows == [(value,)]
    assert time.monotonic() - started < 5
