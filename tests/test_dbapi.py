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


def test_execute_sets_description_and_rowcount(cursor: dbapi.Cursor) -> None:
    assert cursor.description is None
    assert cursor.rowcount == -1

    cursor.execute(SQL)

    assert cursor.description == [
        ("level", None, None, None, None, None, None),
        ("count", None, None, None, None, None, None),
    ]
    assert cursor.rowcount == 3


def test_empty_result_has_empty_description(
    cursor: dbapi.Cursor, client: Mock
) -> None:
    client.search.return_value = {
        "columns": [],
        "rows": [],
        "is_partial": False,
        "function_error": [],
    }

    cursor.execute(SQL)

    assert cursor.description == []
    assert cursor.rowcount == 0
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
