"""DBAPI 2.0 (PEP 249) driver for OpenObserve"""

from typing import Any

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


class Cursor:
    description: list[tuple[Any, ...]] | None = None
    rowcount = -1
    arraysize = 1
    lastrowid = None

    def execute(self, operation: str, parameters: Any = None) -> None:
        raise NotImplementedError

    def executemany(self, operation: str, seq_of_parameters: Any) -> None:
        raise NotImplementedError

    def fetchone(self) -> tuple[Any, ...] | None:
        raise NotImplementedError

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        raise NotImplementedError

    def fetchall(self) -> list[tuple[Any, ...]]:
        raise NotImplementedError

    def setinputsizes(self, sizes: Any) -> None:
        pass

    def setoutputsize(self, size: int, column: int | None = None) -> None:
        pass

    def close(self) -> None:
        pass


class Connection:
    def cursor(self) -> Cursor:
        raise NotImplementedError

    def commit(self) -> None:
        # OpenObserve is read-only through this driver
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def connect(**kwargs: Any) -> Connection:
    raise NotImplementedError
