from unittest.mock import Mock

import pytest
import requests

from sqlalchemy_openobserve import client as client_module
from sqlalchemy_openobserve.client import OpenObserveClient, OpenObserveError


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
    session.request.return_value.json.return_value = {"hits": []}

    result = client.search("select * from logs", 10, 20)

    assert result == {"hits": []}
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
            }
        },
    )


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
    )


def test_stream_schema_quotes_stream_name(
    session: Mock, client: OpenObserveClient
) -> None:
    session.request.return_value.json.return_value = {"schema": []}

    assert client.stream_schema("app/logs") == {"schema": []}
    session.request.assert_called_once_with(
        "GET",
        "https://observe.example:5080/api/my%20org/streams/app%2Flogs/schema",
        timeout=30,
    )


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


def test_close_closes_http_session(session: Mock) -> None:
    client = OpenObserveClient("localhost", 5080, "default", "user", "pass")

    client.close()

    session.close.assert_called_once_with()
