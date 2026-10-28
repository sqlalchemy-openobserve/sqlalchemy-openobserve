"""Work out the time range of a query"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_HOUR_US = 3_600_000_000
_DAY_US = 86_400_000_000
# OpenObserve ingests nothing stamped further ahead than 24 hours, so
# no row can lie further ahead of now than that; the slack covers
# clock skew between the client and the server.
_FUTURE_MARGIN_US = 25 * _HOUR_US
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _delta_us(value: datetime) -> int:
    delta = value - _EPOCH
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


# A bare integer only counts as an epoch inside this window; outside
# it is an ordinary number, and bounds nothing.
_EPOCH_MIN_US = _delta_us(datetime(1990, 1, 1, tzinfo=timezone.utc))
_EPOCH_MAX_US = _delta_us(datetime(2100, 1, 1, tzinfo=timezone.utc))

# Months and years have no fixed length, so each unit carries its
# shortest and its longest: the shortest goes into a lower bound and
# the longest into an upper one, each widened whichever way cannot
# cut into the real window. The fixed units carry one length twice.
_INTERVAL_US: dict[str, tuple[int, int]] = {
    "microsecond": (1, 1),
    "microseconds": (1, 1),
    "us": (1, 1),
    "millisecond": (1_000, 1_000),
    "milliseconds": (1_000, 1_000),
    "ms": (1_000, 1_000),
    "second": (1_000_000, 1_000_000),
    "seconds": (1_000_000, 1_000_000),
    "sec": (1_000_000, 1_000_000),
    "secs": (1_000_000, 1_000_000),
    "s": (1_000_000, 1_000_000),
    "minute": (60_000_000, 60_000_000),
    "minutes": (60_000_000, 60_000_000),
    "min": (60_000_000, 60_000_000),
    "mins": (60_000_000, 60_000_000),
    "hour": (_HOUR_US, _HOUR_US),
    "hours": (_HOUR_US, _HOUR_US),
    "hr": (_HOUR_US, _HOUR_US),
    "hrs": (_HOUR_US, _HOUR_US),
    "day": (_DAY_US, _DAY_US),
    "days": (_DAY_US, _DAY_US),
    "d": (_DAY_US, _DAY_US),
    "week": (7 * _DAY_US, 7 * _DAY_US),
    "weeks": (7 * _DAY_US, 7 * _DAY_US),
    "w": (7 * _DAY_US, 7 * _DAY_US),
    "month": (28 * _DAY_US, 31 * _DAY_US),
    "months": (28 * _DAY_US, 31 * _DAY_US),
    "year": (365 * _DAY_US, 366 * _DAY_US),
    "years": (365 * _DAY_US, 366 * _DAY_US),
    "y": (365 * _DAY_US, 366 * _DAY_US),
    "yr": (365 * _DAY_US, 366 * _DAY_US),
    "yrs": (365 * _DAY_US, 366 * _DAY_US),
}

# An INTERVAL string, one <number> <unit> pair at a time.
_INTERVAL_PAIR = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)(?:\s+|$)")

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
_INVERT = {
    ">=": "<",
    ">": "<=",
    "<": ">=",
    "<=": ">",
    "=": "!=",
    "!=": "=",
    "<>": "=",
}

# Set operators combine the branches of a statement.
_COMPOUND_OPS = frozenset({"UNION", "EXCEPT", "INTERSECT"})

_NOW_NAMES = ("NOW", "CURRENT_TIMESTAMP")

# A day-granular literal, like '2024-01-01', covers its whole day
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}|\d{8}")


@dataclass
class _Token:
    kind: str  # "name", "string", "number" or "op"
    text: str
    quoted: bool = False


@dataclass
class _TimeValue:
    us: int  # microseconds since the epoch
    date_only: bool  # a day-granular literal, like '2024-01-01'


def _kw(token: _Token | None) -> str:
    if token is not None and token.kind == "name" and not token.quoted:
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
            while j < n and (
                sql[j].isdigit()
                or sql[j] == "."
                or sql[j] in "eE"
                or (sql[j] in "+-" and sql[j - 1] in "eE")
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


def _iso_datetime(text: str, side: str) -> datetime | None:
    # Python 3.11 parses most ISO 8601: space or "T" separator, "Z"
    # and numeric offsets; naive strings are read as UTC. Python
    # also keeps 6 fractional digits, so a lower bound truncates
    # the rest away and an upper one rounds up onto the next
    # microsecond, each widened whichever way cannot cut into the
    # real window.
    value = text.strip()
    bump = False
    fraction = re.search(r"\.(\d+)", value)
    if fraction is not None and len(fraction.group(1)) > 6:
        if side == "high" and fraction.group(1)[6:].strip("0"):
            bump = True
        value = value[: fraction.start() + 7]
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if bump:
        parsed += timedelta(microseconds=1)
    return parsed


def _parse_datetime(text: str, side: str) -> _TimeValue | None:
    parsed = _iso_datetime(text, side)
    if parsed is None:
        return None
    return _TimeValue(
        _delta_us(parsed), _DATE_ONLY.fullmatch(text.strip()) is not None
    )


def _parse_date(text: str) -> _TimeValue | None:
    # A typed DATE gives midnight whatever the time in the string:
    # CAST('2024-01-01 12:00:00' AS DATE) bounds from Jan 1 00:00,
    # not 12:00. The day-granular value covers its whole day on the
    # upper side. A sub-microsecond fraction is truncated away
    # rather than rounded up: rounding '23:59:59.9999999' up would
    # land on the next day.
    parsed = _iso_datetime(text, "low")
    if parsed is None:
        return None
    midnight = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    return _TimeValue(_delta_us(midnight), True)


def _now_us() -> int:
    return _delta_us(datetime.now(timezone.utc))


def _interval_us(text: str, longest: bool) -> int | None:
    # A sign, an `ago` or an `hh:mm:ss` part silently drops out of
    # a findall of the pairs and narrows the bound onto the wrong
    # side: INTERVAL '-1 day' would read as +1 day, and '1 day
    # 12:00:00' as 1 day. A string that holds anything besides
    # <number> <unit> pairs bounds nothing.
    rest = text.strip()
    if not rest:
        return None
    total = 0
    while rest:
        pair = _INTERVAL_PAIR.match(rest)
        if pair is None:
            return None
        span = _INTERVAL_US.get(pair.group(2).lower())
        if span is None:
            return None
        total += int(float(pair.group(1)) * (span[1] if longest else span[0]))
        rest = rest[pair.end() :]
    return total


def _time_value(tokens: list[_Token], side: str) -> _TimeValue | None:
    # A literal plus or minus an INTERVAL. Which length a month or a
    # year takes depends on the side the bound is bound for: the
    # shortest keeps a lower bound under the real one and the longest
    # keeps an upper bound over it, whichever way the INTERVAL goes.
    if not tokens:
        return None
    if (
        len(tokens) >= 4
        and tokens[-1].kind == "string"
        and _kw(tokens[-2]) == "INTERVAL"
        and tokens[-3].kind == "op"
        and tokens[-3].text in ("+", "-")
    ):
        longest = (side == "high") == (tokens[-3].text == "+")
        base = _time_value(tokens[:-3], side)
        span = _interval_us(tokens[-1].text, longest)
        if base is None or span is None:
            return None
        if tokens[-3].text == "+":
            return _TimeValue(base.us + span, False)
        return _TimeValue(base.us - span, False)
    return _base_time_value(tokens, side)


def _base_time_value(tokens: list[_Token], side: str) -> _TimeValue | None:
    if len(tokens) == 1 and tokens[0].kind == "string":
        return _parse_datetime(tokens[0].text, side)
    if (
        len(tokens) == 2
        and tokens[1].kind == "string"
        and _kw(tokens[0]) in ("TIMESTAMP", "DATE")
    ):
        if _kw(tokens[0]) == "DATE":
            return _parse_date(tokens[1].text)
        return _parse_datetime(tokens[1].text, side)
    if len(tokens) == 1 and _kw(tokens[0]) == "CURRENT_TIMESTAMP":
        return _TimeValue(_now_us(), False)
    if (
        len(tokens) == 3
        and _kw(tokens[0]) in _NOW_NAMES
        and tokens[1].kind == "op"
        and tokens[1].text == "("
        and tokens[2].kind == "op"
        and tokens[2].text == ")"
    ):
        return _TimeValue(_now_us(), False)
    cast = _cast_value(tokens, side)
    if cast is not None:
        return cast
    return _to_timestamp_value(tokens, side)


def _cast_value(tokens: list[_Token], side: str) -> _TimeValue | None:
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
                if typ.startswith("TIMESTAMP"):
                    return _parse_datetime(inner[0].text, side)
                if typ == "DATE":
                    return _parse_date(inner[0].text)
    return None


def _to_timestamp_value(tokens: list[_Token], side: str) -> _TimeValue | None:
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
            # A string is read through the format argument, not as
            # ISO 8601, so a format leaves the value unresolved
            # rather than guessed at ISO 8601.
            if len(args) == 1 and len(args[0]) == 1:
                if args[0][0].kind == "string":
                    return _parse_datetime(args[0][0].text, side)
    return None


def _epoch_value(tokens: list[_Token]) -> _TimeValue | None:
    # _timestamp is microseconds, and only a 16-digit integer is one
    # of those. Shorter ones are seconds or milliseconds, which the
    # server compares against as they stand, far tighter than the
    # writer meant, so they bound nothing.
    if len(tokens) == 1 and tokens[0].kind == "number":
        text = tokens[0].text
        if len(text) == 16 and text.isdigit():
            value = int(text)
            if _EPOCH_MIN_US <= value <= _EPOCH_MAX_US:
                return _TimeValue(value, False)
    return None


def _is_timestamp_column(tokens: list[_Token]) -> bool:
    # The window is on _timestamp, so only the column itself, bare or
    # qualified with a table name, bounds it: a filter on any other
    # column, time-like name or not, leaves _timestamp anywhere.
    i = 0
    while True:
        if i >= len(tokens) or tokens[i].kind != "name":
            return False
        if i + 1 == len(tokens):
            return tokens[i].text.lower() == "_timestamp"
        if tokens[i + 1].kind != "op" or tokens[i + 1].text != ".":
            return False
        i += 2


def _literal_value(tokens: list[_Token], side: str) -> _TimeValue | None:
    value = _time_value(tokens, side)
    if value is not None:
        return value
    return _epoch_value(tokens)


def _comparison_bounds(
    op: str, tokens: list[_Token]
) -> tuple[int | None, int | None]:
    # The window bounds of `_timestamp <op> value`. Bounds are
    # inclusive below and exclusive above, like the filter
    # OpenObserve itself applies; a day-granular literal covers its
    # whole day on the upper side.
    if op == ">=":
        value = _literal_value(tokens, "low")
        if value is None:
            return None, None
        return value.us, None
    if op == ">":
        value = _literal_value(tokens, "low")
        if value is None:
            return None, None
        return value.us + 1, None
    if op == "<=":
        value = _literal_value(tokens, "high")
        if value is None:
            return None, None
        return None, value.us + (_DAY_US if value.date_only else 1)
    if op == "<":
        value = _literal_value(tokens, "high")
        if value is None:
            return None, None
        return None, value.us
    if op == "=":
        low = _literal_value(tokens, "low")
        high = _literal_value(tokens, "high")
        if low is None or high is None:
            return None, None
        return low.us, high.us + (_DAY_US if high.date_only else 1)
    return None, None


def _comparison(
    lhs: list[_Token], op: str, rhs: list[_Token]
) -> tuple[int | None, int | None]:
    if _is_timestamp_column(lhs) and not _is_timestamp_column(rhs):
        return _comparison_bounds(op, rhs)
    if _is_timestamp_column(rhs) and not _is_timestamp_column(lhs):
        return _comparison_bounds(_FLIP[op], lhs)
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


def _split_keyword(tokens: list[_Token], keyword: str) -> list[list[_Token]]:
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
    # The ANDs and ORs inside a CASE ... WHEN cannot be told from
    # top-level ones, so a CASE anywhere in the clause leaves the
    # range unresolved rather than half-split.
    if any(_kw(token) == "CASE" for token in tokens):
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
        return _negate(tokens[1:])
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


def _negate(tokens: list[_Token]) -> tuple[int | None, int | None]:
    # NOT over a single comparison flips the operator: a bound
    # widened to be safe is widened the wrong way round once
    # negated, so the flipped bound is worked out from its own
    # value. NOT over anything more — a conjunction, a disjunction,
    # another NOT, a BETWEEN — stays wide: the rows the other
    # predicates still match cannot be bounded.
    while (
        len(tokens) >= 2
        and tokens[0].kind == "op"
        and tokens[0].text == "("
        and tokens[-1].kind == "op"
        and tokens[-1].text == ")"
        and _is_wrapped(tokens)
    ):
        inner = tokens[1:-1]
        if inner and _kw(inner[0]) == "SELECT":
            break
        tokens = inner
    if not tokens:
        return None, None
    if len(_split_keyword(tokens, "OR")) > 1:
        return None, None
    if len(_split_keyword(tokens, "AND")) > 1:
        return None, None
    if _kw(tokens[0]) == "NOT":
        return None, None
    comparison = _single_comparison(tokens)
    if comparison is None:
        return None, None
    lhs, op, rhs = comparison
    return _comparison(lhs, _INVERT[op], rhs)


def _single_comparison(
    tokens: list[_Token],
) -> tuple[list[_Token], str, list[_Token]] | None:
    # The one top-level comparison operator, if the tokens are a
    # single comparison.
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
                    return None
                cmp_at = i
    if cmp_at < 0:
        return None
    return tokens[:cmp_at], tokens[cmp_at].text, tokens[cmp_at + 1 :]


def _predicate(tokens: list[_Token]) -> tuple[int | None, int | None]:
    comparison = _single_comparison(tokens)
    if comparison is not None:
        lhs, op, rhs = comparison
        return _comparison(lhs, op, rhs)
    return _set_predicate(tokens)


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
        if not _is_timestamp_column(operand):
            return None, None
        if negated:
            # NOT IN and NOT BETWEEN match rows on either side of
            # the span, and no single window covers both.
            return None, None
        if keyword == "BETWEEN":
            parts = _split_keyword(tokens[i + 1 :], "AND")
            if len(parts) != 2:
                return None, None
            low = _literal_value(parts[0], "low")
            high = _literal_value(parts[1], "high")
            if low is None or high is None:
                return None, None
            return (
                low.us,
                high.us + (_DAY_US if high.date_only else 1),
            )
        group = tokens[i + 1 :]
        if (
            len(group) >= 2
            and group[0].kind == "op"
            and group[0].text == "("
            and group[-1].kind == "op"
            and group[-1].text == ")"
            and _is_wrapped(group)
        ):
            lows: list[int] = []
            highs: list[int] = []
            for item in _split_commas(group[1:-1]):
                low = _literal_value(item, "low")
                high = _literal_value(item, "high")
                if low is None or high is None:
                    return None, None
                lows.append(low.us)
                highs.append(high.us + (_DAY_US if high.date_only else 1))
            if lows:
                return min(lows), max(highs)
        return None, None
    return None, None


def _stops_clause(tokens: list[_Token], i: int) -> bool:
    # Whether the keyword at tokens[i] starts a new clause. Every one
    # of these words also parses as an unquoted column name on this
    # server, so the word alone cannot be trusted to end a clause:
    # only the shape of the clause that follows it can, and nothing
    # a column produces matches that shape.
    keyword = _kw(tokens[i])
    nxt = tokens[i + 1] if i + 1 < len(tokens) else None
    if keyword in ("GROUP", "ORDER"):
        return _kw(nxt) == "BY"
    if keyword in ("LIMIT", "OFFSET"):
        # LIMIT 5 ends the clause, and so do LIMIT ALL, LIMIT (5)
        # and a trailing LIMIT — the placeholder of LIMIT
        # %(limit)s, dropped by the tokenizer. LIMIT %s is a
        # parameter too, but the same shape is a modulo on the
        # word, so only the one the statement ends with, or OFFSET
        # follows, is taken for the clause: an arithmetic use
        # continues with an operator instead.
        if nxt is None:
            return True
        if nxt.kind == "number":
            return True
        if _kw(nxt) == "ALL":
            return True
        if (
            nxt.kind == "op"
            and nxt.text == "("
            and i + 2 < len(tokens)
            and tokens[i + 2].kind == "number"
            and i + 3 < len(tokens)
            and tokens[i + 3].kind == "op"
            and tokens[i + 3].text == ")"
        ):
            return True
        if (
            nxt.kind == "op"
            and nxt.text == "%"
            and i + 2 < len(tokens)
            and tokens[i + 2].kind == "name"
            and not tokens[i + 2].quoted
            and tokens[i + 2].text == "s"
            and (i + 3 >= len(tokens) or _kw(tokens[i + 3]) == "OFFSET")
        ):
            return True
        return False
    if keyword == "WINDOW":
        after = tokens[i + 2] if i + 2 < len(tokens) else None
        return nxt is not None and nxt.kind == "name" and _kw(after) == "AS"
    if keyword in _COMPOUND_OPS:
        if _kw(nxt) == "SELECT":
            return True
        if _kw(nxt) in ("ALL", "DISTINCT"):
            after = tokens[i + 2] if i + 2 < len(tokens) else None
            return _kw(after) == "SELECT"
        return False
    return False


def _branch_wheres(tokens: list[_Token]) -> list[list[_Token] | None]:
    # One entry per top-level branch of the statement: the tokens of
    # its WHERE clause, or None when it has none. A branch without a
    # WHERE matches rows at any time, so the branches are combined
    # with the widest window of them all — wide open when one has
    # none. Branches are delimited by set operators and semicolons.
    branches: list[list[_Token] | None] = []
    start: int | None = None
    stop: int | None = None
    seen = False
    depth = 0
    i = 0
    n = len(tokens)

    def cut() -> None:
        # End the branch at the last cut point. A trailing `;` would
        # leave an empty one behind, which would count as a branch
        # without a WHERE and open the window wide, so a branch that
        # holds no tokens at all is not kept.
        nonlocal start, stop, seen
        if not seen and start is None:
            return
        if start is None:
            branches.append(None)
        elif stop is None:
            branches.append(tokens[start:])
        else:
            branches.append(tokens[start:stop])
        start = None
        stop = None
        seen = False

    while i < n:
        token = tokens[i]
        seen = True
        if token.kind == "op":
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
            elif token.text == ";" and depth == 0:
                if start is not None and stop is None:
                    stop = i
                cut()
            i += 1
            continue
        keyword = _kw(token)
        if depth == 0 and _stops_clause(tokens, i):
            if start is not None and stop is None:
                stop = i
            if keyword in _COMPOUND_OPS:
                cut()
            i += 1
            continue
        if (
            depth == 0
            and keyword == "WHERE"
            and start is None
            and stop is None
        ):
            start = i + 1
        i += 1
    cut()
    return branches


def _second_stream(tokens: list[_Token]) -> bool:
    # OpenObserve applies the window to every stream in the query,
    # not only to the one whose _timestamp is filtered, so a query
    # that reads a second stream cannot be narrowed: the other
    # stream's rows can lie anywhere. A JOIN, a comma between
    # tables and a subquery of its own all read one.
    depth = 0
    from_depth: int | None = None
    for i, token in enumerate(tokens):
        if token.kind == "op":
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
                if from_depth is not None and depth < from_depth:
                    from_depth = None
            elif token.text == "," and from_depth == depth:
                return True
            elif token.text == ";" and depth == 0:
                from_depth = None
            continue
        keyword = _kw(token)
        if keyword == "JOIN":
            return True
        if keyword == "FROM":
            if depth >= 1:
                return True
            if from_depth is None:
                from_depth = depth
            continue
        if from_depth == depth and (
            keyword == "WHERE" or _stops_clause(tokens, i)
        ):
            from_depth = None
    return False


def _analyze(sql: str) -> tuple[int | None, int | None]:
    try:
        tokens = _tokenize(sql)
        if _second_stream(tokens):
            return None, None
        intervals = [
            _parse_bool(where) if where is not None else (None, None)
            for where in _branch_wheres(tokens)
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

    OpenObserve needs these outside the SQL on every search, and
    applies them itself as ``_timestamp >= start AND _timestamp <
    end``, on top of the SQL and to every stream in it. The range
    therefore only has to cover the rows the SQL can match: wider is
    always safe, narrower loses rows.

    Only a comparison on _timestamp itself narrows the range: the
    window is on _timestamp, and a comparison on any other column,
    time-like name or not, says nothing about it — nor does one on a
    function of it, like date_trunc or histogram, which rounds or
    moves the timestamps it compares. A query that reads a second
    stream — a JOIN, a comma between tables, a subquery — cannot be
    narrowed either: the window reaches every stream in the query,
    not only the filtered one, and the other stream's rows can lie
    anywhere. Nor can one whose WHERE holds a CASE, whose inner ANDs
    and ORs cannot be told from top-level ones. Of the branches a
    set operator combines, each WHERE yields a window and the widest
    of them covers the lot — wide open when a branch has no WHERE.

    Within a branch, AND keeps the narrowest window of its
    conjuncts, OR the widest of its disjuncts, and NOT flips the one
    comparison it negates, worked out from the value again so that
    the day a day-granular inclusive bound covers is not carried
    through the flip: NOT (_timestamp <= '2024-01-01') starts just
    after midnight, not after the whole day. NOT over anything more
    than a single comparison leaves the range wide: the rows the
    other predicates still match cannot be bounded.

    A comparison that cannot be resolved leaves that side of the
    range open: start falls back to just after the epoch (the API
    rejects zero), and end to now plus the 24 hours OpenObserve
    ingests stamped ahead of it, with slack for clock skew, or to
    the far future when the start itself lies ahead of now, since
    no horizon based on now covers the rows it matches.

    A timestamp value is any ISO 8601 literal string, with or without
    TIMESTAMP or DATE typing, CAST, to_timestamp, or now() plus or
    minus an INTERVAL; a DATE-typed one lands on midnight whatever the
    time in the string, a to_timestamp with a format argument and an
    INTERVAL of anything besides <number> <unit> pairs bound nothing,
    and a sub-microsecond fraction is truncated away below and rounded
    up above. The shortest month and year go into a lower bound and
    the longest into an upper one. A bare integer is an epoch only as
    16-digit microseconds, the width of _timestamp itself: shorter
    ones are seconds or milliseconds, compared as they stand and far
    tighter than the writer meant, so they bound nothing.
    """
    low, high = _analyze(sql)
    if low is None or low < 1:
        low = 1
    if high is None:
        now = _now_us()
        high = now + _FUTURE_MARGIN_US
        if low > now:
            # A lower bound ahead of now matches rows that no
            # horizon based on now can cover: the end goes to the
            # far future instead of collapsing onto the start.
            high = max(low + 1, _EPOCH_MAX_US)
    elif high <= low:
        # Both bounds resolved with the end at or before the start:
        # the SQL cannot match anything, so any window is safe, and
        # the smallest one keeps the search cheap.
        high = low + 1
    return low, high
