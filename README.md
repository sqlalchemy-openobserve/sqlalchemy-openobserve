# sqlalchemy-openobserve

A SQLAlchemy dialect and DBAPI driver for [OpenObserve](https://openobserve.ai),
built so that Apache Superset can connect to it.

> **Status: pre-alpha.** Nothing here works yet. Do not install it.

## What this will be

OpenObserve is queried with SQL over an HTTP API. This package exposes it to
SQLAlchemy so tools built on SQLAlchemy can read from it. It is read-only.
The client checks connectivity by listing streams; a `SELECT 1` probe is not
valid because OpenObserve queries must reference a stream.

The Superset side (the DB engine spec) will live in the Apache Superset
repository, not here.

## Planned connection string

```
openobserve://{email}:{password}@{host}:{port}/{organization}
```

- The username is your OpenObserve email. Write its `@` as `%40`, for example
  `root%40example.com`, and percent-encode any special characters in the
  password the same way.
- HTTPS will be used by default, with `?ssl=false` for a plain HTTP server
  such as a local one on port 5080.
- `openobserve+rest://` is the same thing with the driver named explicitly.

## Requirements

Python 3.11 or newer, SQLAlchemy 2.0.52 or newer in the 2.0 series.

## Development

```bash
pip install -e ".[dev]"
```

The checks that CI runs on every pull request:

```bash
ruff check .
ruff format --check .
mypy --strict src
pytest
```

### A local OpenObserve

To try code against a real server, start OpenObserve in Docker and load the
sample data:

```bash
docker compose up -d
python3 scripts/load_sample_data.py
```

This gives you a logs stream named `app_logs` with 3,000 rows from the last
48 hours. Running the script again adds another 3,000 rows.

| | |
|---|---|
| URL | http://localhost:5080 |
| Organization | `default` |
| Email | `root@example.com` |
| Password | `Complexpass#123` |

The web UI is at the same URL. Stop the server, and delete its data, with
`docker compose down`.

## Licence

Apache-2.0. This package is an HTTP client and contains no OpenObserve
server code.
