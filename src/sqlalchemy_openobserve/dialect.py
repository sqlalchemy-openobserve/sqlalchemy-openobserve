"""SQLAlchemy dialect for OpenObserve"""

from types import ModuleType

from sqlalchemy.engine import default


class OpenObserveDialect(default.DefaultDialect):
    name = "openobserve"
    driver = "rest"
    supports_statement_cache = False

    @classmethod
    def import_dbapi(cls) -> ModuleType:
        from sqlalchemy_openobserve import dbapi

        return dbapi
