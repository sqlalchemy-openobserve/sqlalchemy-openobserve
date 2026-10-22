"""Tests for working out a query's time range"""

from datetime import datetime, timezone
from typing import Any

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
            year, month, day, hour, minute, second, microsecond,
            tzinfo=timezone.utc,
        )
    )


def now_us() -> int:
    return to_us(datetime.now(timezone.utc))


def test_no_time_filter_returns_everything() -> None:
    low, high = extract_time_range("SELECT * FROM logs")
    assert low == 1
    assert now_us() <= high <= now_us() + 2 * HOUR


def test_lower_bound_only() -> None:
    low, high = extract_time_range("SELECT * FROM t WHERE ts >= '2024-01-01'")
    assert low == us(2024, 1, 1)
    assert high >= now_us()


def test_upper_bound_only() -> None:
    low, high = extract_time_range("SELECT * FROM t WHERE ts < '2024-01-01'")
    assert low == 1
    assert high == us(2024, 1, 1)


def test_both_bounds() -> None:
    sql = (
        "SELECT * FROM logs WHERE ts >= '2024-01-01T00:00:00Z' "
        "AND ts < '2024-02-01T00:00:00Z'"
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
        low, _ = extract_time_range(f"SELECT * FROM t WHERE ts >= {literal}")
        assert low == expected, literal


def test_strict_and_inclusive_operators() -> None:
    sql = (
        "SELECT * FROM t WHERE ts > '2024-01-01 00:00:00' "
        "AND ts <= '2024-02-01 12:00:00'"
    )
    low, high = extract_time_range(sql)
    assert low == us(2024, 1, 1) + 1
    assert high == us(2024, 2, 1, 12) + 1


def test_mirrored_operators() -> None:
    low, _ = extract_time_range("SELECT * FROM t WHERE '2024-01-01' <= ts")
    assert low == us(2024, 1, 1)
    _, high = extract_time_range("SELECT * FROM t WHERE '2024-01-01' > ts")
    assert high == us(2024, 1, 1)


def test_equality_window() -> None:
    sql = "SELECT * FROM t WHERE ts = '2024-01-02T03:04:05.123456'"
    low, high = extract_time_range(sql)
    value = us(2024, 1, 2, 3, 4, 5, 123456)
    assert (low, high) == (value, value + 1)


def test_between_is_inclusive() -> None:
    sql = (
        "SELECT * FROM t WHERE ts BETWEEN '2024-01-01 00:00:00' "
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
        "SELECT * FROM t WHERE NOT (ts < '2024-01-01')"
    )
    assert low == us(2024, 1, 1)
    _, high = extract_time_range(
        "SELECT * FROM t WHERE NOT (ts >= '2024-01-01')"
    )
    assert high == us(2024, 1, 1)


def test_not_between_is_wide() -> None:
    sql = "SELECT * FROM t WHERE ts NOT BETWEEN '2024-01-01' AND '2024-02-01'"
    low, high = extract_time_range(sql)
    assert low == 1
    assert high >= now_us()


# NOT distributes over the connective it negates, by De Morgan: a
# time filter negated alongside another predicate cannot cut off the
# rows that still match through it.


def test_not_mixed_conjunction_stays_wide() -> None:
    # NOT (ts >= a AND region = 'us') still matches rows at any ts
    # through region != 'us'
    sql = (
        "SELECT * FROM t WHERE NOT (ts >= '2024-01-01' "
        "AND region = 'us')"
    )
    low, high = extract_time_range(sql)
    assert low == 1
    assert high >= now_us()


def test_not_mixed_conjunction_wrapped_stays_wide() -> None:
    sql = (
        "SELECT * FROM t WHERE NOT "
        "((ts >= '2024-01-01' AND region = 'us'))"
    )
    low, high = extract_time_range(sql)
    assert low == 1
    assert high >= now_us()


def test_not_both_time_bounds_stays_wide() -> None:
    # Rows outside the window can lie on either side of it
    sql = (
        "SELECT * FROM t WHERE NOT "
        "(ts >= '2024-01-01' AND ts < '2024-02-01')"
    )
    low, high = extract_time_range(sql)
    assert low == 1
    assert high >= now_us()


def test_not_distributes_over_and() -> None:
    # NOT (ts >= a AND ts >= b) == ts < a OR ts < b == ts < max(a, b)
    sql = (
        "SELECT * FROM t WHERE NOT (ts >= '2024-01-01' "
        "AND ts >= '2024-02-01')"
    )
    low, high = extract_time_range(sql)
    assert low == 1
    assert high == us(2024, 2, 1)


def test_not_distributes_over_or() -> None:
    # NOT (ts < a OR ts >= b) == ts >= a AND ts < b
    sql = (
        "SELECT * FROM t WHERE NOT (ts < '2024-01-01' "
        "OR ts >= '2024-02-01')"
    )
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 2, 1))


def test_not_mixed_disjunction_bounds_other_side() -> None:
    # NOT (ts >= a OR region = 'us') == ts < a AND region != 'us':
    # every row the SQL matches has ts < a
    sql = (
        "SELECT * FROM t WHERE NOT (ts >= '2024-01-01' "
        "OR region = 'us')"
    )
    low, high = extract_time_range(sql)
    assert low == 1
    assert high == us(2024, 1, 1)


def test_or_takes_widest() -> None:
    sql = "SELECT * FROM t WHERE ts >= '2024-01-01' OR ts >= '2023-06-01'"
    low, _ = extract_time_range(sql)
    assert low == us(2023, 6, 1)


def test_or_with_non_time_column_is_wide() -> None:
    sql = "SELECT * FROM t WHERE ts >= '2024-01-01' OR region = 'us'"
    low, _ = extract_time_range(sql)
    assert low == 1


def test_and_takes_tightest() -> None:
    sql = (
        "SELECT * FROM t WHERE ts >= '2024-01-01' "
        "AND ts >= '2024-02-01' AND ts <= '2024-03-01'"
    )
    low, high = extract_time_range(sql)
    assert (low, high) == (us(2024, 2, 1), us(2024, 3, 2))


def test_contradictory_bounds_stay_ordered() -> None:
    sql = "SELECT * FROM t WHERE ts >= '2024-02-01' AND ts < '2024-01-01'"
    low, high = extract_time_range(sql)
    assert (low, high) == (us(2024, 2, 1), us(2024, 2, 1) + 1)


# A lower bound in the future matches rows that no horizon based on
# now can cover: the end falls back to the far future instead of
# collapsing onto the start.


def test_future_lower_bound_stays_open() -> None:
    sql = "SELECT * FROM t WHERE ts >= '2030-01-01'"
    low, high = extract_time_range(sql)
    assert low == us(2030, 1, 1)
    assert high == us(2100, 1, 1)


def test_future_lower_bound_interval_stays_open() -> None:
    sql = "SELECT * FROM t WHERE ts >= now() + INTERVAL '1 month'"
    before = now_us()
    low, high = extract_time_range(sql)
    after = now_us()
    assert before + 31 * DAY <= low <= after + 31 * DAY
    assert high == us(2100, 1, 1)


def test_future_lower_bound_epoch_stays_open() -> None:
    sql = f"SELECT * FROM t WHERE _timestamp >= {us(2080, 1, 1)}"
    low, high = extract_time_range(sql)
    assert low == us(2080, 1, 1)
    assert high == us(2100, 1, 1)


def test_future_bounds_resolved_is_exact() -> None:
    sql = (
        "SELECT * FROM t WHERE ts >= '2030-01-01' "
        "AND ts < '2031-01-01'"
    )
    assert extract_time_range(sql) == (us(2030, 1, 1), us(2031, 1, 1))


def test_cast_forms() -> None:
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE ts >= CAST('2024-01-01' AS TIMESTAMP)"
    )
    assert low == us(2024, 1, 1)
    _, high = extract_time_range(
        "SELECT * FROM t WHERE CAST('2024-01-01' AS DATE) > ts"
    )
    assert high == us(2024, 1, 1)
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE CAST(ts AS TIMESTAMP) >= '2024-01-01'"
    )
    assert low == us(2024, 1, 1)


def test_typed_literal() -> None:
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE ts >= TIMESTAMP '2024-01-01'"
    )
    assert low == us(2024, 1, 1)


def test_to_timestamp_forms() -> None:
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE ts >= to_timestamp('2024-01-01T00:00:00Z')"
    )
    assert low == us(2024, 1, 1)
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE ts >= to_timestamp('2024-01-01', '%Y-%m-%d')"
    )
    assert low == us(2024, 1, 1)


def test_now_minus_interval() -> None:
    before = now_us()
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE ts >= now() - INTERVAL '24 hours'"
    )
    after = now_us()
    assert before - 24 * HOUR <= low <= after - 24 * HOUR


def test_current_timestamp_minus_interval() -> None:
    before = now_us()
    _, high = extract_time_range(
        "SELECT * FROM t WHERE ts < CURRENT_TIMESTAMP - INTERVAL '30 minutes'"
    )
    after = now_us()
    assert before - 30 * 60_000_000 <= high <= after - 30 * 60_000_000


def test_month_interval_stays_wide() -> None:
    before = now_us()
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE ts >= now() - INTERVAL '1 month'"
    )
    after = now_us()
    assert low <= after - 31 * DAY
    _, high = extract_time_range(
        "SELECT * FROM t WHERE ts < now() + INTERVAL '1 month'"
    )
    assert high >= before + 31 * DAY


def test_multi_part_interval() -> None:
    before = now_us()
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE ts >= now() - INTERVAL '1 day 2 hours'"
    )
    after = now_us()
    assert before - 26 * HOUR <= low <= after - 26 * HOUR


def test_epoch_digit_scales() -> None:
    scales = {
        1704067200: us(2024, 1, 1),
        1704067200000: us(2024, 1, 1),
        1704067200000000: us(2024, 1, 1),
        1704067200000000000: us(2024, 1, 1),
    }
    for value, expected in scales.items():
        low, _ = extract_time_range(
            f"SELECT * FROM t WHERE _timestamp >= {value}"
        )
        assert low == expected, value


def test_epoch_needs_time_like_column() -> None:
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE id >= 1704067200000000"
    )
    assert low == 1


def test_epoch_outside_plausible_window_is_wide() -> None:
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE _timestamp >= 9999999999999999"
    )
    assert low == 1


def test_in_list() -> None:
    sql = (
        "SELECT * FROM t WHERE ts IN "
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
    sql = (
        "SELECT * FROM t WHERE o IN "
        "(SELECT id FROM u WHERE ts >= '2024-01-01')"
    )
    low, _ = extract_time_range(sql)
    assert low == 1


def test_join_on_is_wide() -> None:
    sql = (
        "SELECT a.x FROM a JOIN b ON a.id = b.id "
        "AND b.ts >= '2024-01-01' WHERE a.y = 1"
    )
    low, _ = extract_time_range(sql)
    assert low == 1


def test_having_is_wide() -> None:
    sql = (
        "SELECT region, max(ts) FROM t WHERE region = 'us' "
        "GROUP BY region HAVING ts >= '2024-01-01'"
    )
    low, _ = extract_time_range(sql)
    assert low == 1


def test_union_takes_widest() -> None:
    sql = (
        "SELECT ts FROM a WHERE ts >= '2024-01-01' "
        "UNION ALL SELECT ts FROM b WHERE ts >= '2023-01-01'"
    )
    low, high = extract_time_range(sql)
    assert low == us(2023, 1, 1)
    assert high >= now_us()


def test_comment_is_ignored() -> None:
    sql = (
        "SELECT * FROM t -- WHERE ts >= '2024-01-01'\n"
        "WHERE note = 'hello' AND other = 1"
    )
    low, _ = extract_time_range(sql)
    assert low == 1


def test_escaped_quote_is_not_a_time() -> None:
    sql = "SELECT * FROM t WHERE note = 'it''s 2024-01-01'"
    low, _ = extract_time_range(sql)
    assert low == 1


def test_quoted_column_name() -> None:
    low, _ = extract_time_range(
        'SELECT * FROM t WHERE "My Time" >= 1704067200000000'
    )
    assert low == us(2024, 1, 1)


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
        "SELECT * FROM t WHERE ts >= %(start)s AND ts < %(end)s"
    )
    assert low == 1


def test_function_of_column() -> None:
    low, _ = extract_time_range(
        "SELECT * FROM t WHERE date_trunc('week', ts) >= '2024-01-01'"
    )
    assert low == us(2024, 1, 1)


def test_never_raises() -> None:
    queries = (
        "",
        "NOT VALID SQL !!!",
        "SELECT * FROM t WHERE (((",
        "SELECT * FROM t WHERE ts >= '2024-01-01'; SELECT 1",
    )
    for sql in queries:
        low, high = extract_time_range(sql)
        assert 1 <= low < high, sql


def test_lowercase_keywords() -> None:
    sql = (
        "select * from t where ts >= '2024-01-01' "
        "and ts < '2024-02-01'"
    )
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 2, 1))


# Queries as Superset sends them. Its get_time_filter() builds
# `col >= <text literal>` / `col < <text literal>` with literals from
# dttm_sql_literal(): the engine spec's convert_dttm() output (the
# DataFusion spec, OpenObserve's engine, renders TIMESTAMP '...'
# literals), a bare epoch integer for a python_date_format of
# epoch_s/epoch_ms/epoch_us, or a strftime string; the query is compiled
# with literal_binds against this dialect. These pin those shapes.


def test_superset_compiled_time_filter() -> None:
    # The full Superset path: a chart query compiled through this
    # dialect with literal_binds, as compile_sqla_query() does.
    col: sa.ColumnClause[Any] = sa.column("_timestamp")
    query = (
        sa.select(sa.func.count())
        .select_from(sa.table("logs"))
        .where(
            col >= sa.text("TIMESTAMP '2024-01-01 00:00:00.000000'"),
            col < sa.text("TIMESTAMP '2024-02-01 00:00:00.000000'"),
        )
    )
    engine = create_engine(
        "openobserve+rest://user:pass@localhost:5080/default"
    )
    sql = str(query.compile(engine, compile_kwargs={"literal_binds": True}))
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 2, 1))


def test_superset_true_prefix() -> None:
    # get_time_filter() wraps the clauses in and_(True, ...)
    sql = (
        "SELECT count(*) FROM logs WHERE true AND _timestamp >= "
        "TIMESTAMP '2024-01-01 00:00:00.000000' AND _timestamp < "
        "TIMESTAMP '2024-02-01 00:00:00.000000'"
    )
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 2, 1))


def test_superset_date_typed_literal() -> None:
    sql = "SELECT count(*) FROM logs WHERE d >= DATE '2024-01-01'"
    low, _ = extract_time_range(sql)
    assert low == us(2024, 1, 1)


def test_superset_strftime_fallback_literal() -> None:
    sql = "SELECT count(*) FROM logs WHERE ts >= '2024-01-01 00:00:00'"
    low, _ = extract_time_range(sql)
    assert low == us(2024, 1, 1)


def test_superset_epoch_us_literals() -> None:
    # python_date_format="epoch_us" on _timestamp: bare epoch integers
    sql = (
        "SELECT count(*) FROM logs WHERE _timestamp >= 1704067200000000 "
        "AND _timestamp < 1706745600000000"
    )
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 2, 1))


def test_superset_grain_wrapped_filter_column() -> None:
    # A TEMPORAL_RANGE adhoc filter with a time grain wraps the column
    sql = (
        "SELECT count(*) FROM logs WHERE "
        "DATE_TRUNC('day', _timestamp) >= "
        "TIMESTAMP '2024-01-01 00:00:00.000000'"
    )
    low, high = extract_time_range(sql)
    assert low == us(2024, 1, 1)
    assert high >= now_us()


def test_superset_epoch_wrapped_filter_column() -> None:
    # An epoch python_date_format column is wrapped with epoch_to_dttm
    sql = (
        "SELECT count(*) FROM logs WHERE from_unixtime(_timestamp) >= "
        "TIMESTAMP '2024-01-01 00:00:00'"
    )
    low, _ = extract_time_range(sql)
    assert low == us(2024, 1, 1)


# A day-granular literal, like '2024-01-01', covers its whole day on
# the inclusive upper side: the range ends at the next midnight.


def test_date_only_equality_covers_whole_day() -> None:
    sql = "SELECT * FROM t WHERE day = '2024-01-01'"
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 1, 2))


def test_date_only_upper_bound_ends_at_next_midnight() -> None:
    sql = "SELECT * FROM t WHERE day <= '2024-01-01'"
    low, high = extract_time_range(sql)
    assert low == 1
    assert high == us(2024, 1, 2)


def test_date_only_between_covers_final_day() -> None:
    sql = "SELECT * FROM t WHERE day BETWEEN '2024-01-01' AND '2024-01-02'"
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 1, 3))


def test_date_only_in_covers_final_day() -> None:
    sql = "SELECT * FROM t WHERE day IN ('2024-01-01', '2024-01-02')"
    assert extract_time_range(sql) == (us(2024, 1, 1), us(2024, 1, 3))


# A date-shaped string compared to an ordinary column is just a string
# comparison: it must not narrow the range.


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


def test_time_function_of_ordinary_column_still_bounds() -> None:
    # The function marks the expression as time, even when the column
    # inside it has an ordinary name
    sql = "SELECT * FROM t WHERE from_unixtime(start) >= '2024-01-01'"
    low, _ = extract_time_range(sql)
    assert low == us(2024, 1, 1)


def test_unknown_function_of_ordinary_column_is_wide() -> None:
    sql = "SELECT * FROM t WHERE lower(region) >= '2024-01-01'"
    low, _ = extract_time_range(sql)
    assert low == 1
