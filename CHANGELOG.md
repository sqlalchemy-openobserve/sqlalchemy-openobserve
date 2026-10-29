# Changelog

## Unreleased

- `extract_time_range()` works out the time range of a search from the
  time comparisons in its SQL, replacing the unimplemented stub.
- Package skeleton. No working functionality yet.
- Implement the OpenObserve HTTP client for search and stream metadata, with
  `ssl` and TLS certificate `verify` options.
- Target Apache Superset 7 only. Superset 6.x and earlier pin SQLAlchemy
  below 2.0, so pip cannot install them alongside this package.
