"""Prepare and inspect SQL statements sent to OpenObserve."""

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlglot import exp, parse
from sqlglot.errors import SqlglotError


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


def _parse_one(sql: str) -> exp.Expression | None:
    try:
        statements = parse(sql)
    except SqlglotError:
        return None
    if len(statements) != 1:
        return None
    return statements[0]


def is_read_only(sql: str) -> bool:
    """Return whether ``sql`` is one read-only query statement."""
    statement = _parse_one(sql)
    if isinstance(statement, exp.Query):
        return not any(
            isinstance(node, (exp.DDL, exp.DML))
            for node in statement.walk()
        )
    if not isinstance(statement, exp.Command):
        return False

    command = str(statement.this).upper()
    if command == "SHOW":
        return True
    if command == "EXPLAIN" and isinstance(statement.expression, exp.Literal):
        return is_read_only(statement.expression.this)
    return False


def _identifier_name(identifier: exp.Identifier) -> str:
    if identifier.args.get("quoted"):
        return identifier.name
    return identifier.name.lower()


def _table_name(table: exp.Table) -> str:
    return ".".join(
        _identifier_name(part)
        for part in table.parts
        if isinstance(part, exp.Identifier)
    )


def _stream_names(
    query: exp.Expression,
    inherited_ctes: dict[str, exp.Expression] | None = None,
    resolving: frozenset[str] = frozenset(),
) -> set[str] | None:
    ctes = dict(inherited_ctes or {})
    if isinstance(query, exp.Query):
        ctes.update({cte.alias.lower(): cte.this for cte in query.ctes})

    sources: tuple[exp.Expression, ...]
    if isinstance(query, exp.SetOperation):
        sources = (query.this, query.expression)
    elif isinstance(query, exp.Select):
        from_ = query.args.get("from_")
        sources = tuple(
            source
            for source in (
                from_.this if isinstance(from_, exp.From) else None,
                *(join.this for join in query.args.get("joins") or []),
            )
            if isinstance(source, exp.Expression)
        )
    elif isinstance(query, exp.Subquery):
        return _stream_names(query.this, ctes, resolving)
    else:
        return None

    names: set[str] = set()
    for source in sources:
        if isinstance(source, exp.Table):
            name = _table_name(source)
            cte_name = source.name.lower() if len(source.parts) == 1 else ""
            if cte_name in ctes:
                if cte_name in resolving:
                    return None
                resolved = _stream_names(
                    ctes[cte_name], ctes, resolving | {cte_name}
                )
                if resolved is None:
                    return None
                names.update(resolved)
            elif name:
                names.add(name)
            else:
                return None
        elif isinstance(source, exp.Subquery):
            return None
        else:
            return None
    return names


def stream_ref(sql: str) -> str | None:
    """Return the single real stream reachable from a query's sources."""
    statement = _parse_one(sql)
    if not isinstance(statement, exp.Query):
        return None
    names = _stream_names(statement)
    return next(iter(names)) if names is not None and len(names) == 1 else None


def selects_timestamp(sql: str) -> bool:
    """Return whether the outer query outputs raw ``_timestamp`` or ``*``."""
    statement = _parse_one(sql)
    if isinstance(statement, exp.SetOperation):
        statement = statement.this
    if not isinstance(statement, exp.Select):
        return False

    for projection in statement.expressions:
        if isinstance(projection, exp.Star):
            return True
        if isinstance(projection, exp.Column) and isinstance(
            projection.this, exp.Star
        ):
            return True

        output_name = projection.alias_or_name.lower()
        value = (
            projection.this
            if isinstance(projection, exp.Alias)
            else projection
        )
        if (
            isinstance(value, exp.Column)
            and not isinstance(value.this, exp.Star)
            and value.name.lower() == "_timestamp"
            and output_name == "_timestamp"
        ):
            return True
    return False
