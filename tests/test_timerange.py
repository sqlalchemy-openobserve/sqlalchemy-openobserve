"""Tests for working out a query's time range"""

import random
from datetime import datetime, timezone
from operator import eq, ge, gt, le, lt, ne
from typing import Any, Callable

import sqlalchemy as sa
from sqlalchemy import create_engine

from sqlalchemy_openobserve.timerange import extract_time_range

HOUR = 3_600_000_000
DAY = 86_400_000_000


def to_us(value: datetime) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def us(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    microsecond: int = 0,
) -> int:
    return to_us(
        datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            microsecond,
            tzinfo=timezone.utc,
        )
    )


def now_us() -> int:
    return to_us(datetime.now(timezone.utc))


def test_no_time_filter_returns_everything() -> None:
    low, high = extract_time_range("SELECT * FROM logs")
    assert low == 1
    assert now_us() <= high <= now_us() + 26 * HOUR


def test_default_end_covers_rows_stamped_ahead() -> None:
    # OpenObserve ingests timestamps up to 24 hours ahead of now, so
    # the default end must cover a row stamped +5h, which the wide
    # window returns and a one-hour margin would not.
    before = now_us()
    _, high = extract_time_range("SELECT * FROM t WHERE level = 'info'")
    after = now_us()
    assert high >= after + 5 * HOUR
    assert high <= before + 26 * HOUR


def test_lower_bound_only() -> None:
    low, high = extract_time_range(
        "SELECT * FROM t WHERE _timestamp >= '2024-01-01'"
    )
    assert low == us(2024, 1, 1)
    assert high >= now_us()


def test_upper_bound_only() -> None:
    low, high = extract_time_range(
        "SELECT * FROM t WHERE _timestamp < '2024-01-01'"
    )
    assert low == 1
    assert high == us(2024, 1, 1)


def test_both_bounds() -> None:
    sql = (
        "SELECT * FROM logs WHERE _timestamp >= '2024-01-01T00:00:00Z' "
        "AND _timestamp < '2024-02-01T00:00:00Z'"
    )
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 2, 1))


def test_iso_string_forms() -> None:
    forms = {
        "'2024-01-02 03:04:05'": us(2024, 1, 2, 3, 4, 5),
        "'2024-01-02T03:04:05'": us(2024, 1, 2, 3, 4, 5),
        "'2024-01-02T03:04:05.123456Z'": us(2024, 1, 2, 3, 4, 5, 123456),
        "'2024-01-02T03:04:05.123456789Z'": us(2024, 1, 2, 3, 4, 5, 123456),
        "'2024-01-02T08:34:05+05:30'": us(2024, 1, 2, 3, 4, 5),
        "'2024-01-02'": us(2024, 1, 2),
    }
    for literal, expected in forms.items():
        sql = f"SELECT * FROM t WHERE _timestamp >= {literal}"
        low, _ = extract_time_range(sql)
        assert low == expected, literal


def test_strict_and_inclusive_operators() -> None:
    sql = (
        "SELECT * FROM t WHERE _timestamp > '2024-01-01 00:00:00' "
        "AND _timestamp <= '2024-02-01 12:00:00'"
    )
    low, high = extract_time_range(sql)
    assert low == us(2024, 1, 1) + 1
    assert high == us(2024, 2, 1, 12) + 1


def test_mirrored_operators() -> None:
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE '2024-01-01' <= _timestamp"
    )
    assert low == us(2024, 1, 1)
    _, high = extract_time_range(
        "SELECT * FROM t WHERE '2024-01-01' > _timestamp"
    )
    assert high == us(2024, 1, 1)


def test_equality_window() -> None:
    sql = "SELECT * FROM t WHERE _timestamp = '2024-01-02T03:04:05.123456'"
    low, high = extract_time_range(sql)
    value = us(2024, 1, 2, 3, 4, 5, 123456)
    assert (low, high) == (value, value + 1)


def test_between_is_inclusive() -> None:
    sql = (
        "SELECT * FROM t WHERE _timestamp BETWEEN '2024-01-01 00:00:00' "
        "AND '2024-02-01 00:00:00'"
    )
    low, high = extract_time_range(sql)
    assert (low, high) == (us(2024, 1, 1), us(2024, 2, 1) + 1)


def test_between_epoch_integers() -> None:
    sql = (
        "SELECT * FROM t WHERE _timestamp BETWEEN 1704067200000000 "
        "AND 1704153600000000"
    )
    low, high = extract_time_range(sql)
    assert (low, high) == (us(2024, 1, 1), us(2024, 1, 2) + 1)


def test_not_complements_one_sided_bound() -> None:
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE NOT (_timestamp < '2024-01-01')"
    )
    assert low == us(2024, 1, 1)
    _, high = extract_time_range(
        "SELECT * FROM t WHERE NOT (_timestamp >= '2024-01-01')"
    )
    assert high == us(2024, 1, 1)


def test_not_over_date_only_upper_bound_starts_just_after() -> None:
    # The day a day-granular inclusive upper bound covers must not
    # carry through the flip: the true start is just after midnight,
    # not after the whole day.
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE NOT (_timestamp <= '2024-01-01')"
    )
    assert low == us(2024, 1, 1) + 1


def test_not_between_is_wide() -> None:
    sql = (
        "SELECT * FROM t WHERE _timestamp NOT BETWEEN '2024-01-01' "
        "AND '2024-02-01'"
    )
    low, high = extract_time_range(sql)
    assert low == 1
    assert high >= now_us()


# NOT over anything more than a single comparison stays wide: the
# other predicates still match rows the negated time filter leaves
# outside its window, and that window, once widened to be safe, is
# widened the wrong way round by a complement.


def test_not_mixed_conjunction_stays_wide() -> None:
    # NOT (_timestamp >= a AND region = 'us') still matches rows at
    # any _timestamp through region != 'us'
    sql = (
        "SELECT * FROM t WHERE NOT "
        "(_timestamp >= '2024-01-01' AND region = 'us')"
    )
    low, high = extract_time_range(sql)
    assert low == 1
    assert high >= now_us()


def test_not_mixed_conjunction_wrapped_stays_wide() -> None:
    sql = (
        "SELECT * FROM t WHERE NOT "
        "((_timestamp >= '2024-01-01' AND region = 'us'))"
    )
    low, high = extract_time_range(sql)
    assert low == 1
    assert high >= now_us()


def test_not_both_time_bounds_stays_wide() -> None:
    # Rows outside the window can lie on either side of it
    sql = (
        "SELECT * FROM t WHERE NOT "
        "(_timestamp >= '2024-01-01' AND _timestamp < '2024-02-01')"
    )
    low, high = extract_time_range(sql)
    assert low == 1
    assert high >= now_us()


def test_not_over_conjunction_is_wide() -> None:
    # NOT (_timestamp >= a AND _timestamp >= b) == _timestamp < a OR
    # _timestamp < b, and rows matching either can lie anywhere
    sql = (
        "SELECT * FROM t WHERE NOT "
        "(_timestamp >= '2024-01-01' AND _timestamp >= '2024-02-01')"
    )
    low, high = extract_time_range(sql)
    assert low == 1
    assert high >= now_us()


def test_not_over_disjunction_is_wide() -> None:
    # NOT (_timestamp < a OR _timestamp >= b) == _timestamp >= a AND
    # _timestamp < b, which narrows in theory, but the complement of
    # each part is only as safe as the part is narrow
    sql = (
        "SELECT * FROM t WHERE NOT "
        "(_timestamp < '2024-01-01' OR _timestamp >= '2024-02-01')"
    )
    low, high = extract_time_range(sql)
    assert low == 1
    assert high >= now_us()


def test_not_mixed_disjunction_is_wide() -> None:
    # NOT (_timestamp >= a OR region = 'us') still matches rows at
    # any _timestamp through region != 'us'
    sql = (
        "SELECT * FROM t WHERE NOT "
        "(_timestamp >= '2024-01-01' OR region = 'us')"
    )
    low, high = extract_time_range(sql)
    assert low == 1
    assert high >= now_us()


def test_nested_not_is_wide() -> None:
    for sql in (
        "SELECT * FROM t WHERE NOT NOT (_timestamp >= '2024-01-01')",
        "SELECT * FROM t WHERE NOT (NOT (_timestamp >= '2024-01-01'))",
    ):
        low, high = extract_time_range(sql)
        assert low == 1, sql
        assert high >= now_us(), sql


def test_or_takes_widest() -> None:
    sql = (
        "SELECT * FROM t WHERE _timestamp >= '2024-01-01' "
        "OR _timestamp >= '2023-06-01'"
    )
    low, _ = extract_time_range(sql)
    assert low == us(2023, 6, 1)


def test_or_with_non_time_column_is_wide() -> None:
    sql = "SELECT * FROM t WHERE _timestamp >= '2024-01-01' OR region = 'us'"
    low, _ = extract_time_range(sql)
    assert low == 1


def test_and_takes_tightest() -> None:
    sql = (
        "SELECT * FROM t WHERE _timestamp >= '2024-01-01' "
        "AND _timestamp >= '2024-02-01' AND _timestamp <= '2024-03-01'"
    )
    low, high = extract_time_range(sql)
    assert (low, high) == (us(2024, 2, 1), us(2024, 3, 2))


def test_contradictory_bounds_stay_ordered() -> None:
    sql = (
        "SELECT * FROM t WHERE _timestamp >= '2024-02-01' "
        "AND _timestamp < '2024-01-01'"
    )
    low, high = extract_time_range(sql)
    assert (low, high) == (us(2024, 2, 1), us(2024, 2, 1) + 1)


# A lower bound in the future matches rows that no horizon based on
# now can cover: the end falls back to the far future instead of
# collapsing onto the start.


def test_future_lower_bound_stays_open() -> None:
    sql = "SELECT * FROM t WHERE _timestamp >= '2030-01-01'"
    low, high = extract_time_range(sql)
    assert low == us(2030, 1, 1)
    assert high == us(2100, 1, 1)


def test_future_lower_bound_interval_stays_open() -> None:
    # A month added to a lower bound takes its shortest length, so
    # the bound stays under the real one
    sql = "SELECT * FROM t WHERE _timestamp >= now() + INTERVAL '1 month'"
    before = now_us()
    low, high = extract_time_range(sql)
    after = now_us()
    assert before + 28 * DAY <= low <= after + 28 * DAY
    assert high == us(2100, 1, 1)


def test_future_lower_bound_epoch_stays_open() -> None:
    sql = f"SELECT * FROM t WHERE _timestamp >= {us(2080, 1, 1)}"
    low, high = extract_time_range(sql)
    assert low == us(2080, 1, 1)
    assert high == us(2100, 1, 1)


def test_future_bounds_resolved_is_exact() -> None:
    sql = (
        "SELECT * FROM t WHERE _timestamp >= '2030-01-01' "
        "AND _timestamp < '2031-01-01'"
    )
    assert extract_time_range(sql) == (us(2030, 1, 1), us(2031, 1, 1))


def test_cast_forms() -> None:
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE _timestamp >= CAST('2024-01-01' AS TIMESTAMP)"
    )
    assert low == us(2024, 1, 1)
    _, high = extract_time_range(
        "SELECT * FROM t WHERE CAST('2024-01-01' AS DATE) > _timestamp"
    )
    assert high == us(2024, 1, 1)


def test_typed_literal() -> None:
    # v1.0.4 rejects this shape server-side (Int64 >= Timestamp);
    # kept so the parser keeps up with a server that types
    # _timestamp as a timestamp.
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE _timestamp >= TIMESTAMP '2024-01-01'"
    )
    assert low == us(2024, 1, 1)


def test_to_timestamp_forms() -> None:
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE _timestamp >= "
        "to_timestamp('2024-01-01T00:00:00Z')"
    )
    assert low == us(2024, 1, 1)
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE _timestamp >= "
        "to_timestamp('2024-01-01', '%Y-%m-%d')"
    )
    assert low == us(2024, 1, 1)


def test_now_minus_interval() -> None:
    before = now_us()
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE _timestamp >= now() - INTERVAL '24 hours'"
    )
    after = now_us()
    assert before - 24 * HOUR <= low <= after - 24 * HOUR


def test_current_timestamp_minus_interval() -> None:
    before = now_us()
    _, high = extract_time_range(
        "SELECT * FROM t WHERE _timestamp < CURRENT_TIMESTAMP "
        "- INTERVAL '30 minutes'"
    )
    after = now_us()
    assert before - 30 * 60_000_000 <= high <= after - 30 * 60_000_000


# Months and years have no fixed length: the shortest goes into a
# lower bound and the longest into an upper one, whichever way the
# INTERVAL goes, each widened the way that cannot cut into the real
# window.


def test_month_interval_is_widened_per_direction() -> None:
    # '2024-02-01' + 1 month is Mar 1 at the shortest Feb, so the
    # lower bound takes Feb 29 to stay under it; the longest, Mar 3,
    # would start past it.
    sql = (
        "SELECT * FROM t WHERE _timestamp >= '2024-02-01' + INTERVAL '1 month'"
    )
    low, _ = extract_time_range(sql)
    assert low == us(2024, 2, 29)
    # '2024-03-01' - 1 month is Feb 1 at the longest Feb, so the
    # upper bound takes Feb 2 to stay over it; the shortest, Jan 30,
    # would end before it.
    sql = (
        "SELECT * FROM t WHERE _timestamp < '2024-03-01' - INTERVAL '1 month'"
    )
    _, high = extract_time_range(sql)
    assert high == us(2024, 2, 2)


def test_year_interval_is_widened_per_direction() -> None:
    # '2024-01-01' + 1 year is Jan 1 2025 across the leap day, so the
    # lower bound takes the shortest year, Dec 31 2024, to stay under
    sql = (
        "SELECT * FROM t WHERE _timestamp >= '2024-01-01' + INTERVAL '1 year'"
    )
    low, _ = extract_time_range(sql)
    assert low == us(2024, 12, 31)


def test_month_interval_around_now() -> None:
    # Subtracted from a lower bound the month takes its longest, and
    # added to an upper bound its longest too: each widened the way
    # that cannot cut into the real window.
    before = now_us()
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE _timestamp >= now() - INTERVAL '1 month'"
    )
    after = now_us()
    assert before - 31 * DAY <= low <= after - 31 * DAY
    before = now_us()
    _, high = extract_time_range(
        "SELECT * FROM t WHERE _timestamp < now() + INTERVAL '1 month'"
    )
    after = now_us()
    assert before + 31 * DAY <= high <= after + 31 * DAY


def test_multi_part_interval() -> None:
    before = now_us()
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE _timestamp >= now() - INTERVAL '1 day 2 hours'"
    )
    after = now_us()
    assert before - 26 * HOUR <= low <= after - 26 * HOUR


def test_epoch_digit_scales() -> None:
    # _timestamp is microseconds, so only a 16-digit integer is one
    # of those: the server compares shorter ones as they stand,
    # against microseconds — 1793016000 is a tiny number, not a
    # recent date — so they bound nothing.
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE _timestamp >= 1704067200000000"
    )
    assert low == us(2024, 1, 1)
    for value in (1704067200, 1704067200000, 1704067200000000000):
        low, _ = extract_time_range(
            f"SELECT * FROM t WHERE _timestamp >= {value}"
        )
        assert low == 1, value


def test_epoch_needs_timestamp_column() -> None:
    low, _ = extract_time_range("SELECT * FROM t WHERE id >= 1704067200000000")
    assert low == 1


def test_epoch_outside_plausible_window_is_wide() -> None:
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE _timestamp >= 9999999999999999"
    )
    assert low == 1


def test_in_list() -> None:
    sql = (
        "SELECT * FROM t WHERE _timestamp IN "
        "('2024-01-01 00:00:00', '2024-01-03 00:00:00')"
    )
    low, high = extract_time_range(sql)
    assert (low, high) == (us(2024, 1, 1), us(2024, 1, 3) + 1)


def test_in_epoch_integers() -> None:
    sql = (
        "SELECT * FROM t WHERE _timestamp IN "
        "(1704067200000000, 1704240000000000)"
    )
    low, high = extract_time_range(sql)
    assert (low, high) == (us(2024, 1, 1), us(2024, 1, 3) + 1)


def test_subquery_time_filter_is_wide() -> None:
    # A time filter inside a subquery bounds the subquery's rows,
    # not the outer query's
    sql = (
        "SELECT * FROM t WHERE o IN "
        "(SELECT id FROM u WHERE _timestamp >= '2024-01-01')"
    )
    low, _ = extract_time_range(sql)
    assert low == 1


def test_subquery_stream_is_wide() -> None:
    # The window reaches the subquery's stream too: a filter on this
    # table's _timestamp bounds nothing about that stream's rows.
    sql = (
        "SELECT * FROM t WHERE _timestamp >= '2024-01-01' "
        "AND level IN (SELECT level FROM u)"
    )
    low, _ = extract_time_range(sql)
    assert low == 1


def test_join_on_is_wide() -> None:
    sql = (
        "SELECT a.x FROM a JOIN b ON a.id = b.id "
        "AND b._timestamp >= '2024-01-01' WHERE a.y = 1"
    )
    low, _ = extract_time_range(sql)
    assert low == 1


def test_join_is_wide() -> None:
    # OpenObserve applies the window to every stream in the query:
    # b's older rows would fall outside a window narrowed onto a's
    # filter, and come back NULL (checked on a real server).
    sql = (
        "SELECT b.message FROM a LEFT JOIN b ON a.level = b.level "
        "WHERE a._timestamp >= '2024-01-01'"
    )
    low, high = extract_time_range(sql)
    assert low == 1
    assert high >= now_us()


def test_comma_join_is_wide() -> None:
    sql = "SELECT b.message FROM a, b WHERE a._timestamp >= '2024-01-01'"
    low, _ = extract_time_range(sql)
    assert low == 1


def test_having_is_wide() -> None:
    sql = (
        "SELECT region, max(_timestamp) FROM t WHERE region = 'us' "
        "GROUP BY region HAVING _timestamp >= '2024-01-01'"
    )
    low, _ = extract_time_range(sql)
    assert low == 1


def test_union_takes_widest() -> None:
    sql = (
        "SELECT _timestamp FROM a WHERE _timestamp >= '2024-01-01' "
        "UNION ALL SELECT _timestamp FROM b WHERE _timestamp >= '2023-01-01'"
    )
    low, high = extract_time_range(sql)
    assert low == us(2023, 1, 1)
    assert high >= now_us()


def test_union_branch_without_where_is_wide() -> None:
    # A branch with no WHERE matches rows at any time: the window,
    # applied to every stream in the query, cuts its older rows even
    # though the SQL never names a time (checked on a real server:
    # 6 rows wide, 9 expected). Same for EXCEPT and INTERSECT.
    for keyword in ("UNION ALL", "UNION", "EXCEPT", "INTERSECT"):
        sql = (
            "SELECT _timestamp FROM a WHERE _timestamp >= '2024-01-01' "
            f"{keyword} SELECT _timestamp FROM b"
        )
        low, high = extract_time_range(sql)
        assert low == 1, keyword
        assert high >= now_us(), keyword


def test_case_in_where_is_wide() -> None:
    # The AND inside the WHEN would be split like a top-level one and
    # narrow the window onto the WHEN's bound, losing the rows the
    # ELSE still matches
    sql = (
        "SELECT * FROM t WHERE CASE WHEN _timestamp > "
        "TIMESTAMP '2026-10-26 00:00:00' AND level = 'a' "
        "THEN false ELSE true END"
    )
    low, _ = extract_time_range(sql)
    assert low == 1


def test_comment_is_ignored() -> None:
    sql = (
        "SELECT * FROM t -- WHERE _timestamp >= '2024-01-01'\n"
        "WHERE note = 'hello' AND other = 1"
    )
    low, _ = extract_time_range(sql)
    assert low == 1


def test_escaped_quote_is_not_a_time() -> None:
    sql = "SELECT * FROM t WHERE note = 'it''s 2024-01-01'"
    low, _ = extract_time_range(sql)
    assert low == 1


# Only a comparison on _timestamp itself narrows the range: the
# window is on _timestamp, and a filter on any other column, however
# time-like its name, says nothing about it.


def test_time_like_column_names_bound_nothing() -> None:
    for column in (
        "candidate",
        "update_count",
        "response_time",
        "event_date",
        "ts",
        "dttm",
        "day",
        '"My Time"',
    ):
        low, _ = extract_time_range(
            f"SELECT * FROM t WHERE {column} >= '2026-12-01'"
        )
        assert low == 1, column


def test_typed_literal_on_another_column_bounds_nothing() -> None:
    # A typed timestamp bounds the range against _timestamp only:
    # against any other column it is an ordinary comparison, however
    # timestamp-shaped the literal.
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE created >= TIMESTAMP '2026-12-01'"
    )
    assert low == 1


def test_time_function_of_column_bounds_nothing() -> None:
    # date_trunc, histogram and friends round the timestamps they
    # compare, so the raw column can lie outside any window they
    # imply; arithmetic on it moves it just the same.
    sql = (
        "SELECT * FROM t WHERE date_trunc('day', _timestamp) = "
        "TIMESTAMP '2026-10-26'"
    )
    low, _ = extract_time_range(sql)
    assert low == 1
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE histogram(_timestamp, '1 day') >= 5"
    )
    assert low == 1
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE _timestamp + INTERVAL '1 day' >= '2024-01-01'"
    )
    assert low == 1


def test_conversion_function_of_column_bounds_nothing() -> None:
    # Superset wraps an epoch column with from_unixtime, which moves
    # the column's timestamps into its own scale
    sql = (
        "SELECT * FROM t WHERE from_unixtime(_timestamp) >= "
        "TIMESTAMP '2024-01-01'"
    )
    low, _ = extract_time_range(sql)
    assert low == 1


def test_cast_of_column_bounds_nothing() -> None:
    sql = "SELECT * FROM t WHERE CAST(_timestamp AS TIMESTAMP) >= '2024-01-01'"
    low, _ = extract_time_range(sql)
    assert low == 1


def test_unknown_function_of_ordinary_column_is_wide() -> None:
    sql = "SELECT * FROM t WHERE lower(region) >= '2024-01-01'"
    low, _ = extract_time_range(sql)
    assert low == 1


def test_superset_style_histogram_query() -> None:
    sql = (
        "SELECT histogram(_timestamp, '1 day') AS t, count(*) AS c "
        "FROM logs WHERE _timestamp >= '2024-01-01' "
        "AND _timestamp < '2024-02-01' "
        "GROUP BY t ORDER BY t LIMIT 100"
    )
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 2, 1))


def test_unrendered_placeholder_is_wide() -> None:
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE _timestamp >= %(start)s "
        "AND _timestamp < %(end)s"
    )
    assert low == 1


# Every one of these words parses as an unquoted column name on this
# server (verified against v1.0.4), so a filter on one must not be
# taken for the start of the next clause: the OR after it would be
# dropped and the window narrowed onto the time filter.


def test_clause_keyword_column_names_are_not_cut() -> None:
    for column in (
        "full",
        "set",
        "values",
        "window",
        "limit",
        "offset",
        "having",
        "qualify",
        "order",
        "group",
        "case",
        "join",
        "union",
        "except",
        "intersect",
        "select",
        "from",
        "where",
        "on",
        "using",
        "cross",
        "inner",
        "natural",
        "left",
        "right",
        "with",
        "into",
        "returning",
    ):
        sql = (
            "SELECT * FROM t WHERE _timestamp >= '2024-01-01' "
            f"AND {column} = 1 OR level = 'b'"
        )
        low, _ = extract_time_range(sql)
        assert low == 1, column


def test_clause_after_where_is_cut() -> None:
    sql = (
        "SELECT * FROM t WHERE _timestamp >= '2024-01-01' "
        "GROUP BY level HAVING level = 'a' ORDER BY level LIMIT 10"
    )
    low, _ = extract_time_range(sql)
    assert low == us(2024, 1, 1)


def test_window_clause_after_where_is_cut() -> None:
    sql = (
        "SELECT level, row_number() OVER w AS rn FROM t "
        "WHERE _timestamp >= '2024-01-01' "
        "WINDOW w AS (PARTITION BY level)"
    )
    low, _ = extract_time_range(sql)
    assert low == us(2024, 1, 1)


def test_never_raises() -> None:
    queries = (
        "",
        "NOT VALID SQL !!!",
        "SELECT * FROM t WHERE (((",
        "SELECT * FROM t WHERE _timestamp >= '2024-01-01'; SELECT 1",
        "SELECT * FROM t WHERE CASE WHEN",
    )
    for sql in queries:
        low, high = extract_time_range(sql)
        assert 1 <= low < high, sql


def test_lowercase_keywords() -> None:
    sql = (
        "select * from t where _timestamp >= '2024-01-01' "
        "and _timestamp < '2024-02-01'"
    )
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 2, 1))


# What Superset sends for this engine, as pinned against the server
# in docker-compose.yml (v1.0.4): _timestamp is an Int64 of
# microseconds, and only an integer literal compares against it — a
# TIMESTAMP or a bare string literal is rejected with "Cannot infer
# common argument type … Int64 >= Timestamp(ns)". The
# python_date_format that works is epoch_us, whose dttm literals are
# bare 16-digit integers, as get_time_filter() builds them and as the
# query below compiles. The TIMESTAMP and strftime shapes are kept
# above as parser robustness: pinned against what they narrow, not
# against what the server runs.


def test_superset_compiled_time_filter() -> None:
    # The full Superset path: a chart query compiled through this
    # dialect with literal_binds, as compile_sqla_query() does.
    col: sa.ColumnClause[Any] = sa.column("_timestamp")
    query = (
        sa.select(sa.func.count())
        .select_from(sa.table("logs"))
        .where(col >= 1704067200000000, col < 1706745600000000)
    )
    engine = create_engine(
        "openobserve+rest://user:pass@localhost:5080/default"
    )
    sql = str(query.compile(engine, compile_kwargs={"literal_binds": True}))
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 2, 1))


def test_superset_true_prefix() -> None:
    # get_time_filter() wraps the clauses in and_(True, ...)
    sql = (
        "SELECT count(*) FROM logs WHERE true "
        "AND _timestamp >= 1704067200000000 "
        "AND _timestamp < 1706745600000000"
    )
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 2, 1))


def test_superset_epoch_us_literals() -> None:
    # python_date_format="epoch_us" on _timestamp: bare epoch
    # integers, the one shape the server runs against _timestamp
    sql = (
        "SELECT count(*) FROM logs WHERE _timestamp >= 1704067200000000 "
        "AND _timestamp < 1706745600000000"
    )
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 2, 1))


def test_superset_date_typed_literal_on_other_column() -> None:
    # convert_dttm() renders DATE '...' for a date column: against
    # anything but _timestamp it bounds nothing
    sql = "SELECT count(*) FROM logs WHERE d >= DATE '2024-01-01'"
    low, _ = extract_time_range(sql)
    assert low == 1


def test_superset_strftime_fallback_literal_on_other_column() -> None:
    # The strftime fallback renders a bare string literal: against
    # anything but _timestamp it is an ordinary string comparison
    sql = "SELECT count(*) FROM logs WHERE ts >= '2024-01-01 00:00:00'"
    low, _ = extract_time_range(sql)
    assert low == 1


def test_superset_grain_wrapped_filter_column_is_wide() -> None:
    # A TEMPORAL_RANGE adhoc filter with a time grain wraps the
    # column, and date_trunc rounds it out of any window
    sql = (
        "SELECT count(*) FROM logs WHERE "
        "DATE_TRUNC('day', _timestamp) >= "
        "TIMESTAMP '2024-01-01 00:00:00.000000'"
    )
    low, high = extract_time_range(sql)
    assert low == 1
    assert high >= now_us()


def test_superset_epoch_wrapped_filter_column_is_wide() -> None:
    # An epoch python_date_format column is wrapped with
    # epoch_to_dttm, which moves the column into its own scale
    sql = (
        "SELECT count(*) FROM logs WHERE from_unixtime(_timestamp) >= "
        "TIMESTAMP '2024-01-01 00:00:00'"
    )
    low, _ = extract_time_range(sql)
    assert low == 1


# A day-granular literal, like '2024-01-01', covers its whole day on
# the upper side: the range ends at the next midnight.


def test_date_only_equality_covers_whole_day() -> None:
    sql = "SELECT * FROM t WHERE _timestamp = '2024-01-01'"
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 1, 2))


def test_date_only_upper_bound_ends_at_next_midnight() -> None:
    sql = "SELECT * FROM t WHERE _timestamp <= '2024-01-01'"
    low, high = extract_time_range(sql)
    assert low == 1
    assert high == us(2024, 1, 2)


def test_date_only_between_covers_final_day() -> None:
    sql = (
        "SELECT * FROM t WHERE _timestamp BETWEEN "
        "'2024-01-01' AND '2024-01-02'"
    )
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 1, 3))


def test_date_only_in_covers_final_day() -> None:
    sql = "SELECT * FROM t WHERE _timestamp IN ('2024-01-01', '2024-01-02')"
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 1, 3))


# A date-shaped string compared to an ordinary column is just a
# string comparison: it must not narrow the range.


def test_non_time_column_does_not_constrain_range() -> None:
    sql = (
        "SELECT * FROM t WHERE region > '2024-01-01' "
        "AND _timestamp >= '2024-06-01'"
    )
    low, _ = extract_time_range(sql)
    assert low == us(2024, 6, 1)


def test_non_time_column_stricter_than_time_filter_is_ignored() -> None:
    # The region bound is the tighter one; it still must not win
    sql = (
        "SELECT * FROM t WHERE region > '2025-06-01' "
        "AND _timestamp >= '2024-06-01'"
    )
    low, _ = extract_time_range(sql)
    assert low == us(2024, 6, 1)


# A randomized check that the window covers every row a filter
# matches: the hand-written cases above cannot cover the nesting.


def _literals() -> list[tuple[str, int]]:
    # (sql literal, the µs it means)
    lits: list[tuple[str, int]] = [
        ("'2023-06-15'", us(2023, 6, 15)),
        ("'2025-03-01'", us(2025, 3, 1)),
    ]
    for day in range(1, 28, 3):
        date = f"2024-01-{day:02d}"
        lits.append((f"'{date}'", us(2024, 1, day)))
        lits.append((f"'{date}T12:00:00Z'", us(2024, 1, day, 12)))
        if day % 4 == 1:
            lits.append((str(us(2024, 1, day)), us(2024, 1, day)))
    return lits


def _stamps(lits: list[tuple[str, int]]) -> list[int]:
    stamps: set[int] = set()
    for _, value in lits:
        for delta in (0, 1, -1, HOUR, -HOUR, DAY, -DAY, 26 * HOUR, -26 * HOUR):
            stamps.add(value + delta)
    stamps.update(
        {
            us(2023, 1, 1),
            us(2025, 12, 31),
            us(2024, 2, 14),
            us(2024, 1, 15, 6, 30),
        }
    )
    return sorted(stamps)


_OPS: dict[str, Callable[[int, int], bool]] = {
    ">=": ge,
    ">": gt,
    "<": lt,
    "<=": le,
    "=": eq,
    "<>": ne,
    "!=": ne,
}

_FLIP = {
    ">=": "<=",
    "<=": ">=",
    ">": "<",
    "<": ">",
    "=": "=",
    "<>": "<>",
    "!=": "!=",
}


def _leaf(
    rng: random.Random, lits: list[tuple[str, int]]
) -> tuple[str, Callable[[int], bool]]:
    if rng.random() < 0.15:
        # A filter on another column: it matches rows wherever their
        # _timestamp lies, so it must not narrow anything
        return "region = 'us'", lambda ts: True
    if rng.random() < 0.25:
        low, high = rng.sample(lits, 2)
        if low[1] > high[1]:
            low, high = high, low
        sql = f"_timestamp BETWEEN {low[0]} AND {high[0]}"
        return sql, lambda ts, low=low, high=high: low[1] <= ts <= high[1]
    op = rng.choice([">=", ">", "<", "<=", "=", "<>", "!="])
    literal, value = rng.choice(lits)
    fn = _OPS[op]
    if rng.random() < 0.5:
        # The same comparison, written the other way round:
        # literal FLIP[op] _timestamp is _timestamp op literal
        sql = f"{literal} {_FLIP[op]} _timestamp"
        return sql, lambda ts, fn=fn, value=value: fn(ts, value)
    sql = f"_timestamp {op} {literal}"
    return sql, lambda ts, fn=fn, value=value: fn(ts, value)


def _random_filter(
    rng: random.Random, lits: list[tuple[str, int]], depth: int = 0
) -> tuple[str, Callable[[int], bool]]:
    if depth >= 3 or rng.random() < 0.35:
        return _leaf(rng, lits)
    kind = rng.choice(["and", "or", "not"])
    if kind == "not":
        inner_sql, inner = _random_filter(rng, lits, depth + 1)
        return f"NOT ({inner_sql})", lambda ts, inner=inner: not inner(ts)
    left_sql, left = _random_filter(rng, lits, depth + 1)
    right_sql, right = _random_filter(rng, lits, depth + 1)
    if kind == "and":
        return (
            f"({left_sql} AND {right_sql})",
            lambda ts, left=left, right=right: left(ts) and right(ts),
        )
    return (
        f"({left_sql} OR {right_sql})",
        lambda ts, left=left, right=right: left(ts) or right(ts),
    )


def test_random_filters_cover_every_matching_row() -> None:
    rng = random.Random(20240127)
    lits = _literals()
    stamps = _stamps(lits)
    for _ in range(400):
        sql, matches = _random_filter(rng, lits)
        low, high = extract_time_range(f"SELECT * FROM t WHERE {sql}")
        assert 1 <= low < high, sql
        for ts in stamps:
            if matches(ts):
                assert low <= ts < high, (sql, ts)
