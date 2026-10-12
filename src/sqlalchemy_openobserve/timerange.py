"""Work out the time range of a query"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

_MARGIN_US = 3_600_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _delta_us(value: datetime) -> int:
    delta = value - _EPOCH
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


# Bare epoch integers are guessed from their digit count; anything outside
# this window is treated as an ordinary number.
_EPOCH_MIN_US = _delta_us(datetime(1990, 1, 1, tzinfo=timezone.utc))
_EPOCH_MAX_US = _delta_us(datetime(2100, 1, 1, tzinfo=timezone.utc))

# Months and years have no fixed length, so the longest one is used: it
# keeps interval arithmetic from cutting into the real window.
_INTERVAL_US: dict[str, int] = {
    "microsecond": 1,
    "microseconds": 1,
    "us": 1,
    "millisecond": 1_000,
    "milliseconds": 1_000,
    "ms": 1_000,
    "second": 1_000_000,
    "seconds": 1_000_000,
    "sec": 1_000_000,
    "secs": 1_000_000,
    "s": 1_000_000,
    "minute": 60_000_000,
    "minutes": 60_000_000,
    "min": 60_000_000,
    "mins": 60_000_000,
    "hour": 3_600_000_000,
    "hours": 3_600_000_000,
    "hr": 3_600_000_000,
    "hrs": 3_600_000_000,
    "day": 86_400_000_000,
    "days": 86_400_000_000,
    "d": 86_400_000_000,
    "week": 604_800_000_000,
    "weeks": 604_800_000_000,
    "w": 604_800_000_000,
    "month": 31 * 86_400_000_000,
    "months": 31 * 86_400_000_000,
    "year": 366 * 86_400_000_000,
    "years": 366 * 86_400_000_000,
    "y": 366 * 86_400_000_000,
    "yr": 366 * 86_400_000_000,
    "yrs": 366 * 86_400_000_000,
}

_TWO_CHAR_OPS = (">=", "<=", "<>", "!=", "::", "||")
_ONE_CHAR_OPS = "!+-*/%=<>(),.;~:"
_NAME_STOP = "'\"`" + _ONE_CHAR_OPS

_COMPARISONS = {">=", "<=", "<>", "!=", ">", "<", "="}
_FLIP = {
    ">=": "<=",
    "<=": ">=",
    ">": "<",
    "<": ">",
    "=": "=",
    "<>": "<>",
    "!=": "!=",
}

_NOW_NAMES = ("NOW", "CURRENT_TIMESTAMP")

# Only the WHERE clauses of the top-level statement are analysed: time
# filters in subqueries, JOIN ON clauses and HAVING do not bound the
# rows a plain SELECT can match.
_CLAUSE_STOPS = frozenset(
    {
        "GROUP",
        "ORDER",
        "LIMIT",
        "OFFSET",
        "HAVING",
        "UNION",
        "EXCEPT",
        "INTERSECT",
        "WINDOW",
        "QUALIFY",
        "SELECT",
        "FROM",
        "WITH",
        "FOR",
        "JOIN",
        "ON",
        "USING",
        "WHERE",
        "CROSS",
        "INNER",
        "LEFT",
        "RIGHT",
        "FULL",
        "NATURAL",
        "VALUES",
        "SET",
        "INTO",
        "RETURNING",
    }
)


@dataclass
class _Token:
    kind: str  # "name", "string", "number" or "op"
    text: str
    quoted: bool = False


def _kw(token: _Token) -> str:
    if token.kind == "name" and not token.quoted:
        return token.text.upper()
    return ""


def _scan_quoted(sql: str, i: int, quote: str) -> tuple[str, int]:
    parts: list[str] = []
    j = i + 1
    n = len(sql)
    while j < n:
        if sql[j] == quote:
            if j + 1 < n and sql[j + 1] == quote:
                parts.append(quote)
                j += 2
                continue
            return "".join(parts), j + 1
        parts.append(sql[j])
        j += 1
    return "".join(parts), n


def _tokenize(sql: str) -> list[_Token]:
    tokens: list[_Token] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i + 2)
            i = n if j < 0 else j + 1
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif ch == "'":
            text, i = _scan_quoted(sql, i, ch)
            tokens.append(_Token("string", text))
        elif ch in ('"', "`"):
            text, i = _scan_quoted(sql, i, ch)
            tokens.append(_Token("name", text, quoted=True))
        elif ch == "%" and sql.startswith("%(", i):
            # An unrendered bind parameter; drop it so the comparison
            # stays unresolved rather than misreading it.
            j = sql.find(")s", i + 2)
            i = n if j < 0 else j + 2
        elif ch.isdigit():
            j = i
            while (
                j < n
                and (
                    sql[j].isdigit()
                    or sql[j] == "."
                    or sql[j] in "eE"
                    or (sql[j] in "+-" and sql[j - 1] in "eE")
                )
            ):
                j += 1
            tokens.append(_Token("number", sql[i:j]))
            i = j
        elif sql[i : i + 2] in _TWO_CHAR_OPS:
            tokens.append(_Token("op", sql[i : i + 2]))
            i += 2
        elif ch in _ONE_CHAR_OPS:
            tokens.append(_Token("op", ch))
            i += 1
        else:
            j = i
            while j < n and not sql[j].isspace() and sql[j] not in _NAME_STOP:
                j += 1
            tokens.append(_Token("name", sql[i:j]))
            i = j
    return tokens


def _datetime_us(text: str) -> int | None:
    # Python 3.11 parses most ISO 8601: space or "T" separator, "Z" and
    # numeric offsets; naive strings are read as UTC.
    value = re.sub(r"(\.\d{6})\d+", r"\g<1>", text.strip())
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _delta_us(parsed)


def _now_us() -> int:
    return _delta_us(datetime.now(timezone.utc))


def _interval_us(text: str) -> int | None:
    pairs = re.findall(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)", text)
    if not pairs:
        return None
    total = 0
    for value, unit in pairs:
        factor = _INTERVAL_US.get(unit.lower())
        if factor is None:
            return None
        total += int(float(value) * factor)
    return total


def _time_value(tokens: list[_Token]) -> int | None:
    if not tokens:
        return None
    if (
        len(tokens) >= 4
        and tokens[-1].kind == "string"
        and _kw(tokens[-2]) == "INTERVAL"
        and tokens[-3].kind == "op"
        and tokens[-3].text in ("+", "-")
    ):
        base = _time_value(tokens[:-3])
        span = _interval_us(tokens[-1].text)
        if base is None or span is None:
            return None
        if tokens[-3].text == "+":
            return base + span
        return base - span
    return _base_time_value(tokens)


def _base_time_value(tokens: list[_Token]) -> int | None:
    if len(tokens) == 1 and tokens[0].kind == "string":
        return _datetime_us(tokens[0].text)
    if (
        len(tokens) == 2
        and tokens[1].kind == "string"
        and _kw(tokens[0]) in ("TIMESTAMP", "DATE")
    ):
        return _datetime_us(tokens[1].text)
    if len(tokens) == 1 and _kw(tokens[0]) == "CURRENT_TIMESTAMP":
        return _now_us()
    if (
        len(tokens) == 3
        and _kw(tokens[0]) in _NOW_NAMES
        and tokens[1].kind == "op"
        and tokens[1].text == "("
        and tokens[2].kind == "op"
        and tokens[2].text == ")"
    ):
        return _now_us()
    cast = _cast_us(tokens)
    if cast is not None:
        return cast
    return _to_timestamp_us(tokens)


def _cast_us(tokens: list[_Token]) -> int | None:
    if (
        len(tokens) >= 6
        and _kw(tokens[0]) == "CAST"
        and tokens[1].kind == "op"
        and tokens[1].text == "("
        and tokens[-1].kind == "op"
        and tokens[-1].text == ")"
    ):
        inner = tokens[2:-1]
        if (
            len(inner) >= 3
            and inner[0].kind == "string"
            and _kw(inner[1]) == "AS"
            and inner[2].kind == "name"
        ):
            typ = inner[2].text.upper()
            rest = inner[3:]
            if rest == [] or (
                len(rest) == 3
                and rest[0].kind == "op"
                and rest[0].text == "("
                and rest[1].kind == "number"
                and rest[2].kind == "op"
                and rest[2].text == ")"
            ):
                if typ.startswith("TIMESTAMP") or typ == "DATE":
                    return _datetime_us(inner[0].text)
    return None


def _to_timestamp_us(tokens: list[_Token]) -> int | None:
    if (
        len(tokens) >= 4
        and tokens[0].kind == "name"
        and not tokens[0].quoted
        and tokens[1].kind == "op"
        and tokens[1].text == "("
        and tokens[-1].kind == "op"
        and tokens[-1].text == ")"
    ):
        name = tokens[0].text.lower()
        if name == "to_timestamp" or name.startswith("to_timestamp_"):
            args = _split_commas(tokens[2:-1])
            if len(args) <= 2 and len(args[0]) == 1:
                if args[0][0].kind == "string":
                    return _datetime_us(args[0][0].text)
    return None


def _epoch_int(tokens: list[_Token]) -> int | None:
    if len(tokens) == 1 and tokens[0].kind == "number":
        if tokens[0].text.isdigit():
            return int(tokens[0].text)
    return None


def _scale_epoch(value: int) -> int | None:
    digits = len(str(value))
    if digits == 10:
        scaled = value * 1_000_000
    elif digits == 13:
        scaled = value * 1_000
    elif digits == 16:
        scaled = value
    elif digits == 19:
        scaled = value // 1_000
    else:
        return None
    if _EPOCH_MIN_US <= scaled <= _EPOCH_MAX_US:
        return scaled
    return None


def _timeish(tokens: list[_Token]) -> bool:
    parts: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i].kind != "name":
            return False
        parts.append(tokens[i].text)
        i += 1
        if i < len(tokens):
            if tokens[i].kind == "op" and tokens[i].text == ".":
                i += 1
            else:
                return False
    if not parts:
        return False
    name = parts[-1].lower()
    return "time" in name or "date" in name or name in ("ts", "dttm")


def _bounds(op: str, value: int) -> tuple[int | None, int | None]:
    # Bounds are inclusive below and exclusive above, like the filter
    # OpenObserve itself applies.
    if op == ">=":
        return value, None
    if op == ">":
        return value + 1, None
    if op == "<=":
        return None, value + 1
    if op == "<":
        return None, value
    if op == "=":
        return value, value + 1
    return None, None


def _and_intersect(
    a: tuple[int | None, int | None],
    b: tuple[int | None, int | None],
) -> tuple[int | None, int | None]:
    lows = [x for x in (a[0], b[0]) if x is not None]
    highs = [x for x in (a[1], b[1]) if x is not None]
    return (max(lows) if lows else None, min(highs) if highs else None)


def _or_all(
    intervals: list[tuple[int | None, int | None]],
) -> tuple[int | None, int | None]:
    # The widest window that covers the union of the intervals, where
    # an open bound on any side opens that side of the result.
    if not intervals:
        return None, None
    lows = [x[0] for x in intervals if x[0] is not None]
    highs = [x[1] for x in intervals if x[1] is not None]
    low = min(lows) if len(lows) == len(intervals) else None
    high = max(highs) if len(highs) == len(intervals) else None
    return low, high


def _complement(
    interval: tuple[int | None, int | None],
) -> tuple[int | None, int | None]:
    low, high = interval
    if low is None:
        return high, None
    if high is None:
        return None, low
    return None, None


def _split_commas(tokens: list[_Token]) -> list[list[_Token]]:
    parts: list[list[_Token]] = []
    current: list[_Token] = []
    depth = 0
    for token in tokens:
        if token.kind == "op":
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
            elif token.text == "," and depth == 0:
                parts.append(current)
                current = []
                continue
        current.append(token)
    parts.append(current)
    return parts


def _split_keyword(
    tokens: list[_Token], keyword: str
) -> list[list[_Token]]:
    # An AND directly after BETWEEN belongs to BETWEEN, not to the
    # boolean expression.
    parts: list[list[_Token]] = []
    current: list[_Token] = []
    depth = 0
    between = False
    for token in tokens:
        if token.kind == "op":
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
            current.append(token)
            continue
        if depth == 0 and _kw(token) == keyword:
            if keyword == "AND" and between:
                between = False
                current.append(token)
                continue
            parts.append(current)
            current = []
            continue
        if depth == 0 and _kw(token) == "BETWEEN":
            between = True
        current.append(token)
    parts.append(current)
    return parts


def _is_wrapped(tokens: list[_Token]) -> bool:
    if len(tokens) < 2:
        return False
    depth = 0
    for i, token in enumerate(tokens):
        if token.kind == "op":
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
                if depth == 0 and i != len(tokens) - 1:
                    return False
    return depth == 0


def _parse_bool(tokens: list[_Token]) -> tuple[int | None, int | None]:
    if not tokens:
        return None, None
    or_parts = _split_keyword(tokens, "OR")
    if len(or_parts) > 1:
        return _or_all([_parse_bool(part) for part in or_parts])
    and_parts = _split_keyword(tokens, "AND")
    if len(and_parts) > 1:
        result: tuple[int | None, int | None] = (None, None)
        for part in and_parts:
            result = _and_intersect(result, _parse_bool(part))
        return result
    if _kw(tokens[0]) == "NOT" and len(tokens) > 1:
        return _complement(_parse_bool(tokens[1:]))
    if (
        tokens[0].kind == "op"
        and tokens[0].text == "("
        and _is_wrapped(tokens)
    ):
        inner = tokens[1:-1]
        if inner and _kw(inner[0]) == "SELECT":
            return None, None
        return _parse_bool(inner)
    return _predicate(tokens)


def _predicate(tokens: list[_Token]) -> tuple[int | None, int | None]:
    cmp_at = -1
    depth = 0
    for i, token in enumerate(tokens):
        if token.kind == "op":
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
            elif depth == 0 and token.text in _COMPARISONS:
                if cmp_at >= 0:
                    return None, None
                cmp_at = i
    if cmp_at >= 0:
        return _comparison(
            tokens[:cmp_at], tokens[cmp_at].text, tokens[cmp_at + 1 :]
        )
    return _set_predicate(tokens)


def _comparison(
    lhs: list[_Token], op: str, rhs: list[_Token]
) -> tuple[int | None, int | None]:
    left = _time_value(lhs)
    right = _time_value(rhs)
    if left is not None and right is None:
        return _bounds(_FLIP[op], left)
    if right is not None and left is None:
        return _bounds(op, right)
    left_int = _epoch_int(lhs)
    right_int = _epoch_int(rhs)
    if left_int is not None and _timeish(rhs):
        scaled = _scale_epoch(left_int)
        if scaled is not None:
            return _bounds(_FLIP[op], scaled)
    if right_int is not None and _timeish(lhs):
        scaled = _scale_epoch(right_int)
        if scaled is not None:
            return _bounds(op, scaled)
    return None, None


def _side_value(
    tokens: list[_Token], operand: list[_Token]
) -> int | None:
    value = _time_value(tokens)
    if value is not None:
        return value
    number = _epoch_int(tokens)
    if number is not None and _timeish(operand):
        return _scale_epoch(number)
    return None


def _set_predicate(tokens: list[_Token]) -> tuple[int | None, int | None]:
    depth = 0
    for i, token in enumerate(tokens):
        if token.kind == "op":
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
            continue
        if depth != 0:
            continue
        keyword = _kw(token)
        if keyword not in ("IN", "BETWEEN"):
            continue
        negated = i > 0 and _kw(tokens[i - 1]) == "NOT"
        operand = tokens[: i - 1 if negated else i]
        if keyword == "BETWEEN":
            parts = _split_keyword(tokens[i + 1 :], "AND")
            if len(parts) != 2:
                return None, None
            low = _side_value(parts[0], operand)
            high = _side_value(parts[1], operand)
            if low is None or high is None:
                return None, None
            interval = (low, high + 1)
            return _complement(interval) if negated else interval
        group = tokens[i + 1 :]
        if (
            len(group) >= 2
            and group[0].kind == "op"
            and group[0].text == "("
            and group[-1].kind == "op"
            and group[-1].text == ")"
            and _is_wrapped(group)
        ):
            values: list[int] = []
            complete = True
            for item in _split_commas(group[1:-1]):
                value = _side_value(item, operand)
                if value is None:
                    complete = False
                    break
                values.append(value)
            if complete and values:
                interval = (min(values), max(values) + 1)
                return _complement(interval) if negated else interval
        return None, None
    return None, None


def _where_ranges(tokens: list[_Token]) -> list[list[_Token]]:
    ranges: list[list[_Token]] = []
    start: int | None = None
    depth = 0
    for i, token in enumerate(tokens):
        if token.kind == "op":
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
            elif token.text == ";" and depth == 0:
                if start is not None:
                    ranges.append(tokens[start:i])
                    start = None
            continue
        if depth != 0:
            continue
        keyword = _kw(token)
        if start is not None:
            if keyword in _CLAUSE_STOPS:
                ranges.append(tokens[start:i])
                start = None
        elif keyword == "WHERE":
            start = i + 1
    if start is not None:
        ranges.append(tokens[start:])
    return ranges


def _analyze(sql: str) -> tuple[int | None, int | None]:
    try:
        intervals = [
            _parse_bool(where) for where in _where_ranges(_tokenize(sql))
        ]
    except Exception:
        # Unparseable SQL falls back to the widest range, which is
        # always correct, only slow.
        return None, None
    return _or_all(intervals)


def extract_time_range(sql: str) -> tuple[int, int]:
    """
    Return the (start, end) time range to send with a query, in
    microseconds since the epoch.

    OpenObserve needs these outside the SQL on every search, and applies
    them itself as ``_timestamp >= start AND _timestamp < end``, on top
    of the SQL. The range therefore only has to cover the rows the SQL
    can match: wider is always safe, narrower loses rows.

    Start and end are worked out from the time comparisons in the
    statement's WHERE clause, with AND keeping the narrowest window, OR
    the widest, and NOT turning a one-sided bound into the other side.
    A comparison that cannot be resolved leaves that side of the range
    open: start falls back to just after the epoch (the API rejects
    zero) and end to now plus an hour.

    A timestamp is any ISO 8601 literal string, with or without CAST,
    to_timestamp or TIMESTAMP/DATE typing, and ``now()`` plus or minus an
    INTERVAL. A bare integer is read as an epoch in seconds,
    milliseconds, microseconds or nanoseconds by its digit count, and
    only when it is compared against a column with a time-like name.
    """
    low, high = _analyze(sql)
    if low is None or low < 1:
        low = 1
    if high is None:
        high = _now_us() + _MARGIN_US
    if high <= low:
        high = low + 1
    return low, high
