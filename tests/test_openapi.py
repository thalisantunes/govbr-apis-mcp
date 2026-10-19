"""Tests for OpenAPI spec fetching and summarizing. No network: httpx.MockTransport only."""

import json

import httpx
import pytest

from govbr_apis_mcp import openapi
from govbr_apis_mcp.openapi import fetch_spec

SPEC_URL = "https://api.demo.gov.br/openapi.json"
SPEC = {"openapi": "3.0.0", "info": {"title": "Demo", "version": "1"}, "paths": {}}


@pytest.fixture(autouse=True)
def cache_home(tmp_path, monkeypatch):
    """The spec cache lives under tmp_path, never in the real ~/.cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path / "govbr-apis-mcp"


def make_client(responder):
    """Client answering through `responder`; also returns the URLs actually requested."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return responder(request)

    return httpx.Client(transport=httpx.MockTransport(handler)), requested


def redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


def ok(spec=SPEC) -> httpx.Response:
    return httpx.Response(200, json=spec)


# --- redirects: every hop is checked before it is requested -------------------------------


@pytest.mark.parametrize(
    "location", ["https://evil.example.com/openapi.json", "//evil.example.com/openapi.json"]
)
def test_redirect_off_the_allowlist_is_never_requested(location, cache_home):
    client, requested = make_client(
        lambda request: redirect(location) if request.url.host == "api.demo.gov.br" else ok()
    )
    with pytest.raises(ValueError, match="evil.example.com"):
        fetch_spec("demo", SPEC_URL, client)
    assert requested == [SPEC_URL]
    assert not list(cache_home.glob("*"))


def test_redirect_to_plain_http_is_refused_and_not_cached(cache_home):
    http_url = "http://api.demo.gov.br/openapi.json"
    client, requested = make_client(
        lambda request: redirect(http_url) if request.url.scheme == "https" else ok()
    )
    with pytest.raises(ValueError, match="https"):
        fetch_spec("demo", SPEC_URL, client)
    assert requested == [SPEC_URL]
    assert not list(cache_home.glob("*"))


def test_bad_hop_in_the_middle_of_a_chain_is_refused():
    """gov.br -> evil -> gov.br ends on an allowed host, but the middle hop was still a leak."""
    evil = "https://evil.example.com/hop"
    routes = {SPEC_URL: redirect(evil), evil: redirect(SPEC_URL + "?back=1")}
    client, requested = make_client(lambda request: routes.get(str(request.url)) or ok())
    with pytest.raises(ValueError, match="evil.example.com"):
        fetch_spec("demo", SPEC_URL, client)
    assert requested == [SPEC_URL]


@pytest.mark.parametrize(
    "url", ["http://api.demo.gov.br/openapi.json", "https://evil.example.com/openapi.json"]
)
def test_the_first_url_is_checked_too(url):
    client, requested = make_client(lambda request: ok())
    with pytest.raises(ValueError, match="allowlisted"):
        fetch_spec("demo", url, client)
    assert requested == []


def test_valid_redirect_chain_is_followed():
    routes = {
        SPEC_URL: redirect("/v2/openapi.json"),
        "https://api.demo.gov.br/v2/openapi.json": redirect(
            "https://static.demo.gov.br/s.json", 301
        ),
        "https://static.demo.gov.br/s.json": ok(),
    }
    client, requested = make_client(lambda request: routes[str(request.url)])
    assert fetch_spec("demo", SPEC_URL, client) == SPEC
    assert requested == list(routes)


@pytest.mark.parametrize(("redirects", "allowed"), [(5, True), (6, False)])
def test_redirect_limit(redirects, allowed):
    def responder(request):
        hop = int(request.url.path.rsplit("/", 1)[1])
        return redirect(f"/hop/{hop + 1}") if hop < redirects else ok()

    client, requested = make_client(responder)
    first = "https://api.demo.gov.br/hop/0"
    if allowed:
        assert fetch_spec("demo", first, client) == SPEC
    else:
        with pytest.raises(ValueError, match="redirects"):
            fetch_spec("demo", first, client)
        assert len(requested) == 6  # the sixth redirect is refused, not followed


def test_3xx_without_location_is_an_http_error():
    client, _ = make_client(lambda request: httpx.Response(304))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_spec("demo", SPEC_URL, client)


# --- size cap -----------------------------------------------------------------------------


def padded(total: int) -> bytes:
    """A valid JSON spec padded with whitespace to exactly `total` bytes."""
    raw = json.dumps(SPEC).encode()
    return raw + b" " * (total - len(raw))


def test_oversized_body_is_cut_while_streaming():
    chunk = b" " * (1024 * 1024)
    cap_chunks = openapi.MAX_SPEC_BYTES // len(chunk)
    served = []

    def body():
        for i in range(cap_chunks + 20):  # keeps offering data well past the cap
            served.append(i)
            yield chunk

    client, _ = make_client(lambda request: httpx.Response(200, content=body()))
    with pytest.raises(ValueError, match="spec too large"):
        fetch_spec("demo", SPEC_URL, client)
    assert len(served) <= cap_chunks + 1  # stopped right after crossing the cap


def test_spec_exactly_at_the_cap_is_accepted():
    client, _ = make_client(
        lambda request: httpx.Response(200, content=padded(openapi.MAX_SPEC_BYTES))
    )
    assert fetch_spec("demo", SPEC_URL, client) == SPEC


def test_spec_one_byte_over_the_cap_is_rejected():
    client, _ = make_client(
        lambda request: httpx.Response(200, content=padded(openapi.MAX_SPEC_BYTES + 1))
    )
    with pytest.raises(ValueError, match="spec too large"):
        fetch_spec("demo", SPEC_URL, client)
