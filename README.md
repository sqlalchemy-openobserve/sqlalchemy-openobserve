# sqlalchemy-openobserve

A SQLAlchemy dialect and DBAPI driver for [OpenObserve](https://openobserve.ai),
built so that Apache Superset can connect to it.

> **Status: alpha.** 0.1.0 is the first version that works. It is read-only
> and covers logs streams.

## What this is

OpenObserve is queried with SQL over an HTTP API. This package exposes it to
SQLAlchemy so tools built on SQLAlchemy can read from it. It is read-only.
The client checks connectivity by listing streams; a `SELECT 1` probe is not
valid because OpenObserve queries must reference a stream.

- Each logs stream is a table in a single schema called `default`, which
  compiled SQL leaves out.
- The time range of a search comes from `_timestamp` comparisons in the SQL.
  Any side they do not bound is left open, from just after the epoch to 25
  hours ahead of now. The driver adds no row cap of its own, so the SQL's
  `LIMIT` decides how many rows come back (but see the known limit on
  `ORDER BY ... LIMIT` in the
  [CHANGELOG](https://github.com/sqlalchemy-openobserve/sqlalchemy-openobserve/blob/main/CHANGELOG.md)).
- The server adds `_timestamp` to non-aggregate queries that did not select
  it, and the driver drops it again.

## Install

```bash
pip install sqlalchemy-openobserve
```

## Connection string

```
openobserve://{email}:{password}@{host}:{port}/{organization}
```

- The username is your OpenObserve email. Write its `@` as `%40`, for example
  `root%40example.com`, and percent-encode any special characters in the
  password the same way.
- HTTPS is used by default. Add `?ssl=false` for a plain HTTP server such as
  a local one on port 5080.
- The port defaults to 5080, so give it explicitly for an HTTPS server on 443.
- `?verify=false` turns off TLS certificate checks, and
  `?verify=/path/to/ca.pem` checks against your own CA bundle.
- `openobserve+rest://` is the same thing with the driver named explicitly.

## Example

`_timestamp` holds the event time as microseconds since the Unix epoch.

```python
from sqlalchemy import create_engine, text

engine = create_engine(
    "openobserve://root%40example.com:password@localhost:5080/default"
    "?ssl=false"
)
with engine.connect() as connection:
    result = connection.execute(
        text("SELECT level, count(*) AS n FROM app_logs GROUP BY level")
    )
    for level, n in result:
        print(level, n)
```

## Requirements

Python 3.11 or newer, SQLAlchemy 2.0.52 or newer in the 2.0 series.

## Superset compatibility

This package targets Apache Superset 7. Superset also needs a DB engine spec
for OpenObserve, which will live in the Apache Superset repository, not here,
and is not in Superset yet. The two have been tested together on Superset's
`master` branch plus that engine spec, from the `openobserve-engine-spec`
branch of the `sqlalchemy-openobserve/superset` fork.
Superset 6.x and earlier are not supported: they pin SQLAlchemy below 2.0, and
this package needs SQLAlchemy 2.0.52 or newer, so pip cannot install the two
together.

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
