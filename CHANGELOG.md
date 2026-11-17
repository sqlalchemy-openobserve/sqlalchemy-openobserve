# Changelog

## Unreleased

- Cursor results now describe their columns. `type_code` is the Arrow type
  of a stream field passed through unchanged, and otherwise the type of the
  first non-null value. A result with no rows still has a description, read
  from the query's SELECT list; an unnamed expression is named by its SQL
  text, which the server may spell differently, and a `*` over a CTE,
  subquery or join gives none. The `_timestamp` column the server prepends to
  a query that did not select it is dropped.
- The dialect maps `openobserve://email:password@host:port/organization`
  (with `?ssl=false` and `?verify=`) onto `connect()`, pings with the
  client instead of `SELECT 1`, and reflects logs streams as tables in a
  single `default` schema that compiled SQL leaves out. `_timestamp`
  reflects as the `BIGINT` it is (microseconds since the epoch).
- Implement the DBAPI `connect()`, `Connection` and `Cursor`, with the
  PEP 249 type objects and constructors. Once the statements helpers are
  implemented, a query runs as one OpenObserve search over the time range
  its SQL implies, with no row cap beyond the SQL's own LIMIT.
- `OpenObserveClient.search()` rejects malformed columns and rows and
  returns the server's `function_error` messages.
- `extract_time_range()` works out the time range of a search from the
  time comparisons in its SQL, replacing the unimplemented stub.
- Package skeleton. No working functionality yet.
- Implement the OpenObserve HTTP client for search and stream metadata, with
  `ssl` and TLS certificate `verify` options.
- Target Apache Superset 7 only. Superset 6.x and earlier pin SQLAlchemy
  below 2.0, so pip cannot install them alongside this package.
