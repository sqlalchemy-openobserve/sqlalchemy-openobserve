"""SQLAlchemy dialect and DBAPI driver for OpenObserve"""

from importlib.metadata import PackageNotFoundError, version

from sqlalchemy.dialects import registry

from .dialect import OpenObserveDialect

try:
    __version__ = version("sqlalchemy-openobserve")
except PackageNotFoundError:
    # Running from a source tree or a vendored copy
    __version__ = "0+unknown"

__all__ = ["OpenObserveDialect", "__version__"]

for _name in ("openobserve", "openobserve.rest"):
    registry.register(
        _name, "sqlalchemy_openobserve.dialect", "OpenObserveDialect"
    )
