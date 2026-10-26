"""HTTP client for the OpenObserve API"""

from typing import Any
from urllib.parse import quote

import requests


class OpenObserveError(Exception):
    """An OpenObserve request failed or returned an invalid response."""


class OpenObserveClient:
    """Talks to one OpenObserve organization over HTTP."""

    _timeout = 30

    def __init__(
        self,
        host: str,
        port: int,
        organization: str,
        email: str,
        password: str,
        ssl: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.organization = organization
        self.email = email
        self.password = password
        scheme = "https" if ssl else "http"
        url_host = (
            f"[{host}]" if ":" in host and not host.startswith("[") else host
        )
        self._base_url = f"{scheme}://{url_host}:{port}"
        self._session = requests.Session()
        self._session.auth = (email, password)
        self._session.headers.update({"Accept": "application/json"})

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = (
            f"{self._base_url}/api/{quote(self.organization, safe='')}/{path}"
        )
        try:
            response = self._session.request(
                method, url, timeout=self._timeout, **kwargs
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise OpenObserveError(
                f"OpenObserve {method} request to {path} failed: {exc}"
            ) from exc

    def search(
        self, sql: str, start_time: int, end_time: int, size: int = 1000
    ) -> dict[str, Any]:
        """Run a query. Times are microseconds since the epoch."""
        result = self._request(
            "POST",
            "_search",
            json={
                "query": {
                    "sql": sql,
                    "start_time": start_time,
                    "end_time": end_time,
                    "from": 0,
                    "size": size,
                }
            },
        )
        if not isinstance(result, dict):
            raise OpenObserveError(
                "OpenObserve search response must be an object"
            )
        return result

    def list_streams(self) -> list[str]:
        result = self._request("GET", "streams")
        if not isinstance(result, dict) or not isinstance(
            result.get("list"), list
        ):
            raise OpenObserveError(
                "OpenObserve streams response must contain a list"
            )

        names: list[str] = []
        for stream in result["list"]:
            if not isinstance(stream, dict) or not isinstance(
                stream.get("name"), str
            ):
                raise OpenObserveError(
                    "OpenObserve streams response contains an invalid stream"
                )
            names.append(stream["name"])
        return names

    def stream_schema(self, stream: str) -> dict[str, Any]:
        result = self._request(
            "GET", f"streams/{quote(stream, safe='')}/schema"
        )
        if not isinstance(result, dict):
            raise OpenObserveError(
                "OpenObserve stream schema response must be an object"
            )
        return result

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()
