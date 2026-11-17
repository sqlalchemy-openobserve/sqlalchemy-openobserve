"""SQLAlchemy dialect for OpenObserve"""

from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType
from typing import Any, cast

from sqlalchemy import exc, types as sqltypes, util
from sqlalchemy.engine import Connection, default, reflection
from sqlalchemy.engine.interfaces import (
    ConnectArgsType,
    DBAPIConnection,
    ReflectedCheckConstraint,
    ReflectedColumn,
    ReflectedForeignKeyConstraint,
    ReflectedIndex,
    ReflectedPrimaryKeyConstraint,
    ReflectedUniqueConstraint,
)
from sqlalchemy.engine.url import URL
from sqlalchemy.sql import compiler

from . import dbapi
from .client import OpenObserveClient, OpenObserveError

# OpenObserve has no schemas. Reflection reports this one, as
# elasticsearch-dbapi does, and compiled SQL leaves it out.
DEFAULT_SCHEMA = "default"
DEFAULT_PORT = 5080

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})

# Arrow type names as the stream schema API reports them
_TYPES: dict[str, type[sqltypes.TypeEngine[Any]]] = {
    "Utf8": sqltypes.String,
    "LargeUtf8": sqltypes.String,
    "Boolean": sqltypes.Boolean,
    "Int8": sqltypes.Integer,
    "Int16": sqltypes.Integer,
    "Int32": sqltypes.Integer,
    "Int64": sqltypes.BigInteger,
    "UInt8": sqltypes.Integer,
    "UInt16": sqltypes.Integer,
    "UInt32": sqltypes.BigInteger,
    "UInt64": sqltypes.BigInteger,
    "Float32": sqltypes.Float,
    "Float64": sqltypes.Double,
    "Date32": sqltypes.Date,
    "Date64": sqltypes.Date,
    "Binary": sqltypes.LargeBinary,
    "LargeBinary": sqltypes.LargeBinary,
}


def _column_type(name: str, type_name: str) -> sqltypes.TypeEngine[Any]:
    # _timestamp stays the Int64 it is (microseconds since the epoch): the
    # server rejects comparing it with a timestamp, and returns it as an
    # integer. Superset's engine spec marks it as the time column.
    if type_name.startswith("Timestamp"):
        return sqltypes.TIMESTAMP()
    if type_name in _TYPES:
        return _TYPES[type_name]()
    util.warn(f"Did not recognize OpenObserve type {type_name!r} of {name!r}")
    return sqltypes.NULLTYPE


def _parse_bool(name: str, value: str) -> bool:
    if value.lower() in _TRUE:
        return True
    if value.lower() in _FALSE:
        return False
    raise exc.ArgumentError(f"{name} must be true or false, not {value!r}")


def _client(dbapi_connection: DBAPIConnection | None) -> OpenObserveClient:
    return cast(dbapi.Connection, dbapi_connection).client


@contextmanager
def _translate_errors() -> Iterator[None]:
    try:
        yield
    except OpenObserveError as error:
        raise dbapi.OperationalError(str(error)) from error


class OpenObserveIdentifierPreparer(compiler.IdentifierPreparer):
    # Covers both table and column references, unlike omit_schema
    schema_for_object = staticmethod(lambda obj: "")


class OpenObserveDialect(default.DefaultDialect):
    name = "openobserve"
    driver = "rest"
    supports_statement_cache = False
    preparer = OpenObserveIdentifierPreparer

    @classmethod
    def import_dbapi(cls) -> ModuleType:
        return dbapi

    def create_connect_args(self, url: URL) -> ConnectArgsType:
        """
        Map ``openobserve://email:password@host:port/organization`` onto
        the keywords of ``dbapi.connect()``.

        ``?ssl=false`` connects over plain HTTP. ``?verify=false`` skips
        TLS certificate checks; any other value is a CA bundle path.
        """
        parts = {
            "host": url.host,
            "organization": url.database,
            "email": url.username,
            "password": url.password,
        }
        for part, value in parts.items():
            if not value:
                raise exc.ArgumentError(f"OpenObserve URL has no {part}")

        kwargs: dict[str, Any] = {**parts, "port": url.port or DEFAULT_PORT}
        for key, param in url.query.items():
            if not isinstance(param, str):
                raise exc.ArgumentError(f"{key} is given more than once")
            if key == "ssl":
                kwargs["ssl"] = _parse_bool(key, param)
            elif key == "verify":
                is_bool = param.lower() in _TRUE | _FALSE
                kwargs["verify"] = (
                    _parse_bool(key, param) if is_bool else param
                )
            else:
                raise exc.ArgumentError(
                    f"Unknown OpenObserve URL parameter {key!r}"
                )
        return [], kwargs

    def do_ping(self, dbapi_connection: DBAPIConnection) -> bool:
        # SELECT 1 is not valid here: every query must read a stream
        with _translate_errors():
            _client(dbapi_connection).ping()
        return True

    def _get_default_schema_name(self, connection: Connection) -> str:
        return DEFAULT_SCHEMA

    def get_schema_names(self, connection: Connection, **kw: Any) -> list[str]:
        return [DEFAULT_SCHEMA]

    @reflection.cache
    def get_table_names(
        self, connection: Connection, schema: str | None = None, **kw: Any
    ) -> list[str]:
        # Search only reads logs streams, so metrics and traces are left out
        if schema not in (None, DEFAULT_SCHEMA):
            return []
        with _translate_errors():
            return _client(
                connection.connection.dbapi_connection
            ).list_streams()

    def get_view_names(
        self, connection: Connection, schema: str | None = None, **kw: Any
    ) -> list[str]:
        return []

    def has_table(
        self,
        connection: Connection,
        table_name: str,
        schema: str | None = None,
        **kw: Any,
    ) -> bool:
        return table_name in self.get_table_names(connection, schema, **kw)

    @reflection.cache
    def get_columns(
        self,
        connection: Connection,
        table_name: str,
        schema: str | None = None,
        **kw: Any,
    ) -> list[ReflectedColumn]:
        if schema not in (None, DEFAULT_SCHEMA):
            raise exc.NoSuchTableError(table_name)
        client = _client(connection.connection.dbapi_connection)
        try:
            fields = client.stream_schema(table_name)
        except OpenObserveError as error:
            if not self.has_table(connection, table_name, schema, **kw):
                raise exc.NoSuchTableError(table_name) from error
            raise dbapi.OperationalError(str(error)) from error
        return [
            {
                "name": field["name"],
                "type": _column_type(field["name"], field["type"]),
                "nullable": field["name"] != "_timestamp",
                "default": None,
            }
            for field in fields
        ]

    def get_pk_constraint(
        self,
        connection: Connection,
        table_name: str,
        schema: str | None = None,
        **kw: Any,
    ) -> ReflectedPrimaryKeyConstraint:
        return {"constrained_columns": [], "name": None}

    def get_foreign_keys(
        self,
        connection: Connection,
        table_name: str,
        schema: str | None = None,
        **kw: Any,
    ) -> list[ReflectedForeignKeyConstraint]:
        return []

    def get_indexes(
        self,
        connection: Connection,
        table_name: str,
        schema: str | None = None,
        **kw: Any,
    ) -> list[ReflectedIndex]:
        return []

    def get_unique_constraints(
        self,
        connection: Connection,
        table_name: str,
        schema: str | None = None,
        **kw: Any,
    ) -> list[ReflectedUniqueConstraint]:
        return []

    def get_check_constraints(
        self,
        connection: Connection,
        table_name: str,
        schema: str | None = None,
        **kw: Any,
    ) -> list[ReflectedCheckConstraint]:
        return []
