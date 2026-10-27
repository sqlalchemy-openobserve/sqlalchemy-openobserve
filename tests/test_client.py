from unittest.mock import Mock

import pytest
import requests

from sqlalchemy_openobserve import client as client_module
from sqlalchemy_openobserve.client import (
    OpenObserveClient,
    OpenObserveError,
    QueryError,
)


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> Mock:
    session = Mock()
    monkeypatch.setattr(client_module.requests, "Session", lambda: session)
    return session


@pytest.fixture
def client(session: Mock) -> OpenObserveClient:
    return OpenObserveClient("observe.example", 5080, "my org", "user", "pass")


def test_search_posts_query_with_basic_auth(
    session: Mock, client: OpenObserveClient
) -> None:
    session.request.return_value.json.return_value = {
        "columns": ["level"],
        "hits": [["info"]],
        "is_partial": True,
        "total": 1,
    }

    result = client.search("select * from logs", 10, 20)

    assert result == {
        "columns": ["level"],
        "rows": [["info"]],
        "is_partial": True,
    }
    assert session.auth == ("user", "pass")
    session.request.assert_called_once_with(
        "POST",
        "https://observe.example:5080/api/my%20org/_search",
        timeout=30,
        json={
            "query": {
                "sql": "select * from logs",
                "start_time": 10,
                "end_time": 20,
                "from": 0,
                "size": 1000,
                "query_type": "table",
            }
        },
    )


def test_search_raises_query_error_with_http_400_message(
    session: Mock, client: OpenObserveClient
) -> None:
    response = Mock()
    response.status_code = 400
    response.json.return_value = {
        "code": 400,
        "message": "invalid SQL syntax",
    }
    response.raise_for_status.side_effect = requests.HTTPError(
        "400 Bad Request", response=response
    )
    session.request.return_value = response

    with pytest.raises(QueryError, match="invalid SQL syntax"):
        client.search("select * from", 10, 20)


def test_search_does_not_decode_plain_text_401_body(
    session: Mock, client: OpenObserveClient
) -> None:
    response = Mock()
    response.status_code = 401
    response.raise_for_status.side_effect = requests.HTTPError(
        "401 Unauthorized", response=response
    )
    session.request.return_value = response

    with pytest.raises(OpenObserveError, match="401 Unauthorized"):
        client.search("select * from logs", 10, 20)

    response.json.assert_not_called()


def test_list_streams_returns_stream_names(
    session: Mock, client: OpenObserveClient
) -> None:
    session.request.return_value.json.return_value = {
        "list": [{"name": "app_logs"}, {"name": "audit"}],
        "total": 2,
    }

    assert client.list_streams() == ["app_logs", "audit"]
    session.request.assert_called_once_with(
        "GET",
        "https://observe.example:5080/api/my%20org/streams",
        timeout=30,
        params={"type": "logs"},
    )


def test_list_streams_accepts_stream_type(
    session: Mock, client: OpenObserveClient
) -> None:
    session.request.return_value.json.return_value = {
        "list": [{"name": "cpu"}],
        "total": 1,
    }

    assert client.list_streams("metrics") == ["cpu"]

    session.request.assert_called_once_with(
        "GET",
        "https://observe.example:5080/api/my%20org/streams",
        timeout=30,
        params={"type": "metrics"},
    )


def test_stream_schema_quotes_stream_name(
    session: Mock, client: OpenObserveClient
) -> None:
    session.request.return_value.json.return_value = {
        "schema": [
            {"name": "_timestamp", "type": "Int64", "nullable": False},
            {"name": "message", "type": "Utf8", "nullable": True},
        ],
        "total_fields": 2,
    }

    assert client.stream_schema("app/logs") == [
        {"name": "_timestamp", "type": "Int64"},
        {"name": "message", "type": "Utf8"},
    ]
    session.request.assert_called_once_with(
        "GET",
        "https://observe.example:5080/api/my%20org/streams/app%2Flogs/schema",
        timeout=30,
        params={"type": "logs"},
    )


def test_stream_schema_accepts_stream_type(
    session: Mock, client: OpenObserveClient
) -> None:
    session.request.return_value.json.return_value = {
        "schema": [{"name": "cpu", "type": "Float64"}]
    }

    assert client.stream_schema("cpu", "traces") == [
        {"name": "cpu", "type": "Float64"}
    ]

    session.request.assert_called_once_with(
        "GET",
        "https://observe.example:5080/api/my%20org/streams/cpu/schema",
        timeout=30,
        params={"type": "traces"},
    )


def test_stream_schema_rejects_invalid_fields(
    session: Mock, client: OpenObserveClient
) -> None:
    session.request.return_value.json.return_value = {
        "schema": [{"name": "message"}]
    }

    with pytest.raises(OpenObserveError, match="invalid field"):
        client.stream_schema("app_logs")


def test_http_errors_are_reported_as_client_errors(
    session: Mock, client: OpenObserveClient
) -> None:
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError("unauthorized")
    session.request.return_value = response

    with pytest.raises(OpenObserveError, match="unauthorized") as error:
        client.list_streams()

    assert isinstance(error.value.__cause__, requests.HTTPError)


def test_non_list_stream_response_is_rejected(
    session: Mock, client: OpenObserveClient
) -> None:
    session.request.return_value.json.return_value = {"list": "invalid"}

    with pytest.raises(OpenObserveError, match="must contain a list"):
        client.list_streams()


def test_ping_checks_connection_by_listing_streams(
    monkeypatch: pytest.MonkeyPatch, client: OpenObserveClient
) -> None:
    list_streams = Mock(return_value=["app_logs"])
    monkeypatch.setattr(client, "list_streams", list_streams)

    assert client.ping() is None

    list_streams.assert_called_once_with()


def test_ping_propagates_stream_list_errors(
    monkeypatch: pytest.MonkeyPatch, client: OpenObserveClient
) -> None:
    list_streams = Mock(side_effect=OpenObserveError("connection failed"))
    monkeypatch.setattr(client, "list_streams", list_streams)

    with pytest.raises(OpenObserveError, match="connection failed"):
        client.ping()


def test_ssl_can_be_disabled(session: Mock) -> None:
    client = OpenObserveClient(
        "localhost", 5080, "default", "user", "pass", ssl=False
    )
    session.request.return_value.json.return_value = {}

    client.search("select 1", 1, 2)

    assert (
        session.request.call_args.args[1]
        == "http://localhost:5080/api/default/_search"
    )


@pytest.mark.parametrize("verify", [False, "C:/certs/openobserve-ca.pem"])
def test_tls_verification_can_be_configured(
    session: Mock, verify: bool | str
) -> None:
    OpenObserveClient(
        "observe.example",
        5080,
        "default",
        "user",
        "pass",
        verify=verify,
    )

    assert session.verify == verify


def test_close_closes_http_session(session: Mock) -> None:
    client = OpenObserveClient("localhost", 5080, "default", "user", "pass")

    client.close()

    session.close.assert_called_once_with()
