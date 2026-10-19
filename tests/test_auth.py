"""Tests for the OAuth2 client-credentials flow and the ways its token endpoint fails."""

import base64
import dataclasses
import threading
import time
import types

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


@pytest.fixture
def cpf_entry(monkeypatch):
    """serpro-consulta-cpf: a second OAuth2 API, with credentials of its own."""
    monkeypatch.setenv("SERPRO_CPF_CLIENT_ID", "cpf-id")
    monkeypatch.setenv("SERPRO_CPF_CLIENT_SECRET", "cpf-secret")
    return Catalog.load().get("serpro-consulta-cpf")


@pytest.fixture
def clock(monkeypatch):
    """A hand-wound monotonic clock for the token cache: age every token with `clock.now += n`."""
    clock = types.SimpleNamespace(now=1000.0)
    monkeypatch.setattr(auth, "time", types.SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def client_id_of(request):
    basic = request.headers["authorization"].removeprefix("Basic ")
    return base64.b64decode(basic).decode().split(":")[0]


def token_of_client(request):
    """A token of its own for every API, named after the client id that asked for it."""
    token = f"tok-{client_id_of(request)}"
    return httpx.Response(200, json={"access_token": token, "expires_in": 3600})


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)


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


@pytest.mark.parametrize(("expires_in", "cached"), [(30, False), (60, False), (61, True)])
def test_only_tokens_living_past_a_minute_are_cached(expires_in, cached, entry):
    client, requests = token_client(
        lambda request: httpx.Response(200, json={"access_token": "tok", "expires_in": expires_in})
    )
    auth.build_headers(entry, client)
    auth.build_headers(entry, client)
    assert len(requests) == (1 if cached else 2)


def test_each_api_keeps_its_own_token(entry, cpf_entry):
    client, requests = token_client(token_of_client)
    for _ in range(2):  # the second round comes from the cache
        assert auth.build_headers(entry, client) == {"Authorization": "Bearer tok-the-id"}
        assert auth.build_headers(cpf_entry, client) == {"Authorization": "Bearer tok-cpf-id"}
    assert len(requests) == 2


def test_cached_token_expires_thirty_seconds_early(entry, clock):
    client, requests = token_client(token_of_client)  # lifetime: 3600 s
    auth.build_headers(entry, client)
    clock.now += 3569  # a second short of the lifetime minus the 30 s margin
    auth.build_headers(entry, client)
    assert len(requests) == 1
    clock.now += 2  # now past it
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
    wait_until(lambda: requests)
    time.sleep(0.2)  # time for the other callers to reach the endpoint, if the code lets them
    release.set()
    for thread in threads:
        thread.join(timeout=5)
    assert len(requests) == 1
    assert results == [{"Authorization": "Bearer tok"}] * 8


def test_cached_token_is_not_held_up_by_another_apis_fetch(entry, cpf_entry):
    release = threading.Event()

    def responder(request):
        if client_id_of(request) == "cpf-id":
            release.wait(timeout=5)  # the CPF token endpoint hangs
        return token_of_client(request)

    client, requests = token_client(responder)
    auth.build_headers(entry, client)  # the CNPJ token is cached now
    cold = threading.Thread(target=lambda: auth.build_headers(cpf_entry, client), daemon=True)
    cold.start()
    try:
        wait_until(lambda: len(requests) == 2)  # the CPF request is in flight
        served = []
        hot = threading.Thread(
            target=lambda: served.append(auth.build_headers(entry, client)), daemon=True
        )
        hot.start()
        hot.join(timeout=2)
        assert not hot.is_alive(), "a cached token waited for another API's token request"
        assert served == [{"Authorization": "Bearer tok-the-id"}]
    finally:
        release.set()
        cold.join(timeout=5)


def test_cached_token_is_served_without_taking_the_lock(entry):
    """Double-checked locking: the lock is for fetching, so a valid token never needs it."""
    client, _ = token_client(token_of_client)
    auth.build_headers(entry, client)
    served = []
    reader = threading.Thread(
        target=lambda: served.append(auth.build_headers(entry, client)), daemon=True
    )
    with auth._token_lock_for(entry.id):  # as if a fetch for this very API were under way
        reader.start()
        reader.join(timeout=2)
        assert not reader.is_alive()
    assert served == [{"Authorization": "Bearer tok-the-id"}]


def test_cold_fetches_of_different_apis_run_in_parallel(entry, cpf_entry):
    release = threading.Event()

    def responder(request):
        release.wait(timeout=5)
        return token_of_client(request)

    client, requests = token_client(responder)
    threads = [
        threading.Thread(target=lambda e=e: auth.build_headers(e, client), daemon=True)
        for e in (entry, cpf_entry)
    ]
    for thread in threads:
        thread.start()
    try:
        wait_until(lambda: len(requests) == 2, timeout=2)
        assert len(requests) == 2, "the second API's token request waited for the first's"
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)


def test_deeply_nested_token_response_is_a_token_error(entry):
    nested = b'{"access_token": ' + b"[" * 50_000
    client, _ = token_client(
        lambda request: httpx.Response(
            200, content=nested, headers={"content-type": "application/json"}
        )
    )
    with pytest.raises(auth.TokenError, match="returned no access_token"):
        auth.build_headers(entry, client)
