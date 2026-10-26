"""HTTP client for the OpenObserve API"""

from typing import Any, Literal
from urllib.parse import quote

import requests

StreamType = Literal["logs", "metrics", "traces"]


class OpenObserveError(Exception):
    """An OpenObserve request failed or returned an invalid response."""


class QueryError(OpenObserveError):
    """OpenObserve rejected a query."""


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
                    "query_type": "table",
                }
            },
        )
        if not isinstance(result, dict):
            raise OpenObserveError(
                "OpenObserve search response must be an object"
            )
        if "error" in result:
            raise QueryError(str(result["error"]))
        return {
            "columns": result.get("columns", []),
            "rows": result.get("hits", result.get("rows", [])),
            "is_partial": result.get("is_partial", False),
        }

    def list_streams(self, stream_type: StreamType = "logs") -> list[str]:
        result = self._request("GET", "streams", params={"type": stream_type})
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

    def ping(self) -> None:
        """Check connectivity by listing streams in the organization."""
        self.list_streams()

    def stream_schema(
        self, stream: str, stream_type: StreamType = "logs"
    ) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            f"streams/{quote(stream, safe='')}/schema",
            params={"type": stream_type},
        )
        if not isinstance(result, dict) or not isinstance(
            result.get("schema"), list
        ):
            raise OpenObserveError(
                "OpenObserve stream schema response must contain a list"
            )
        fields: list[dict[str, Any]] = []
        for field in result["schema"]:
            if (
                not isinstance(field, dict)
                or not isinstance(field.get("name"), str)
                or not isinstance(field.get("type"), str)
            ):
                raise OpenObserveError(
                    "OpenObserve stream schema contains an invalid field"
                )
            fields.append({"name": field["name"], "type": field["type"]})
        return fields

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()
