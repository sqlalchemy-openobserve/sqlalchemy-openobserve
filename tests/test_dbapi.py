"""Tests for the DBAPI connection and cursor"""

import datetime
import time
from typing import Any
from unittest.mock import Mock

import pytest

from sqlalchemy_openobserve import dbapi
from sqlalchemy_openobserve.client import (
    OpenObserveClient,
    OpenObserveError,
    QueryError,
)

SQL = "select level, count(*) from app_logs group by level"


@pytest.fixture
def client() -> Mock:
    client = Mock(spec=OpenObserveClient)
    client.search.return_value = {
        "columns": ["level", "count"],
        "rows": [["info", 3], ["warn", 2], ["error", 1]],
        "is_partial": False,
        "function_error": [],
    }
    client.stream_schema.return_value = [
        {"name": "_timestamp", "type": "Int64"},
        {"name": "level", "type": "Utf8"},
        {"name": "status", "type": "Int64"},
    ]
    return client


@pytest.fixture
def connection(client: Mock) -> dbapi.Connection:
    return dbapi.Connection(client)


@pytest.fixture
def cursor(connection: dbapi.Connection) -> dbapi.Cursor:
    return connection.cursor()


@pytest.fixture(autouse=True)
def time_range(monkeypatch: pytest.MonkeyPatch) -> Mock:
    extract = Mock(return_value=(10, 20))
    monkeypatch.setattr(dbapi, "extract_time_range", extract)
    return extract


@pytest.fixture(autouse=True)
def read_only(monkeypatch: pytest.MonkeyPatch) -> Mock:
    check = Mock(return_value=True)
    monkeypatch.setattr(dbapi.statements, "is_read_only", check)
    return check


def test_connect_builds_client_from_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_class = Mock()
    monkeypatch.setattr(dbapi, "OpenObserveClient", client_class)

    connection = dbapi.connect(
        host="observe.example",
        port=5080,
        organization="default",
        email="root@example.com",
        password="secret",
        ssl=False,
        verify="/etc/ssl/ca.pem",
    )

    client_class.assert_called_once_with(
        "observe.example",
        5080,
        "default",
        "root@example.com",
        "secret",
        ssl=False,
        verify="/etc/ssl/ca.pem",
    )
    connection.close()
    client_class.return_value.close.assert_called_once_with()


def test_execute_searches_with_time_range_and_no_size_cap(
    cursor: dbapi.Cursor, client: Mock, time_range: Mock
) -> None:
    cursor.execute(SQL)

    time_range.assert_called_once_with(SQL)
    client.search.assert_called_once_with(SQL, 10, 20, size=-999)


def result(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {
        "columns": columns,
        "rows": rows,
        "is_partial": False,
        "function_error": [],
    }


def names_and_types(cursor: dbapi.Cursor) -> list[tuple[Any, Any]]:
    assert cursor.description is not None
    return [(column[0], column[1]) for column in cursor.description]


def test_execute_sets_description_and_rowcount(cursor: dbapi.Cursor) -> None:
    assert cursor.description is None
    assert cursor.rowcount == -1

    cursor.execute(SQL)

    assert cursor.description == [
        ("level", "Utf8", None, None, None, None, None),
        ("count", "Int64", None, None, None, None, None),
    ]
    assert cursor.rowcount == 3


def test_type_code_is_the_stream_fields_type(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(
        ["_timestamp", "level", "status"], [[1, "info", 200]]
    )

    cursor.execute("select * from app_logs")

    client.stream_schema.assert_called_once_with("app_logs")
    assert names_and_types(cursor) == [
        ("_timestamp", "Int64"),
        ("level", "Utf8"),
        ("status", "Int64"),
    ]


def test_type_code_of_an_expression_comes_from_its_first_value(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(
        ["a", "b", "c", "d", "e"],
        [[None, None, None, None, None], [1, 1.5, "x", True, [1]]],
    )

    cursor.execute("select a, b, c, d, e from app_logs")

    assert names_and_types(cursor) == [
        ("a", "Int64"),
        ("b", "Float64"),
        ("c", "Utf8"),
        ("d", "Boolean"),
        ("e", None),
    ]


def test_type_code_is_none_when_every_value_is_null(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(["a"], [[None], [None]])

    cursor.execute("select a from app_logs")

    assert names_and_types(cursor) == [("a", None)]


def test_query_without_a_single_stream_skips_the_schema_request(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(["level"], [["info"]])

    cursor.execute("select level from a union all select level from b")

    client.stream_schema.assert_not_called()
    assert names_and_types(cursor) == [("level", "Utf8")]


def test_schema_failure_leaves_types_to_the_values(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.stream_schema.side_effect = QueryError("no such stream")

    cursor.execute(SQL)

    assert names_and_types(cursor) == [("level", "Utf8"), ("count", "Int64")]
    assert cursor.rowcount == 3


def test_prepended_timestamp_is_dropped(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(
        ["_timestamp", "level"], [[11, "info"], [12, "warn"]]
    )

    cursor.execute("select level from app_logs")

    assert names_and_types(cursor) == [("level", "Utf8")]
    assert cursor.fetchall() == [("info",), ("warn",)]


@pytest.mark.parametrize(
    "sql",
    [
        "select _timestamp, level from app_logs",
        "select * from app_logs",
        "select distinct _timestamp, level from app_logs",
    ],
)
def test_selected_timestamp_is_kept(
    cursor: dbapi.Cursor, client: Mock, sql: str
) -> None:
    client.search.return_value = result(["_timestamp", "level"], [[11, "x"]])

    cursor.execute(sql)

    assert names_and_types(cursor) == [
        ("_timestamp", "Int64"),
        ("level", "Utf8"),
    ]
    assert cursor.fetchall() == [(11, "x")]


def test_empty_result_describes_the_selected_columns(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result([], [])

    cursor.execute("select level, app_logs.status as code from app_logs")

    assert names_and_types(cursor) == [("level", "Utf8"), ("code", None)]
    assert cursor.rowcount == 0
    assert cursor.fetchall() == []


def test_empty_result_expands_star_to_the_stream_fields(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result([], [])

    cursor.execute("select * from app_logs where false")

    assert names_and_types(cursor) == [
        ("_timestamp", "Int64"),
        ("level", "Utf8"),
        ("status", "Int64"),
    ]


def test_empty_result_names_an_unnamed_expression_by_its_sql(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result([], [])

    cursor.execute("select level, count(*) from app_logs group by level")

    assert names_and_types(cursor) == [("level", "Utf8"), ("count(*)", None)]


def test_empty_result_of_an_unparseable_query_has_no_description(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result([], [])

    cursor.execute("show streams")

    assert cursor.description == []
    assert cursor.fetchall() == []


def test_execute_without_parameters_sends_sql_unchanged(
    cursor: dbapi.Cursor, client: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    render = Mock()
    monkeypatch.setattr(dbapi.statements, "render", render)
    sql = "select * from app_logs where message like '%err%'"

    cursor.execute(sql)

    render.assert_not_called()
    assert client.search.call_args.args[0] == sql


@pytest.mark.parametrize("parameters", [{}, {"level": "error"}])
def test_execute_with_parameters_sends_rendered_sql(
    cursor: dbapi.Cursor,
    client: Mock,
    monkeypatch: pytest.MonkeyPatch,
    parameters: dict[str, Any],
) -> None:
    render = Mock(return_value="rendered sql")
    monkeypatch.setattr(dbapi.statements, "render", render)

    cursor.execute("select * from app_logs", parameters)

    render.assert_called_once_with("select * from app_logs", parameters)
    assert client.search.call_args.args[0] == "rendered sql"


def test_execute_rejects_statements_that_write(
    cursor: dbapi.Cursor, client: Mock, read_only: Mock
) -> None:
    read_only.return_value = False

    with pytest.raises(dbapi.NotSupportedError, match="read-only"):
        cursor.execute("delete from app_logs")

    read_only.assert_called_once_with("delete from app_logs")
    client.search.assert_not_called()


def test_executemany_is_not_supported(cursor: dbapi.Cursor) -> None:
    with pytest.raises(dbapi.NotSupportedError):
        cursor.executemany("select 1 from app_logs", [{}, {}])


def test_query_error_becomes_programming_error(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.side_effect = QueryError("invalid SQL syntax")

    with pytest.raises(dbapi.ProgrammingError, match="invalid SQL syntax"):
        cursor.execute("select * from")


def test_other_client_error_becomes_operational_error(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.side_effect = OpenObserveError("401 Unauthorized")

    with pytest.raises(dbapi.OperationalError, match="401 Unauthorized"):
        cursor.execute(SQL)


def test_partial_results_raise(cursor: dbapi.Cursor, client: Mock) -> None:
    client.search.return_value["is_partial"] = True

    with pytest.raises(dbapi.OperationalError, match="partial"):
        cursor.execute(SQL)

    assert cursor.description is None


def test_partial_results_error_gives_server_reason(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value["is_partial"] = True
    client.search.return_value["function_error"] = [
        "Query hit the default limit",
        "Partition failed",
    ]

    with pytest.raises(
        dbapi.OperationalError,
        match="default limit; Partition failed",
    ):
        cursor.execute(SQL)


def test_failed_execute_clears_previous_result(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    cursor.execute(SQL)
    client.search.side_effect = QueryError("invalid SQL syntax")

    with pytest.raises(dbapi.ProgrammingError):
        cursor.execute("select * from")

    assert cursor.description is None
    assert cursor.rowcount == -1
    with pytest.raises(dbapi.ProgrammingError):
        cursor.fetchall()


def test_fetch_methods_walk_the_rows_once(cursor: dbapi.Cursor) -> None:
    cursor.execute(SQL)

    assert cursor.fetchone() == ("info", 3)
    assert cursor.fetchmany() == [("warn", 2)]
    assert cursor.fetchall() == [("error", 1)]
    assert cursor.fetchone() is None
    assert cursor.fetchmany(5) == []
    assert cursor.fetchall() == []


def test_fetchmany_uses_size_then_arraysize(cursor: dbapi.Cursor) -> None:
    cursor.execute(SQL)
    cursor.arraysize = 2

    assert cursor.fetchmany(1) == [("info", 3)]
    assert cursor.fetchmany() == [("warn", 2), ("error", 1)]


def test_fetchmany_rejects_negative_size(cursor: dbapi.Cursor) -> None:
    cursor.execute(SQL)

    with pytest.raises(dbapi.ProgrammingError, match="negative"):
        cursor.fetchmany(-1)

    assert cursor.fetchall() == [("info", 3), ("warn", 2), ("error", 1)]


def test_execute_again_replaces_the_result(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    cursor.execute(SQL)
    cursor.fetchone()
    client.search.return_value = {
        "columns": ["n"],
        "rows": [[7]],
        "is_partial": False,
        "function_error": [],
    }

    cursor.execute("select count(*) as n from app_logs")

    assert cursor.fetchall() == [(7,)]


@pytest.mark.parametrize("method", ["fetchone", "fetchmany", "fetchall"])
def test_fetch_before_execute_raises(
    cursor: dbapi.Cursor, method: str
) -> None:
    with pytest.raises(dbapi.ProgrammingError, match="execute"):
        getattr(cursor, method)()


def test_closed_cursor_cannot_be_used(cursor: dbapi.Cursor) -> None:
    cursor.execute(SQL)
    cursor.close()

    with pytest.raises(dbapi.InterfaceError):
        cursor.execute(SQL)
    with pytest.raises(dbapi.InterfaceError):
        cursor.fetchall()


def test_closed_connection_cannot_be_used(
    connection: dbapi.Connection, cursor: dbapi.Cursor, client: Mock
) -> None:
    connection.close()
    connection.close()

    client.close.assert_called_once_with()
    with pytest.raises(dbapi.InterfaceError):
        connection.cursor()
    with pytest.raises(dbapi.InterfaceError):
        connection.commit()
    with pytest.raises(dbapi.InterfaceError):
        cursor.execute(SQL)


def test_commit_and_rollback_do_nothing(
    connection: dbapi.Connection, client: Mock
) -> None:
    connection.commit()
    connection.rollback()

    client.search.assert_not_called()


def test_cursor_knows_its_connection(
    connection: dbapi.Connection, cursor: dbapi.Cursor
) -> None:
    assert cursor.connection is connection


def test_connection_exposes_its_client(
    connection: dbapi.Connection, client: Mock
) -> None:
    assert connection.client is client


def test_date_and_time_constructors() -> None:
    assert dbapi.Date(2026, 11, 10) == datetime.date(2026, 11, 10)
    assert dbapi.Time(12, 30, 45) == datetime.time(12, 30, 45)
    assert dbapi.Timestamp(2026, 11, 10, 12, 30, 45) == datetime.datetime(
        2026, 11, 10, 12, 30, 45
    )
    assert dbapi.Binary(b"\x00\xff") == b"\x00\xff"


def test_from_ticks_constructors_use_local_time() -> None:
    ticks = time.mktime((2026, 11, 10, 12, 30, 45, 0, 0, -1))

    assert dbapi.DateFromTicks(ticks) == datetime.date(2026, 11, 10)
    assert dbapi.TimeFromTicks(ticks) == datetime.time(12, 30, 45)
    assert dbapi.TimestampFromTicks(ticks) == datetime.datetime(
        2026, 11, 10, 12, 30, 45
    )


@pytest.mark.parametrize(
    "type_object, type_name",
    [
        (dbapi.STRING, "Utf8"),
        (dbapi.BINARY, "Binary"),
        (dbapi.NUMBER, "Int64"),
        (dbapi.NUMBER, "Float64"),
        (dbapi.DATETIME, "Timestamp"),
    ],
)
def test_type_objects_match_their_type_names(
    type_object: object, type_name: str
) -> None:
    assert type_object == type_name
    assert type_name == type_object


def test_type_objects_do_not_match_other_type_codes() -> None:
    assert dbapi.STRING != "Int64"
    assert dbapi.NUMBER != "Utf8"
    assert dbapi.STRING != None  # noqa: E711
    assert dbapi.ROWID != "Utf8"


def test_a_selected_timestamp_alias_is_kept(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(["_timestamp"], [[3000]])

    cursor.execute("select count(*) as _timestamp from app_logs")

    assert names_and_types(cursor) == [("_timestamp", "Int64")]
    assert cursor.fetchall() == [(3000,)]


def test_a_prepended_timestamp_before_a_timestamp_alias_is_dropped(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(
        ["_timestamp", "_timestamp"], [[11, "info"]]
    )

    cursor.execute("select level as _timestamp from app_logs")

    assert cursor.fetchall() == [("info",)]
    assert cursor.description is not None
    assert len(cursor.description) == 1


def test_aliased_timestamp_aggregate_next_to_a_column_is_kept(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(
        ["_timestamp", "level"], [[11, "info"]]
    )

    cursor.execute(
        "select max(_timestamp) as _timestamp, level from app_logs "
        "group by level"
    )

    assert names_and_types(cursor) == [
        ("_timestamp", "Int64"),
        ("level", "Utf8"),
    ]
    assert cursor.fetchall() == [(11, "info")]


def test_star_over_a_cte_keeps_the_servers_columns(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(["level", "status"], [["info", 200]])

    cursor.execute(
        "with c as (select level, status from app_logs) select * from c"
    )

    assert names_and_types(cursor) == [("level", "Utf8"), ("status", "Int64")]
    assert cursor.fetchall() == [("info", 200)]


def test_empty_star_over_a_cte_is_not_expanded_to_the_stream(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result([], [])

    cursor.execute(
        "with c as (select level from app_logs) select * from c where false"
    )

    assert cursor.description == []


def test_an_alias_does_not_take_the_type_of_a_field_with_its_name(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(["status", "level"], [["500", 200]])

    cursor.execute(
        "select cast(status as varchar) as status, status as level "
        "from app_logs"
    )

    assert names_and_types(cursor) == [("status", "Utf8"), ("level", "Int64")]


def test_schema_type_wins_over_the_value_type(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(["status"], [[200.0]])

    cursor.execute("select status from app_logs")

    assert names_and_types(cursor) == [("status", "Int64")]


def test_a_quoted_alias_keeps_its_case(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(["Lvl"], [])

    cursor.execute('select level as "Lvl" from app_logs')

    assert names_and_types(cursor) == [("Lvl", None)]


def test_a_column_renamed_in_a_cte_does_not_take_the_streams_type(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(["status"], [["500"]])

    cursor.execute(
        "with c as (select cast(status as varchar) as status from app_logs) "
        "select status from c"
    )

    assert names_and_types(cursor) == [("status", "Utf8")]


def test_timestamp_is_kept_when_the_select_list_is_unknown(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(["_timestamp", "level"], [[1, "x"]])

    cursor.execute("with c as (select * from app_logs) select * from c")

    assert names_and_types(cursor) == [
        ("_timestamp", "Int64"),
        ("level", "Utf8"),
    ]


def test_star_keeps_the_timestamp_when_the_schema_is_a_field_short(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(
        ["_timestamp", "level", "status", "new_field"], [[1, "x", 200, "y"]]
    )

    cursor.execute("select * from app_logs")

    assert [name for name, _ in names_and_types(cursor)] == [
        "_timestamp",
        "level",
        "status",
        "new_field",
    ]
    assert cursor.fetchall() == [(1, "x", 200, "y")]


def test_a_query_of_only_expressions_makes_no_schema_request(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = result(["count(*)"], [[3]])

    cursor.execute("select count(*) from app_logs")

    client.stream_schema.assert_not_called()
    assert names_and_types(cursor) == [("count(*)", "Int64")]
