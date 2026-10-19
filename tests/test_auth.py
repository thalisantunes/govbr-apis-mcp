"""Tests for the OAuth2 client-credentials flow and the ways its token endpoint fails."""

import base64
import dataclasses
import threading
import time

import httpx
import pytest

from govbr_apis_mcp import auth
from govbr_apis_mcp.catalog import Catalog

TOKEN_URL = "https://gateway.apiserpro.serpro.gov.br/token"


@pytest.fixture
def entry(monkeypatch):
    """serpro-consulta-cnpj with credentials set and an empty token cache."""
    monkeypatch.setenv("SERPRO_CNPJ_CLIENT_ID", "the-id")
    monkeypatch.setenv("SERPRO_CNPJ_CLIENT_SECRET", "the-secret")
    monkeypatch.setattr(auth, "_token_cache", {})
    return Catalog.load().get("serpro-consulta-cnpj")


def token_client(responder):
    """A client whose token endpoint is `responder`; returns (client, requests)."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responder(request)

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


def test_token_is_requested_with_basic_auth_and_cached(entry):
    client, requests = token_client(
        lambda request: httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    assert auth.build_headers(entry, client) == {"Authorization": "Bearer tok"}
    assert auth.build_headers(entry, client) == {"Authorization": "Bearer tok"}
    assert len(requests) == 1
    sent = requests[0]
    assert (sent.method, str(sent.url)) == ("POST", TOKEN_URL)
    assert (
        sent.headers["authorization"] == "Basic " + base64.b64encode(b"the-id:the-secret").decode()
    )
    assert sent.content == b"grant_type=client_credentials"


def test_short_lived_tokens_are_not_cached(entry):
    client, requests = token_client(
        lambda request: httpx.Response(200, json={"access_token": "tok", "expires_in": 30})
    )
    auth.build_headers(entry, client)
    auth.build_headers(entry, client)
    assert len(requests) == 2


def test_missing_oauth_env_vars_raise_before_any_request(entry, monkeypatch):
    monkeypatch.delenv("SERPRO_CNPJ_CLIENT_SECRET")
    client, requests = token_client(lambda request: httpx.Response(200))
    with pytest.raises(auth.MissingCredentialsError, match="SERPRO_CNPJ_CLIENT_SECRET"):
        auth.build_headers(entry, client)
    assert requests == []


@pytest.mark.parametrize("status", [302, 401, 403, 500, 503])
def test_token_http_failure_is_a_token_error_without_the_body(status, entry):
    client, _ = token_client(
        lambda request: httpx.Response(
            status,
            text="LEAKY-BODY client_secret=the-secret",
            headers={"Location": "https://elsewhere.gov.br/x?k=LEAKY-LOCATION"},
        )
    )
    with pytest.raises(auth.TokenError) as excinfo:
        auth.build_headers(entry, client)
    assert str(excinfo.value) == f"serpro-consulta-cnpj: token endpoint returned HTTP {status}"


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"text": "<html>login</html>"}, id="html"),
        pytest.param({"json": {"error": "invalid_client"}}, id="no-access-token"),
        pytest.param({"json": {"access_token": None}}, id="null-token"),
        pytest.param({"json": {"access_token": ""}}, id="empty-token"),
        pytest.param({"json": ["access_token"]}, id="not-an-object"),
        pytest.param({"json": {"access_token": "  "}}, id="blank-token"),
    ],
)
def test_token_response_without_a_usable_token(kwargs, entry):
    client, _ = token_client(lambda request: httpx.Response(200, **kwargs))
    with pytest.raises(auth.TokenError) as excinfo:
        auth.build_headers(entry, client)
    assert str(excinfo.value) == "serpro-consulta-cnpj: token endpoint returned no access_token"


def test_token_transport_failure_is_a_token_error(entry):
    def timeout(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    client, _ = token_client(timeout)
    with pytest.raises(auth.TokenError) as excinfo:
        auth.build_headers(entry, client)
    assert str(excinfo.value) == "serpro-consulta-cnpj: token request failed (ConnectTimeout)"


def test_failed_token_request_is_not_cached(entry):
    answers = iter(
        [httpx.Response(401), httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})]
    )
    client, requests = token_client(lambda request: next(answers))
    with pytest.raises(auth.TokenError):
        auth.build_headers(entry, client)
    assert auth.build_headers(entry, client) == {"Authorization": "Bearer tok"}
    assert len(requests) == 2


def test_oauth_env_vars_are_stripped(entry, monkeypatch):
    monkeypatch.setenv("SERPRO_CNPJ_CLIENT_ID", "the-id\n")
    monkeypatch.setenv("SERPRO_CNPJ_CLIENT_SECRET", " the-secret \r\n")
    client, requests = token_client(
        lambda request: httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    auth.build_headers(entry, client)
    expected = "Basic " + base64.b64encode(b"the-id:the-secret").decode()
    assert requests[0].headers["authorization"] == expected


def test_whitespace_only_env_var_counts_as_unset(entry, monkeypatch):
    monkeypatch.setenv("SERPRO_CNPJ_CLIENT_SECRET", " \n")
    assert auth.credentials_status(entry)["ready"] is False
    client, requests = token_client(lambda request: httpx.Response(200))
    with pytest.raises(auth.MissingCredentialsError, match="SERPRO_CNPJ_CLIENT_SECRET"):
        auth.build_headers(entry, client)
    assert requests == []


def test_token_that_cannot_travel_in_a_header_is_refused(entry):
    client, requests = token_client(
        lambda request: httpx.Response(
            200, json={"access_token": "LEAKY\nTOKEN", "expires_in": 3600}
        )
    )
    for _ in range(2):
        with pytest.raises(auth.TokenError) as excinfo:
            auth.build_headers(entry, client)
        assert str(excinfo.value) == (
            "serpro-consulta-cnpj: token endpoint returned an access_token with invalid characters"
        )
    assert len(requests) == 2  # the unusable token was not cached


def test_token_edge_whitespace_is_stripped_before_use_and_caching(entry):
    client, requests = token_client(
        lambda request: httpx.Response(200, json={"access_token": "tok \n", "expires_in": 3600})
    )
    assert auth.build_headers(entry, client) == {"Authorization": "Bearer tok"}
    assert auth.build_headers(entry, client) == {"Authorization": "Bearer tok"}
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("expires_in", "cached"),
    [
        pytest.param(b'"3600.0"', True, id="float-in-a-string"),
        pytest.param(b"3600.5", True, id="float"),
        pytest.param(b"null", False, id="null"),
        pytest.param(b'"soon"', False, id="garbage"),
        pytest.param(b"[]", False, id="list"),
        pytest.param(b"NaN", False, id="nan"),
        pytest.param(b"Infinity", False, id="infinity"),
        pytest.param(b"1e400", False, id="overflow"),
    ],
)
def test_odd_expires_in_never_rejects_a_good_token(expires_in, cached, entry):
    """The lifetime only decides caching: if it is unusable the token is used once."""
    body = b'{"access_token": "tok", "expires_in": ' + expires_in + b"}"
    client, requests = token_client(
        lambda request: httpx.Response(
            200, content=body, headers={"content-type": "application/json"}
        )
    )
    assert auth.build_headers(entry, client) == {"Authorization": "Bearer tok"}
    assert auth.build_headers(entry, client) == {"Authorization": "Bearer tok"}
    assert len(requests) == (1 if cached else 2)


def test_credentials_that_cannot_be_encoded_are_a_token_error(entry, monkeypatch):
    monkeypatch.setenv("SERPRO_CNPJ_CLIENT_SECRET", "se\udcffcret")  # a non-UTF-8 byte
    client, requests = token_client(lambda request: httpx.Response(200))
    with pytest.raises(auth.TokenError) as excinfo:
        auth.build_headers(entry, client)
    assert str(excinfo.value) == (
        "serpro-consulta-cnpj: client credentials contain characters that cannot be encoded"
    )
    assert requests == []


def test_malformed_token_url_is_a_token_error(entry):
    config = {**entry.auth_config, "token_url": "https://gateway.apiserpro.serpro.gov.br:x/token"}
    broken = dataclasses.replace(entry, auth_config=config)
    client, requests = token_client(lambda request: httpx.Response(200))
    with pytest.raises(auth.TokenError) as excinfo:
        auth.build_headers(broken, client)
    assert str(excinfo.value) == "serpro-consulta-cnpj: token_url is not a valid URL"
    assert requests == []


def test_concurrent_callers_share_one_token_request(entry):
    """Single flight: on a cold cache, parallel callers wait for the first request."""
    release = threading.Event()

    def slow_token_endpoint(request):
        release.wait(timeout=5)
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    client, requests = token_client(slow_token_endpoint)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(auth.build_headers(entry, client)))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5
    while not requests and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)  # time for the other callers to reach the endpoint, if the code lets them
    release.set()
    for thread in threads:
        thread.join(timeout=5)
    assert len(requests) == 1
    assert results == [{"Authorization": "Bearer tok"}] * 8
