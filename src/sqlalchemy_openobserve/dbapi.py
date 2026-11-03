"""DBAPI 2.0 (PEP 249) driver for OpenObserve"""

from typing import Any

from . import statements
from .client import OpenObserveClient, OpenObserveError, QueryError
from .timerange import extract_time_range

apilevel = "2.0"
threadsafety = 1
# OpenObserve has no server-side bind parameters, so the cursor renders
# literals itself. execute() applies `operation % parameters` only when
# parameters is not None:
# - through an Engine, SQLAlchemy passes {} and doubles every literal "%"
#   ("like '%err%'" arrives as "like '%%err%%'");
# - raw callers such as Superset pass no parameters and undoubled SQL.
paramstyle = "pyformat"


class Warning(Exception):
    pass


class Error(Exception):
    pass


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class DataError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


# OpenObserve's QUERY_WITH_NO_LIMIT. Any other size caps the rows a search
# returns without marking the response partial, so the SQL's own LIMIT is
# left to decide.
_NO_LIMIT = -999

Row = tuple[Any, ...]


class Cursor:
    arraysize = 1
    lastrowid = None

    def __init__(self, connection: "Connection") -> None:
        self.connection = connection
        self.description: list[tuple[Any, ...]] | None = None
        self.rowcount = -1
        self._rows: list[Row] = []
        self._position = 0
        self._closed = False

    def _check_open(self) -> None:
        if self._closed:
            raise InterfaceError("Cursor is closed")
        self.connection._check_open()

    def _check_result(self) -> None:
        self._check_open()
        if self.description is None:
            raise ProgrammingError("No result set: call execute() first")

    def execute(self, operation: str, parameters: Any = None) -> None:
        self._check_open()
        self.description = None
        self.rowcount = -1
        self._rows = []
        self._position = 0

        if parameters is None:
            sql = operation
        else:
            sql = statements.render(operation, parameters)
        if not statements.is_read_only(sql):
            raise NotSupportedError(
                "OpenObserve is read-only through this driver"
            )
        start_time, end_time = extract_time_range(sql)
        try:
            result = self.connection._client.search(
                sql, start_time, end_time, size=_NO_LIMIT
            )
        except QueryError as exc:
            raise ProgrammingError(str(exc)) from exc
        except OpenObserveError as exc:
            raise OperationalError(str(exc)) from exc
        if result["is_partial"]:
            raise OperationalError(
                "OpenObserve returned partial results for this query"
            )

        # The search API returns column names without types
        self.description = [
            (name, None, None, None, None, None, None)
            for name in result["columns"]
        ]
        self._rows = [tuple(row) for row in result["rows"]]
        self.rowcount = len(self._rows)

    def executemany(self, operation: str, seq_of_parameters: Any) -> None:
        raise NotSupportedError("OpenObserve is read-only through this driver")

    def fetchone(self) -> Row | None:
        self._check_result()
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchmany(self, size: int | None = None) -> list[Row]:
        self._check_result()
        if size is None:
            size = self.arraysize
        rows = self._rows[self._position : self._position + size]
        self._position += len(rows)
        return rows

    def fetchall(self) -> list[Row]:
        self._check_result()
        rows = self._rows[self._position :]
        self._position = len(self._rows)
        return rows

    def setinputsizes(self, sizes: Any) -> None:
        pass

    def setoutputsize(self, size: int, column: int | None = None) -> None:
        pass

    def close(self) -> None:
        self._closed = True
        self._rows = []


class Connection:
    def __init__(self, client: OpenObserveClient) -> None:
        self._client = client
        self._closed = False

    def _check_open(self) -> None:
        if self._closed:
            raise InterfaceError("Connection is closed")

    def cursor(self) -> Cursor:
        self._check_open()
        return Cursor(self)

    def commit(self) -> None:
        # OpenObserve is read-only through this driver
        self._check_open()

    def rollback(self) -> None:
        self._check_open()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._client.close()


def connect(
    *,
    host: str,
    port: int,
    organization: str,
    email: str,
    password: str,
    ssl: bool = True,
    verify: bool | str = True,
) -> Connection:
    """
    Open a connection to one OpenObserve organization.

    No request is made until a cursor runs a query.
    """
    client = OpenObserveClient(
        host, port, organization, email, password, ssl=ssl, verify=verify
    )
    return Connection(client)
