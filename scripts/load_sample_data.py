"""Load sample log rows into the local OpenObserve from docker-compose.yml"""

import base64
import json
import random
import time
import urllib.error
import urllib.request

BASE_URL = "http://localhost:5080"
ORGANIZATION = "default"
STREAM = "app_logs"
EMAIL = "root@example.com"
PASSWORD = "Complexpass#123"

ROWS = 3000
HOURS = 48
MICROSECONDS = 1_000_000


def wait_until_ready(timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/healthz", timeout=5):
                return
        except (urllib.error.URLError, OSError):
            if time.monotonic() > deadline:
                raise SystemExit(
                    f"OpenObserve is not reachable at {BASE_URL}. "
                    "Start it with: docker compose up -d"
                ) from None
            time.sleep(1)


def sample_rows() -> list[dict[str, object]]:
    rng = random.Random(7)
    now = int(time.time() * MICROSECONDS)
    rows: list[dict[str, object]] = []
    for i in range(ROWS):
        row: dict[str, object] = {
            "_timestamp": now - rng.randint(0, HOURS * 3600) * MICROSECONDS,
            "level": rng.choice(["info", "info", "info", "warn", "error"]),
            # The last value exercises quoting and escaping
            "service": rng.choice(
                ["api", "web", "worker", 'it\'s 100% "odd"']
            ),
            "status": rng.choice([200, 200, 200, 404, 500]),
            "duration_ms": round(rng.random() * 900, 3),
            "message": f"request {i} done",
        }
        if i % 5 == 0:
            row["user_id"] = f"u{i % 37}"
        rows.append(row)
    return rows


def main() -> None:
    wait_until_ready()
    credentials = base64.b64encode(f"{EMAIL}:{PASSWORD}".encode()).decode()
    request = urllib.request.Request(
        f"{BASE_URL}/api/{ORGANIZATION}/{STREAM}/_json",
        data=json.dumps(sample_rows()).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {credentials}",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        print(response.read().decode())


if __name__ == "__main__":
    main()
