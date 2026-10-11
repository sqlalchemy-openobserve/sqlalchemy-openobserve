"""Prepare a SQL statement before it is sent to OpenObserve"""

from typing import Any


def render(operation: str, parameters: Any = None) -> str:
    """
    Substitute bind parameters into the statement as SQL literals.

    OpenObserve has no server-side bind parameters.
    """
    raise NotImplementedError


def is_read_only(sql: str) -> bool:
    """Return True when the statement only reads data."""
    raise NotImplementedError
