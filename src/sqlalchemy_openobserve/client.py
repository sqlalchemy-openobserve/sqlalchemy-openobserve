"""HTTP client for the OpenObserve API"""

from typing import Any


class OpenObserveClient:
    """Talks to one OpenObserve organization over HTTP."""

    def __init__(
        self,
        host: str,
        port: int,
        organization: str,
        email: str,
        password: str,
    ) -> None:
        self.host = host
        self.port = port
        self.organization = organization
        self.email = email
        self.password = password

    def search(self, sql: str) -> dict[str, Any]:
        raise NotImplementedError

    def list_streams(self) -> list[str]:
        raise NotImplementedError

    def stream_schema(self, stream: str) -> dict[str, Any]:
        raise NotImplementedError
