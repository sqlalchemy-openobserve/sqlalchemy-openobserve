"""The SQLAlchemy dialect against a live OpenObserve"""

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import exc

from sqlalchemy_openobserve import dbapi

from .conftest import ROWS, URL


def test_ping_uses_the_client_not_select_1(engine: sa.Engine) -> None:
    raw = engine.raw_connection()
    try:
        assert engine.dialect.do_ping(raw.dbapi_connection)
    finally:
        raw.close()


def test_ping_fails_with_a_wrong_password(stream: str) -> None:
    engine = sa.create_engine(URL.replace("Complexpass", "Wrongpass"))
    raw = engine.raw_connection()
    try:
        with pytest.raises(dbapi.OperationalError):
            engine.dialect.do_ping(raw.dbapi_connection)
    finally:
        raw.close()
        engine.dispose()


def test_reflection_lists_the_stream_and_its_columns(
    engine: sa.Engine, stream: str
) -> None:
    inspector = sa.inspect(engine)

    assert inspector.get_schema_names() == ["default"]
    assert stream in inspector.get_table_names()
    columns = {c["name"]: c["type"] for c in inspector.get_columns(stream)}
    assert isinstance(columns["_timestamp"], sa.BigInteger)
    assert isinstance(columns["level"], sa.String)
    assert isinstance(columns["status"], sa.BigInteger)
    assert isinstance(columns["duration_ms"], sa.Float)


def test_reflecting_an_unknown_table_raises_no_such_table(
    engine: sa.Engine,
) -> None:
    with pytest.raises(exc.NoSuchTableError):
        sa.Table("no_such_stream_here", sa.MetaData(), autoload_with=engine)


def test_a_reflected_table_can_be_grouped(
    engine: sa.Engine, stream: str
) -> None:
    table = sa.Table(stream, sa.MetaData(), autoload_with=engine)
    count = sa.func.count().label("n")
    query = (
        sa.select(table.c.level, count)
        .group_by(table.c.level)
        .order_by(table.c.level)
    )

    with engine.connect() as connection:
        result = connection.execute(query)
        keys = list(result.keys())
        rows = result.all()

    assert keys == ["level", "n"]
    assert [tuple(row) for row in rows] == [
        ("error", 375),
        ("info", 750),
        ("warn", 375),
    ]


def test_text_with_bind_parameters_and_percent_signs(
    engine: sa.Engine, stream: str
) -> None:
    query = sa.text(
        f"select count(*) from {stream} "
        "where service = :service and message like 'request 1%'"
    )

    with engine.connect() as connection:
        count = connection.execute(query, {"service": "api"}).scalar_one()

    assert count == 202


def test_a_where_false_select_still_has_its_columns(
    engine: sa.Engine, stream: str
) -> None:
    table = sa.Table(stream, sa.MetaData(), autoload_with=engine)

    with engine.connect() as connection:
        result = connection.execute(
            sa.select(table.c.level, table.c.status).where(sa.false())
        )
        assert list(result.keys()) == ["level", "status"]
        assert result.all() == []


def test_a_percent_sign_in_a_string_literal_is_not_doubled(
    engine: sa.Engine, stream: str
) -> None:
    query = sa.text(
        f"select count(*) from {stream} where service = 'it''s 100% \"odd\"'"
    )

    with engine.connect() as connection:
        assert connection.execute(query).scalar_one() == 500


def _count_inlined(engine: sa.Engine, stream: str, condition: Any) -> int:
    table = sa.table(stream, sa.column("service"), sa.column("message"))
    query = (
        sa.select(sa.func.count()).select_from(table).where(condition(table.c))
    )
    sql = str(query.compile(engine, compile_kwargs={"literal_binds": True}))
    with engine.connect() as connection:
        return int(connection.exec_driver_sql(sql).scalar_one())


@pytest.mark.parametrize(
    "condition",
    [
        lambda c: c.service == "\\' OR 1=1 --",
        lambda c: c.service.in_(["x\\", "\\' OR 1=1 --"]),
        lambda c: c.service.contains("\\' OR 1=1 --"),
        lambda c: c.service == sa.literal(b"\\' OR 1=1 --", sa.LargeBinary),
    ],
)
def test_inlined_literals_cannot_match_everything(
    engine: sa.Engine, stream: str, condition: Any
) -> None:
    assert _count_inlined(engine, stream, condition) == 0


def test_inlined_literals_still_match_real_values(
    engine: sa.Engine, stream: str
) -> None:
    odd = 'it\'s 100% "odd"'

    assert (
        _count_inlined(engine, stream, lambda c: c.service == odd) == ROWS // 3
    )


def test_like_with_a_backslash_escape_character_works(
    engine: sa.Engine, stream: str
) -> None:
    unescaped = _count_inlined(
        engine, stream, lambda c: c.message.like("request 1_")
    )
    escaped = _count_inlined(
        engine, stream, lambda c: c.message.like("request 1\\_", escape="\\")
    )

    assert unescaped == 10
    assert escaped == 0


def _inlined_value(engine: sa.Engine, stream: str, expression: Any) -> Any:
    table = sa.table(stream, sa.column("message"))
    query = sa.select(expression).select_from(table).limit(1)
    sql = str(query.compile(engine, compile_kwargs={"literal_binds": True}))
    with engine.connect() as connection:
        return connection.exec_driver_sql(sql).scalar_one()


@pytest.mark.parametrize(
    ("text", "expected"), [("a_", True), ("a\\b", False), ("ab", False)]
)
def test_an_escaped_underscore_matches_only_a_literal_underscore(
    engine: sa.Engine, stream: str, text: str, expected: bool
) -> None:
    expression = sa.literal(text).like("a\\_", escape="\\")

    assert _inlined_value(engine, stream, expression) is expected


@pytest.mark.parametrize("value", ["a\\b", "x\\", "\\' OR 1=1 --", "\\\\"])
def test_an_inlined_backslash_literal_round_trips(
    engine: sa.Engine, stream: str, value: str
) -> None:
    table = sa.table(stream, sa.column("message"))
    query = sa.select(sa.literal(value)).select_from(table).limit(1)
    sql = str(query.compile(engine, compile_kwargs={"literal_binds": True}))

    with engine.connect() as connection:
        assert connection.exec_driver_sql(sql).scalar_one() == value
