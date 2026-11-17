"""Fixtures for tests against a live OpenObserve

Without the server from docker-compose.yml, every test here is skipped.
"""

import contextlib
import time
import uuid
from collections.abc import Iterator

import pytest
import requests
import sqlalchemy as sa

from sqlalchemy_openobserve import dbapi

HOST = "127.0.0.1"
PORT = 5080
ORGANIZATION = "default"
EMAIL = "root@example.com"
PASSWORD = "Complexpass#123"
URL = (
    f"openobserve://root%40example.com:Complexpass%23123@{HOST}:{PORT}"
    "/default?ssl=false"
)

ROWS = 1500
LEVELS = ["info", "info", "warn", "error"]
SERVICES = ["api", "web", 'it\'s 100% "odd"']


def _api(path: str) -> str:
    return f"http://{HOST}:{PORT}/api/{ORGANIZATION}/{path}"


@pytest.fixture(scope="session")
def stream() -> Iterator[str]:
    """A stream of known rows, deleted afterwards.

    Row i is ``i`` seconds old, with level ``LEVELS[i % 4]``, service
    ``SERVICES[i % 3]``, status 200 for even i and 500 for odd i, duration
    ``i / 2``, message ``request i`` and a user_id on every fifth.
    """
    name = f"it_{uuid.uuid4().hex[:8]}"
    auth = (EMAIL, PASSWORD)
    now = int(time.time() * 1_000_000)
    rows = [
        {
            "_timestamp": now - i * 1_000_000,
            "level": LEVELS[i % 4],
            "service": SERVICES[i % 3],
            "status": 200 if i % 2 == 0 else 500,
            "duration_ms": i / 2,
            "message": f"request {i}",
            **({"user_id": f"u{i}"} if i % 5 == 0 else {}),
        }
        for i in range(ROWS)
    ]
    try:
        response = requests.post(
            _api(f"{name}/_json"), json=rows, auth=auth, timeout=60
        )
    except (requests.ConnectionError, requests.Timeout):
        pytest.skip(
            "OpenObserve is not running; start it with: docker compose up -d"
        )
    response.raise_for_status()

    connection = dbapi.connect(
        host=HOST,
        port=PORT,
        organization=ORGANIZATION,
        email=EMAIL,
        password=PASSWORD,
        ssl=False,
    )
    deadline = time.monotonic() + 60
    try:
        while True:
            cursor = connection.cursor()
            try:
                cursor.execute(f"select count(*) from {name}")
                if cursor.fetchone() == (ROWS,):
                    break
            except dbapi.Error:
                pass
            if time.monotonic() > deadline:
                pytest.fail("the rows did not become searchable in time")
            time.sleep(1)
        yield name
    finally:
        connection.close()
        with contextlib.suppress(requests.RequestException):
            requests.delete(
                _api(f"streams/{name}"),
                params={"type": "logs"},
                auth=auth,
                timeout=60,
            )


@pytest.fixture
def cursor(stream: str) -> Iterator[dbapi.Cursor]:
    connection = dbapi.connect(
        host=HOST,
        port=PORT,
        organization=ORGANIZATION,
        email=EMAIL,
        password=PASSWORD,
        ssl=False,
    )
    yield connection.cursor()
    connection.close()


@pytest.fixture(scope="session")
def engine(stream: str) -> Iterator[sa.Engine]:
    engine = sa.create_engine(URL, pool_pre_ping=True)
    yield engine
    engine.dispose()
