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
            isinstance(node, (exp.DDL, exp.DML)) for node in statement.walk()
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
        ctes.update(
            {
                _identifier_name(cte.args["alias"].this): cte.this
                for cte in query.ctes
            }
        )

    if isinstance(query, exp.Subquery):
        return _stream_names(query.this, ctes, resolving)
    if not isinstance(query, exp.Select):
        return None

    from_ = query.args.get("from_")
    sources = (
        from_.this if isinstance(from_, exp.From) else None,
        *(join.this for join in query.args.get("joins") or []),
    )

    names: set[str] = set()
    for source in sources:
        if source is None:
            continue
        if not isinstance(source, exp.Table):
            return None
        table = source.this
        cte_name = (
            _identifier_name(table)
            if len(source.parts) == 1 and isinstance(table, exp.Identifier)
            else ""
        )
        if cte_name in ctes:
            if cte_name in resolving:
                return None
            resolved = _stream_names(
                ctes[cte_name], ctes, resolving | {cte_name}
            )
            if resolved is None:
                return None
            names.update(resolved)
            continue
        name = _table_name(source)
        if not name:
            return None
        names.add(name)
    return names


def stream_ref(sql: str) -> str | None:
    """Return the single real stream a query reads in its FROM and JOINs.

    Subqueries in WHERE or the select list are not followed, and set
    operations return None.
    """
    statement = _parse_one(sql)
    if not isinstance(statement, exp.Query):
        return None
    names = _stream_names(statement)
    return next(iter(names)) if names is not None and len(names) == 1 else None


def selects_timestamp(sql: str) -> bool:
    """Return whether the outer query outputs raw ``_timestamp`` or ``*``.

    A set operation is judged by its leftmost query, which names its columns.
    Renaming it hides the column, so ``SELECT _timestamp AS ts`` is False.
    """
    statement = _parse_one(sql)
    while isinstance(statement, (exp.SetOperation, exp.Subquery)):
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


def _outer_select(sql: str) -> exp.Select | None:
    statement = _parse_one(sql)
    while isinstance(statement, (exp.SetOperation, exp.Subquery)):
        statement = statement.this
    return statement if isinstance(statement, exp.Select) else None


def _is_star(projection: exp.Expression) -> bool:
    return isinstance(projection, exp.Star) or (
        isinstance(projection, exp.Column)
        and isinstance(projection.this, exp.Star)
    )


def output_columns(sql: str, stream_fields: list[str]) -> list[str] | None:
    """Return the column names the outer SELECT outputs, or None if unknown.

    The server names no columns for a result with no rows. ``*`` expands only
    over a stream read directly, not a CTE, subquery or join. Unnamed
    expressions are named by their SQL text, which the server may spell
    differently (``max(logs.status)``).
    """
    statement = _outer_select(sql)
    if statement is None:
        return None

    names: list[str] = []
    for projection in statement.expressions:
        if _is_star(projection):
            source = statement.args.get("from_")
            if (
                statement.args.get("with_")
                or statement.args.get("joins")
                or source is None
                or not isinstance(source.this, exp.Table)
            ):
                return None
            names.extend(stream_fields)
        elif isinstance(projection, exp.Alias):
            alias = projection.args["alias"]
            names.append(alias.name if alias.quoted else alias.name.lower())
        elif isinstance(projection, exp.Column):
            names.append(projection.name)
        else:
            names.append(projection.sql(normalize_functions="lower"))
    return names


def plain_columns(sql: str) -> set[str] | None:
    """Return the fields the outer SELECT passes through unchanged.

    None means ``*``. A column renamed or computed is left out, so its output
    name is not mistaken for the stream field of that name, and so is
    everything under a CTE, which can rename fields.
    """
    statement = _outer_select(sql)
    if statement is None or statement.args.get("with_"):
        return set()

    names: set[str] = set()
    for projection in statement.expressions:
        if _is_star(projection):
            return None
        value = (
            projection.this
            if isinstance(projection, exp.Alias)
            else projection
        )
        if isinstance(value, exp.Column) and (
            projection is value or projection.alias == value.name
        ):
            names.add(value.name)
    return names
