"""Tests for the SQLAlchemy dialect"""

import urllib.error
import urllib.request
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy import (
    column,
    create_engine,
    exc,
    func,
    inspect,
    literal,
    select,
    table,
    types as sqltypes,
)
from sqlalchemy.engine import make_url

from sqlalchemy_openobserve import dbapi
from sqlalchemy_openobserve.client import OpenObserveClient, OpenObserveError
from sqlalchemy_openobserve.dialect import OpenObserveDialect
from sqlalchemy_openobserve.statements import is_read_only

URL = "openobserve://root%40example.com:secret@observe.example/default"


@pytest.fixture
def dialect() -> OpenObserveDialect:
    return OpenObserveDialect()


@pytest.fixture
def client() -> Mock:
    client = Mock(spec=OpenObserveClient)
    client.list_streams.return_value = ["app_logs", "audit"]
    client.stream_schema.return_value = [
        {"name": "_timestamp", "type": "Int64"},
        {"name": "level", "type": "Utf8"},
        {"name": "status", "type": "Int64"},
        {"name": "duration_ms", "type": "Float64"},
        {"name": "ok", "type": "Boolean"},
    ]
    return client


@pytest.fixture
def connection(client: Mock) -> Mock:
    # A SQLAlchemy Connection, as reflection methods receive it
    connection = Mock()
    connection.connection.dbapi_connection = dbapi.Connection(client)
    return connection


def connect_args(url: str) -> dict[str, Any]:
    args, kwargs = OpenObserveDialect().create_connect_args(make_url(url))
    assert args == []
    return dict(kwargs)


def test_connect_args_map_url_parts() -> None:
    assert connect_args(URL) == {
        "host": "observe.example",
        "port": 5080,
        "organization": "default",
        "email": "root@example.com",
        "password": "secret",
    }


def test_connect_args_keep_explicit_port() -> None:
    assert connect_args(URL.replace(".example", ".example:443"))["port"] == 443


@pytest.mark.parametrize(
    "query, expected",
    [
        ("ssl=false", {"ssl": False}),
        ("ssl=True", {"ssl": True}),
        ("verify=false", {"verify": False}),
        ("verify=/etc/ssl/ca.pem", {"verify": "/etc/ssl/ca.pem"}),
        ("ssl=0&verify=no", {"ssl": False, "verify": False}),
    ],
)
def test_connect_args_read_query(query: str, expected: dict[str, Any]) -> None:
    kwargs = connect_args(f"{URL}?{query}")

    assert {key: kwargs[key] for key in expected} == expected


@pytest.mark.parametrize(
    "url, message",
    [
        ("openobserve://u:p@/default", "host"),
        ("openobserve://u:p@observe.example", "organization"),
        ("openobserve://observe.example/default", "email"),
        ("openobserve://u@observe.example/default", "password"),
        (f"{URL}?ssl=maybe", "ssl must be true or false"),
        (f"{URL}?ssl=false&ssl=true", "more than once"),
        (f"{URL}?timeout=5", "Unknown OpenObserve URL parameter 'timeout'"),
    ],
)
def test_connect_args_reject_bad_url(url: str, message: str) -> None:
    with pytest.raises(exc.ArgumentError, match=message):
        connect_args(url)


def test_ping_checks_the_client(
    dialect: OpenObserveDialect, client: Mock
) -> None:
    assert dialect.do_ping(dbapi.Connection(client)) is True
    client.ping.assert_called_once_with()


def test_failed_ping_raises_dbapi_error(
    dialect: OpenObserveDialect, client: Mock
) -> None:
    client.ping.side_effect = OpenObserveError("401 Unauthorized")

    with pytest.raises(dbapi.OperationalError, match="401 Unauthorized"):
        dialect.do_ping(dbapi.Connection(client))


def test_only_schema_is_default(
    dialect: OpenObserveDialect, connection: Mock
) -> None:
    assert dialect.get_schema_names(connection) == ["default"]
    assert dialect._get_default_schema_name(connection) == "default"


@pytest.mark.parametrize("schema", [None, "default"])
def test_table_names_are_logs_streams(
    dialect: OpenObserveDialect,
    connection: Mock,
    client: Mock,
    schema: str | None,
) -> None:
    assert dialect.get_table_names(connection, schema) == ["app_logs", "audit"]
    client.list_streams.assert_called_once_with()


def test_other_schema_has_no_tables(
    dialect: OpenObserveDialect, connection: Mock, client: Mock
) -> None:
    assert dialect.get_table_names(connection, "other") == []
    assert not dialect.has_table(connection, "app_logs", "other")
    client.list_streams.assert_not_called()


def test_table_names_error_becomes_dbapi_error(
    dialect: OpenObserveDialect, connection: Mock, client: Mock
) -> None:
    client.list_streams.side_effect = OpenObserveError("401 Unauthorized")

    with pytest.raises(dbapi.OperationalError, match="401 Unauthorized"):
        dialect.get_table_names(connection)


def test_has_table(dialect: OpenObserveDialect, connection: Mock) -> None:
    assert dialect.has_table(connection, "app_logs")
    assert not dialect.has_table(connection, "missing")


def test_columns_map_openobserve_types(
    dialect: OpenObserveDialect, connection: Mock, client: Mock
) -> None:
    columns = dialect.get_columns(connection, "app_logs")

    client.stream_schema.assert_called_once_with("app_logs")
    assert [(c["name"], type(c["type"]), c["nullable"]) for c in columns] == [
        ("_timestamp", sqltypes.BigInteger, False),
        ("level", sqltypes.String, True),
        ("status", sqltypes.BigInteger, True),
        ("duration_ms", sqltypes.Double, True),
        ("ok", sqltypes.Boolean, True),
    ]
    assert all(c["default"] is None for c in columns)


@pytest.mark.parametrize(
    "type_name, expected",
    [
        ("Int32", sqltypes.Integer),
        ("UInt64", sqltypes.BigInteger),
        ("Float32", sqltypes.Float),
        ("LargeUtf8", sqltypes.String),
        ("Date32", sqltypes.Date),
        ("Timestamp(Microsecond, None)", sqltypes.TIMESTAMP),
        ("Binary", sqltypes.LargeBinary),
    ],
)
def test_other_column_types(
    dialect: OpenObserveDialect,
    connection: Mock,
    client: Mock,
    type_name: str,
    expected: type[sqltypes.TypeEngine[Any]],
) -> None:
    client.stream_schema.return_value = [{"name": "f", "type": type_name}]

    [reflected] = dialect.get_columns(connection, "app_logs")

    assert type(reflected["type"]) is expected


def test_unknown_column_type_warns(
    dialect: OpenObserveDialect, connection: Mock, client: Mock
) -> None:
    client.stream_schema.return_value = [{"name": "f", "type": "Decimal128"}]

    with pytest.warns(exc.SAWarning, match="Decimal128"):
        [reflected] = dialect.get_columns(connection, "app_logs")

    assert isinstance(reflected["type"], sqltypes.NullType)


def test_columns_of_missing_stream(
    dialect: OpenObserveDialect, connection: Mock, client: Mock
) -> None:
    client.stream_schema.side_effect = OpenObserveError("404 Not Found")

    with pytest.raises(exc.NoSuchTableError, match="missing"):
        dialect.get_columns(connection, "missing")


def test_columns_error_of_existing_stream(
    dialect: OpenObserveDialect, connection: Mock, client: Mock
) -> None:
    client.stream_schema.side_effect = OpenObserveError("500 Server Error")

    with pytest.raises(dbapi.OperationalError, match="500 Server Error"):
        dialect.get_columns(connection, "app_logs")


def test_columns_in_other_schema(
    dialect: OpenObserveDialect, connection: Mock, client: Mock
) -> None:
    with pytest.raises(exc.NoSuchTableError):
        dialect.get_columns(connection, "app_logs", "other")

    client.stream_schema.assert_not_called()


def test_streams_have_no_constraints_indexes_or_views(
    dialect: OpenObserveDialect, connection: Mock
) -> None:
    assert dialect.get_pk_constraint(connection, "app_logs") == {
        "constrained_columns": [],
        "name": None,
    }
    assert dialect.get_foreign_keys(connection, "app_logs") == []
    assert dialect.get_indexes(connection, "app_logs") == []
    assert dialect.get_unique_constraints(connection, "app_logs") == []
    assert dialect.get_check_constraints(connection, "app_logs") == []
    assert dialect.get_view_names(connection) == []


def test_compiled_sql_leaves_out_schema(dialect: OpenObserveDialect) -> None:
    logs = table("app_logs", column("level"), schema="default")
    query = (
        select(logs.c.level, func.count())
        .where(logs.c.level == "error")
        .group_by(logs.c.level)
    )

    sql = str(
        query.compile(dialect=dialect, compile_kwargs={"literal_binds": True})
    )

    # SQLAlchemy aliases a schema-qualified table whose schema is left out,
    # so it can't be confused with a table of the same name in no schema
    assert sql == (
        "SELECT app_logs_1.level, count(*) AS count_1 \n"
        "FROM app_logs AS app_logs_1 \n"
        "WHERE app_logs_1.level = 'error' GROUP BY app_logs_1.level"
    )


def test_compiled_sql_without_schema_has_no_alias(
    dialect: OpenObserveDialect,
) -> None:
    logs = table("app_logs", column("level"))

    sql = str(select(logs.c.level).compile(dialect=dialect))

    assert sql == "SELECT app_logs.level \nFROM app_logs"


def test_inspector_through_an_engine(
    monkeypatch: pytest.MonkeyPatch, client: Mock
) -> None:
    client_class = Mock(return_value=client)
    monkeypatch.setattr(dbapi, "OpenObserveClient", client_class)
    engine = create_engine(f"{URL}?ssl=false")

    inspector = inspect(engine)

    assert inspector.default_schema_name == "default"
    assert inspector.get_table_names() == ["app_logs", "audit"]
    assert inspector.has_table("audit")
    assert [c["name"] for c in inspector.get_columns("app_logs")][:2] == [
        "_timestamp",
        "level",
    ]
    client_class.assert_called_once_with(
        "observe.example",
        5080,
        "default",
        "root@example.com",
        "secret",
        ssl=False,
        verify=True,
    )


# The local OpenObserve of docker-compose.yml, with the sample data of
# scripts/load_sample_data.py


LOCAL_URL = (
    "openobserve://root%40example.com:Complexpass%23123@localhost:5080/"
    "default?ssl=false"
)


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(
            "http://localhost:5080/healthz", timeout=1
        ):
            return True
    except (urllib.error.URLError, OSError):
        return False


def test_ping_and_reflection_against_real_server() -> None:
    if not _server_up():
        pytest.skip(
            "OpenObserve is not running; start it with: docker compose up -d"
        )
    engine = create_engine(LOCAL_URL)
    inspector = inspect(engine)

    assert "app_logs" in inspector.get_table_names()
    columns = {c["name"]: c["type"] for c in inspector.get_columns("app_logs")}
    assert isinstance(columns["_timestamp"], sqltypes.BigInteger)
    assert isinstance(columns["level"], sqltypes.String)
    with pytest.raises(exc.NoSuchTableError):
        inspector.get_columns("no_such_stream")

    connection = engine.raw_connection()
    try:
        assert engine.dialect.do_ping(connection)
    finally:
        connection.close()

    wrong_password = create_engine(LOCAL_URL.replace("%23123", "%23999"))
    connection = wrong_password.raw_connection()
    try:
        with pytest.raises(dbapi.OperationalError, match="401"):
            wrong_password.dialect.do_ping(connection)
    finally:
        connection.close()


def _literal_sql(expression: Any) -> str:
    engine = create_engine(LOCAL_URL)
    return str(
        select(column("service"))
        .where(expression)
        .compile(engine, compile_kwargs={"literal_binds": True})
    )


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        ("\\' OR 1=1 --", "concat(chr(92), ''' OR 1=1 --')"),
        ("a\\b", "concat('a', chr(92), 'b')"),
        ("x\\", "concat('x', chr(92))"),
        ("\\", "chr(92)"),
    ],
)
def test_literal_binds_sends_no_backslash(value: str, literal: str) -> None:
    sql = _literal_sql(column("service") == value)

    assert sql.endswith(f"WHERE service = {literal}")
    assert "\\" not in sql


def test_literal_binds_keeps_the_default_quoting_otherwise() -> None:
    assert _literal_sql(column("service") == "it's 100% odd").endswith(
        "WHERE service = 'it''s 100%% odd'"
    )
    assert _literal_sql(column("service").like("%err%")).endswith(
        "WHERE service LIKE '%%err%%'"
    )
    assert _literal_sql(column("service") == 5).endswith("WHERE service = 5")


def test_literal_binds_in_list_and_contains_send_no_backslash() -> None:
    in_sql = _literal_sql(column("service").in_(["a", "b\\c"]))
    contains_sql = _literal_sql(column("service").contains("x\\y"))

    assert "concat('b', chr(92), 'c')" in in_sql
    assert "concat('x', chr(92), 'y')" in contains_sql
    assert "\\" not in in_sql + contains_sql


def test_like_escape_character_stays_a_literal() -> None:
    sql = _literal_sql(column("service").like("a%", escape="\\"))

    assert sql.endswith("WHERE service LIKE 'a%%' ESCAPE '\\'")
    assert is_read_only(sql)


def test_a_backslash_value_is_rewritten_for_any_literal_type() -> None:
    injection = "\\' OR 1=1 --"
    binary = _literal_sql(
        column("service") == literal(injection.encode(), sqltypes.LargeBinary)
    )

    assert "\\" not in binary


class _Prefixed(sqltypes.TypeDecorator[str]):
    impl = sqltypes.String
    cache_ok = True

    def process_literal_param(self, value: Any, dialect: Any) -> str:
        return f"p:{value}"


def test_a_type_decorator_sees_the_whole_value() -> None:
    sql = _literal_sql(column("service", _Prefixed()) == "a\\b")

    assert sql.endswith("WHERE service = concat('p:a', chr(92), 'b')")


def test_a_backslash_in_a_literal_that_cannot_be_rewritten_fails() -> None:
    with pytest.raises(exc.CompileError, match="safely"):
        _literal_sql(
            column("service")
            == literal(["a\\b"], sqltypes.ARRAY(sqltypes.String))
        )
