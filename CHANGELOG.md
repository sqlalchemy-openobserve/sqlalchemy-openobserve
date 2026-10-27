# Changelog

## Unreleased

- Implement the OpenObserve HTTP client for search and stream metadata, with
  `ssl` and TLS certificate `verify` options.
- Target Apache Superset 7 only. Superset 6.x and earlier pin SQLAlchemy
  below 2.0, so pip cannot install them alongside this package.
