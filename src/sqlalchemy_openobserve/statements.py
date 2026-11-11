"""Prepare and inspect SQL statements sent to OpenObserve."""

import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

_WRITE_WORDS = frozenset(
    {
        "ALTER",
        "ATTACH",
        "CALL",
        "COPY",
        "CREATE",
        "DELETE",
        "DETACH",
        "DROP",
        "EXEC",
        "EXECUTE",
        "GRANT",
        "INSERT",
        "MERGE",
        "REPLACE",
        "REVOKE",
        "TRUNCATE",
        "UPDATE",
        "UPSERT",
    }
)
_QUERY_WORDS = frozenset({"EXPLAIN", "SELECT", "SHOW", "WITH"})
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, Decimal)):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers cannot be SQL literals")
        return str(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "X'" + bytes(value).hex() + "'"
    if isinstance(value, (datetime, date, time)):
        return _quote(value.isoformat())
    if isinstance(value, UUID):
        return _quote(str(value))
    raise TypeError(
        f"unsupported SQL bind parameter type: {type(value).__name__}"
    )


def render(operation: str, parameters: Any = None) -> str:
    """Substitute ``pyformat`` bind parameters as escaped SQL literals."""
    if parameters is None:
        return operation
    mapping = parameters if isinstance(parameters, Mapping) else None
    sequence: Sequence[Any] | None = None
    if mapping is None:
        if isinstance(parameters, Sequence) and not isinstance(
            parameters, (str, bytes, bytearray, memoryview)
        ):
            sequence = parameters
        else:
            sequence = (parameters,)

    result: list[str] = []
    index = 0
    i = 0
    while i < len(operation):
        if operation[i] != "%":
            result.append(operation[i])
            i += 1
            continue
        if i + 1 < len(operation) and operation[i + 1] == "%":
            result.append("%")
            i += 2
            continue
        if i + 1 < len(operation) and operation[i + 1] == "s":
            if sequence is None or index >= len(sequence):
                raise TypeError("not enough positional bind parameters")
            result.append(_literal(sequence[index]))
            index += 1
            i += 2
            continue
        if i + 1 < len(operation) and operation[i + 1] == "(":
            end = operation.find(")s", i + 2)
            if end < 0:
                raise ValueError("invalid pyformat bind parameter")
            name = operation[i + 2 : end]
            if not name or mapping is None:
                raise TypeError("named binds require a parameter mapping")
            try:
                value = mapping[name]
            except KeyError as exc:
                raise KeyError(f"missing bind parameter: {name}") from exc
            result.append(_literal(value))
            i = end + 2
            continue
        raise ValueError("invalid percent marker in SQL statement")

    if sequence is not None and index != len(sequence):
        raise TypeError("too many positional bind parameters")
    return "".join(result)


def _tokens(sql: str) -> list[tuple[str, str]]:
    """Return SQL tokens while discarding comments and string contents."""
    tokens: list[tuple[str, str]] = []
    i = 0
    while i < len(sql):
        if sql[i].isspace():
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            i = len(sql) if end < 0 else end + 1
        elif sql.startswith("/*", i):
            depth = 1
            i += 2
            while i < len(sql) and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                tokens.append(("invalid", "unterminated comment"))
        elif sql[i] == "'":
            i += 1
            closed = False
            while i < len(sql):
                if sql[i] == "'":
                    if i + 1 < len(sql) and sql[i + 1] == "'":
                        i += 2
                    else:
                        i += 1
                        closed = True
                        break
                else:
                    i += 1
            if not closed:
                tokens.append(("invalid", "unterminated string"))
            tokens.append(("string", ""))
        elif sql[i] in ('"', "`"):
            quote = sql[i]
            i += 1
            parts: list[str] = []
            closed = False
            while i < len(sql):
                if sql[i] == quote:
                    if i + 1 < len(sql) and sql[i + 1] == quote:
                        parts.append(quote)
                        i += 2
                    else:
                        i += 1
                        closed = True
                        break
                else:
                    parts.append(sql[i])
                    i += 1
            if not closed:
                tokens.append(("invalid", "unterminated identifier"))
            tokens.append(("quoted", "".join(parts)))
        else:
            match = _NAME.match(sql, i)
            if match:
                tokens.append(("word", match.group().upper()))
                i = match.end()
            else:
                tokens.append(("symbol", sql[i]))
                i += 1
    return tokens


def is_read_only(sql: str) -> bool:
    """Return whether ``sql`` is one read-only query statement."""
    tokens = _tokens(sql)
    if not tokens or any(kind == "invalid" for kind, _ in tokens):
        return False
    words = [value for kind, value in tokens if kind == "word"]
    if not words or words[0] not in _QUERY_WORDS:
        return False
    if any(word in _WRITE_WORDS for word in words):
        return False
    semicolons = [
        i for i, token in enumerate(tokens) if token == ("symbol", ";")
    ]
    return not semicolons or semicolons == [len(tokens) - 1]


def stream_ref(sql: str) -> str | None:
    """Return the first top-level stream named by a query's ``FROM``."""
    tokens = _tokens(sql)
    depth = 0
    for i, (kind, value) in enumerate(tokens):
        if (kind, value) == ("symbol", "("):
            depth += 1
        elif (kind, value) == ("symbol", ")"):
            depth = max(depth - 1, 0)
        elif depth == 0 and kind == "word" and value == "FROM":
            if i + 1 >= len(tokens):
                return None
            next_kind, next_value = tokens[i + 1]
            if (next_kind, next_value) == ("symbol", "("):
                return None
            if next_kind not in {"word", "quoted"}:
                return None
            parts = [
                next_value if next_kind == "quoted" else next_value.lower()
            ]
            j = i + 2
            while j + 1 < len(tokens) and tokens[j] == ("symbol", "."):
                part_kind, part_value = tokens[j + 1]
                if part_kind not in {"word", "quoted"}:
                    break
                parts.append(
                    part_value if part_kind == "quoted" else part_value.lower()
                )
                j += 2
            return ".".join(parts)
    return None


def selects_timestamp(sql: str) -> bool:
    """Return whether the outer query selects ``_timestamp`` or a wildcard."""
    tokens = _tokens(sql)
    depth = 0
    select: int | None = None
    for i, (kind, value) in enumerate(tokens):
        if (kind, value) == ("symbol", "("):
            depth += 1
        elif (kind, value) == ("symbol", ")"):
            depth = max(depth - 1, 0)
        elif depth == 0 and kind == "word" and value == "SELECT":
            select = i
            break
    if select is None:
        return False

    expressions: list[list[tuple[str, str]]] = [[]]
    depth = 0
    for kind, value in tokens[select + 1 :]:
        if (kind, value) == ("symbol", "("):
            depth += 1
        elif (kind, value) == ("symbol", ")"):
            depth = max(depth - 1, 0)
        elif depth == 0 and kind == "word" and value == "FROM":
            break
        elif depth == 0 and (kind, value) == ("symbol", ","):
            expressions.append([])
        else:
            expressions[-1].append((kind, value))

    for expression in expressions:
        if expression == [("symbol", "*")]:
            return True
        if len(expression) >= 2 and expression[-2:] == [
            ("symbol", "."),
            ("symbol", "*"),
        ]:
            return True
        if len(expression) == 1:
            kind, value = expression[0]
            if kind in {"word", "quoted"} and value.lower() == "_timestamp":
                return True
        if len(expression) == 3:
            qualifier, dot, column = expression
            if (
                qualifier[0] in {"word", "quoted"}
                and dot == ("symbol", ".")
                and column[0] in {"word", "quoted"}
                and column[1].lower() == "_timestamp"
            ):
                return True
    return False
