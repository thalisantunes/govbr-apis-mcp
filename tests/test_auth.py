"""Tests for the OAuth2 client-credentials flow and the ways its token endpoint fails."""

import base64

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
