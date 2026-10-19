"""Tests for OpenAPI spec fetching and summarizing. No network: httpx.MockTransport only."""

import gzip
import json
import os
import re
import time
from types import SimpleNamespace

import httpx
import pytest

from govbr_apis_mcp import openapi
from govbr_apis_mcp.openapi import describe_operation, fetch_spec, summarize_spec

SPEC_URL = "https://api.demo.gov.br/openapi.json"
SPEC = {"openapi": "3.0.0", "info": {"title": "Demo", "version": "1"}, "paths": {}}


@pytest.fixture(autouse=True)
def cache_home(tmp_path, monkeypatch):
    """The spec cache lives under tmp_path, never in the real ~/.cache, and the allowlist is
    the built-in one whatever the developer has exported."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.delenv("GOVBR_EXTRA_ALLOWED_HOSTS", raising=False)
    return tmp_path / "govbr-apis-mcp"


def unread(response: httpx.Response) -> httpx.Response:
    """httpx reads a response built from bytes on the spot, but a real transport hands over
    one whose body is still on the wire (iter_raw refuses a body that was already read), so
    rebuild such a response the way a transport would."""
    if not response.is_stream_consumed:
        return response
    stream = httpx.ByteStream(response.content)
    return httpx.Response(response.status_code, headers=response.headers, stream=stream)


def mock_client(handler, **options) -> httpx.Client:
    transport = httpx.MockTransport(lambda request: unread(handler(request)))
    return httpx.Client(transport=transport, **options)


def make_client(responder):
    """Client answering through `responder`; also returns the URLs actually requested."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return responder(request)

    return mock_client(handler), requested


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


@pytest.mark.parametrize("status", [302, 307, 308])
def test_a_redirect_without_location_is_an_http_error(status):
    client, requested = make_client(lambda request: httpx.Response(status))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_spec("demo", SPEC_URL, client)
    assert requested == [SPEC_URL]


def test_a_client_that_follows_redirects_by_default_is_still_checked_hop_by_hop():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "api.demo.gov.br":
            return redirect("https://evil.example.com/openapi.json")
        return ok()

    client = mock_client(handler, follow_redirects=True)
    with pytest.raises(ValueError, match="evil.example.com"):
        fetch_spec("demo", SPEC_URL, client)
    assert requested == [SPEC_URL]


def test_a_spec_url_httpx_cannot_parse_is_a_value_error():
    client, requested = make_client(lambda request: ok())
    with pytest.raises(ValueError, match="invalid spec URL"):
        fetch_spec("demo", "https://api.demo.gov.br:evil/openapi.json", client)
    assert requested == []


@pytest.mark.parametrize(
    "location",
    [
        "https://api.demo.gov.br:evil/x",  # httpx: Invalid URL in location header
        "https:foo",  # a scheme but no host: httpx.InvalidURL
        "https:sub.gov.br/x",
        "javascript:alert(1)",
        "mailto:someone@demo.gov.br",
        "https://xn--/",  # a broken A-label: idna.IDNAError, a UnicodeError
    ],
)
def test_a_location_httpx_cannot_parse_is_a_value_error(location):
    client, requested = make_client(lambda request: redirect(location))
    with pytest.raises(
        ValueError, match=rf"Spec fetch for demo: invalid redirect from {re.escape(SPEC_URL)}"
    ):
        fetch_spec("demo", SPEC_URL, client)
    assert requested == [SPEC_URL]


HUGE = "a" * 60_000  # httpx lets URLs up to 64 KiB through, and a server chooses the Location


def test_a_refused_hop_does_not_echo_a_huge_url():
    client, _ = make_client(lambda request: redirect("https://evil.example.com/" + HUGE))
    with pytest.raises(ValueError, match="refused https://evil.example.com/a+") as err:
        fetch_spec("demo", SPEC_URL, client)
    assert len(str(err.value)) < 600


def test_an_invalid_redirect_does_not_echo_a_huge_url():
    routes = {SPEC_URL: redirect("https://api.demo.gov.br/" + HUGE)}
    client, _ = make_client(lambda request: routes.get(str(request.url)) or redirect("https:foo"))
    with pytest.raises(ValueError, match="invalid redirect from") as err:
        fetch_spec("demo", SPEC_URL, client)
    assert len(str(err.value)) < 600


def test_an_invalid_spec_url_does_not_echo_a_huge_url():
    client, _ = make_client(lambda request: ok())
    with pytest.raises(ValueError, match="invalid spec URL") as err:
        fetch_spec("demo", "https://api.demo.gov.br:evil/" + HUGE, client)
    assert len(str(err.value)) < 600


def test_a_genuine_protocol_error_is_not_passed_off_as_a_bad_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body", request=request
        )

    client = mock_client(handler)
    with pytest.raises(httpx.RemoteProtocolError, match="peer closed connection"):
        fetch_spec("demo", SPEC_URL, client)


# --- size cap -----------------------------------------------------------------------------


FIVE_MIB = 5 * 1024 * 1024  # written out on purpose: the tests must notice if the cap moves


def padded(total: int) -> bytes:
    """A valid JSON spec padded with whitespace to exactly `total` bytes."""
    raw = json.dumps(SPEC).encode()
    return raw + b" " * (total - len(raw))


def test_oversized_body_is_cut_while_streaming():
    chunk = b" " * (1024 * 1024)
    served = []

    def body():
        for i in range(25):  # keeps offering data far past 5 MiB
            served.append(i)
            yield chunk

    client, _ = make_client(lambda request: httpx.Response(200, content=body()))
    with pytest.raises(ValueError, match="spec too large"):
        fetch_spec("demo", SPEC_URL, client)
    assert len(served) == 6  # the sixth MiB crosses the cap and is the last one read


def test_spec_exactly_at_the_cap_is_accepted():
    client, _ = make_client(lambda request: httpx.Response(200, content=padded(FIVE_MIB)))
    assert fetch_spec("demo", SPEC_URL, client) == SPEC


def test_spec_one_byte_over_the_cap_is_rejected():
    client, _ = make_client(lambda request: httpx.Response(200, content=padded(FIVE_MIB + 1)))
    with pytest.raises(ValueError, match="spec too large"):
        fetch_spec("demo", SPEC_URL, client)


# --- compression and time: the cap is on what travels, the deadline on the whole download --


def gzipped(size: int, times: int = 1) -> bytes:
    data = b" " * size
    for _ in range(times):
        data = gzip.compress(data, compresslevel=1)
    return data


@pytest.mark.parametrize("encoding", ["gzip", "gzip, gzip", "deflate", "br", "GZIP"])
def test_compressed_responses_are_refused_before_a_byte_is_read(encoding):
    """A few hundred bytes of gzip inflate to gigabytes in a single step, so a cap checked
    afterwards is too late: the response is refused on its header alone."""
    served = []

    def body():
        served.append(1)
        yield gzipped(6 * 1024 * 1024, times=2 if "," in encoding else 1)

    client, _ = make_client(
        lambda request: httpx.Response(200, content=body(), headers={"content-encoding": encoding})
    )
    message = rf"compressed spec responses are not supported \(Content-Encoding: {encoding}\)"
    with pytest.raises(ValueError, match=message):
        fetch_spec("demo", SPEC_URL, client)
    assert served == []  # nothing was read, so nothing was inflated


@pytest.mark.parametrize("encoding", ["identity", "Identity", " identity "])
def test_content_encoding_identity_is_accepted(encoding):
    client, _ = make_client(
        lambda request: httpx.Response(200, json=SPEC, headers={"content-encoding": encoding})
    )
    assert fetch_spec("demo", SPEC_URL, client) == SPEC


def test_every_request_asks_for_an_uncompressed_body():
    asked: list[str | None] = []

    def responder(request):
        asked.append(request.headers.get("accept-encoding"))
        return redirect("/v2/openapi.json") if request.url.path == "/openapi.json" else ok()

    client, _ = make_client(responder)
    fetch_spec("demo", SPEC_URL, client)
    assert asked == ["identity", "identity"]  # the first hop and the redirect target


class Clock:
    """A time.monotonic that only moves when a test moves it."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(openapi, "time", SimpleNamespace(monotonic=fake.monotonic, time=time.time))
    return fake


def test_a_slow_drip_is_cut_off_by_the_total_deadline(clock):
    served = []

    def drip():
        for i in range(10):
            clock.now += 25  # each chunk shows up 25 s after the previous one
            served.append(i)
            yield b" "

    client, _ = make_client(lambda request: httpx.Response(200, content=drip()))
    with pytest.raises(ValueError, match="spec download exceeded 60 s"):
        fetch_spec("demo", SPEC_URL, client)
    assert len(served) == 3  # 25 s, 50 s, and 75 s is past the 60 s deadline


def test_redirect_hops_count_towards_the_total_deadline(clock):
    def responder(request):
        clock.now += 40  # every hop takes 40 s
        return redirect(f"/t{clock.now:.0f}")

    client, requested = make_client(responder)
    with pytest.raises(ValueError, match="spec download exceeded 60 s"):
        fetch_spec("demo", SPEC_URL, client)
    assert len(requested) == 2  # 40 s in, a second hop starts; 80 s in, a third is refused


# --- YAML specs and document sniffing -----------------------------------------------------

YAML_SPEC = """\
openapi: 3.0.0
info:
  title: Demo YAML
  version: 2024-01-01
paths:
  /things:
    get:
      responses:
        200:
          description: ok
"""


@pytest.mark.parametrize("content_type", ["application/yaml", "text/plain", "application/json"])
def test_yaml_spec_is_parsed_whatever_the_server_calls_it(content_type):
    client, requested = make_client(
        lambda request: httpx.Response(
            200, content=YAML_SPEC.encode(), headers={"content-type": content_type}
        )
    )
    spec = fetch_spec("demo", SPEC_URL, client)
    assert spec["info"] == {"title": "Demo YAML", "version": "2024-01-01"}
    assert list(spec["paths"]["/things"]["get"]["responses"]) == ["200"]
    # The cache holds JSON, so a cache hit returns exactly what the first call returned.
    assert fetch_spec("demo", SPEC_URL, client) == spec
    assert len(requested) == 1


def test_swagger_2_document_is_accepted():
    swagger = {"swagger": "2.0", "info": {"title": "Old", "version": "1"}, "paths": {}}
    client, _ = make_client(lambda request: ok(swagger))
    assert fetch_spec("demo", SPEC_URL, client) == swagger


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b'{"hello": "world"}',
        b'{"paths": {}}',
        b"<html><body>Login</body></html>",
        b"plain text",
        b"a: b: c",
        b"",
        b"\xff\xfe\x00",
    ],
)
def test_non_openapi_documents_are_rejected_and_not_cached(body, cache_home):
    client, _ = make_client(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ValueError, match="not an OpenAPI document"):
        fetch_spec("demo", SPEC_URL, client)
    assert not list(cache_home.glob("*"))


YAML_BOMB = """\
openapi: 3.0.0
l0: &l0 [x, x, x, x, x, x, x, x, x, x]
l1: &l1 [*l0, *l0, *l0, *l0, *l0, *l0, *l0, *l0, *l0, *l0]
l2: &l2 [*l1, *l1, *l1, *l1, *l1, *l1, *l1, *l1, *l1, *l1]
l3: [*l2, *l2, *l2, *l2, *l2, *l2, *l2, *l2, *l2, *l2]
"""


def test_yaml_aliases_are_refused_before_anything_expands(cache_home):
    """Nested anchors, each listing the previous one ten times, stay tiny on the wire and
    multiply on every level once serialized: the size cap cannot catch them, so no aliases."""
    client, _ = make_client(lambda request: httpx.Response(200, content=YAML_BOMB.encode()))
    with pytest.raises(ValueError, match="YAML aliases are not supported"):
        fetch_spec("demo", SPEC_URL, client)
    assert not list(cache_home.glob("*"))


@pytest.mark.parametrize(
    ("body", "where"),
    [
        (b'{"openapi": ', r"line 1 column 13 \(char 12\)"),  # cut short: YAML fails too
        (b"plain text", r"line 1 column 1 \(char 0\)"),  # YAML reads it, as a string
    ],
)
def test_a_document_that_is_neither_json_nor_yaml_says_where_json_broke(body, where):
    client, _ = make_client(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ValueError, match=rf"not an OpenAPI document.*{where}"):
        fetch_spec("demo", SPEC_URL, client)


def test_a_yaml_key_that_is_a_date_is_a_value_error(cache_home):
    body = b"openapi: 3.0.0\nx-changelog:\n  2024-01-01: first release\n"
    client, _ = make_client(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ValueError, match="unsupported key types in spec"):
        fetch_spec("demo", SPEC_URL, client)
    assert not list(cache_home.glob("*"))


def too_deep(*args, **kwargs):
    raise RecursionError("maximum recursion depth exceeded")


# Where json raises RecursionError depends on the Python build (3.12 counts C recursion on its
# own, 3.14 derives the limit from the real stack), so a document sized for one limit proves
# nothing on another. The parsers are made to give up instead.
def test_json_past_the_recursion_limit_is_a_value_error(monkeypatch, cache_home):
    monkeypatch.setattr(openapi.json, "loads", too_deep)
    client, _ = make_client(lambda request: httpx.Response(200, content=b"[[[[]]]]"))
    with pytest.raises(ValueError, match="spec too deeply nested"):
        fetch_spec("demo", SPEC_URL, client)
    assert not list(cache_home.glob("*"))


def test_yaml_past_the_recursion_limit_is_a_value_error(monkeypatch, cache_home):
    monkeypatch.setattr(openapi.yaml, "load", too_deep)
    client, _ = make_client(lambda request: httpx.Response(200, content=b"openapi: 3.0.0"))
    with pytest.raises(ValueError, match="spec too deeply nested"):
        fetch_spec("demo", SPEC_URL, client)
    assert not list(cache_home.glob("*"))


def test_pyyaml_itself_gives_up_on_absurd_nesting(cache_home):
    """PyYAML recurses in Python code, so its limit is sys.getrecursionlimit() on every build."""
    body = b"x: " + b"[" * 100_000
    client, _ = make_client(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ValueError, match="spec too deeply nested"):
        fetch_spec("demo", SPEC_URL, client)


@pytest.mark.parametrize("function", ["dumps", "loads"])
def test_serializing_past_the_recursion_limit_is_a_value_error(function, monkeypatch):
    monkeypatch.setattr(openapi.json, function, too_deep)
    with pytest.raises(ValueError, match="spec too deeply nested"):
        openapi._round_trip(SPEC)


# --- cache key ----------------------------------------------------------------------------


def test_cache_is_keyed_by_api_id_and_url(cache_home):
    url_a, url_b = SPEC_URL, "https://api.demo.gov.br/v2/openapi.json"
    specs = {
        url_a: {**SPEC, "info": {"title": "A", "version": "1"}},
        url_b: {**SPEC, "info": {"title": "B", "version": "1"}},
    }
    client, requested = make_client(lambda request: ok(specs[str(request.url)]))

    assert fetch_spec("demo", url_a, client)["info"]["title"] == "A"
    # Same id, new openapi_url: a catalog edit must not be shadowed by the old cache entry.
    assert fetch_spec("demo", url_b, client)["info"]["title"] == "B"
    assert fetch_spec("demo", url_a, client)["info"]["title"] == "A"
    assert requested == [url_a, url_b]  # the third call was a cache hit
    assert openapi.cache_path("demo", url_a) != openapi.cache_path("demo", url_b)
    assert openapi.cache_path("one", url_a) != openapi.cache_path("two", url_a)
    assert len(list(cache_home.glob("*.json"))) == 2


def test_cache_file_name_is_a_hash_inside_the_cache_dir(cache_home):
    path = openapi.cache_path("../../etc/passwd", SPEC_URL)
    assert path.parent == cache_home
    assert re.fullmatch(r"[0-9a-f]{64}\.json", path.name)


@pytest.mark.parametrize(("age_hours", "fetched_again"), [(23, False), (25, True)])
def test_cache_entries_live_24_hours(age_hours, fetched_again):
    client, requested = make_client(lambda request: ok())
    fetch_spec("demo", SPEC_URL, client)
    path = openapi.cache_path("demo", SPEC_URL)
    then = time.time() - age_hours * 3600
    os.utime(path, (then, then))
    fetch_spec("demo", SPEC_URL, client)
    assert len(requested) == (2 if fetched_again else 1)


@pytest.mark.parametrize("garbage", [b"{not json", b"\xff\xfe\x00 not text"], ids=["json", "utf8"])
def test_a_corrupted_cache_file_is_repaired_by_fetching_again(garbage):
    client, requested = make_client(lambda request: ok())
    path = openapi.cache_path("demo", SPEC_URL)
    path.write_bytes(garbage)
    assert fetch_spec("demo", SPEC_URL, client) == SPEC
    assert len(requested) == 1
    assert json.loads(path.read_text(encoding="utf-8")) == SPEC


# --- summarize_spec / describe_operation --------------------------------------------------


def operation(spec, path, method):
    return describe_operation(spec, path, method)["operations"][method.upper()]


def test_summarize_survives_null_summary_and_description():
    spec = {
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/a": {"get": {"summary": None, "description": None}},
            "/b": {"get": {"description": "x" * 200}},
            "/c": {"get": {"summary": "Short", "description": None}},
        },
    }
    summaries = {op["path"]: op["summary"] for op in summarize_spec(spec)["operations"]}
    assert summaries == {"/a": None, "/b": "x" * 120, "/c": "Short"}


REF_PARAMS_SPEC = {
    "openapi": "3.0.0",
    "paths": {
        "/items/{id}": {
            "parameters": [{"$ref": "#/components/parameters/ItemId"}],
            "get": {
                "parameters": [
                    {"$ref": "#/components/parameters/Limit"},
                    {"$ref": "other.yaml#/parameters/Elsewhere"},
                ],
                "responses": {"200": {"description": "ok"}},
            },
        }
    },
    "components": {
        "parameters": {
            "ItemId": {
                "name": "id",
                "in": "path",
                "required": True,
                "description": "Item id",
                "schema": {"type": "string"},
            },
            "Limit": {
                "name": "limit",
                "in": "query",
                "schema": {"$ref": "#/components/schemas/Limit"},
            },
        },
        "schemas": {"Limit": {"type": "integer", "maximum": 100}},
    },
}


def test_parameter_refs_are_resolved_before_reading_name_and_location():
    params = operation(REF_PARAMS_SPEC, "/items/{id}", "get")["parameters"]
    by_name = {p["name"]: p for p in params if p["name"]}
    assert by_name["id"]["in"] == "path"
    assert by_name["id"]["required"] is True
    assert by_name["id"]["description"] == "Item id"
    assert by_name["id"]["schema"] == {"type": "string"}
    assert by_name["limit"]["in"] == "query"
    assert by_name["limit"]["schema"] == {"type": "integer", "maximum": 100}
    assert len(params) == 3
    unresolved = [p for p in params if p["name"] is None]
    # the pointer into another file cannot be followed, but the model is told which one it was
    assert [p["$ref"] for p in unresolved] == ["other.yaml#/parameters/Elsewhere"]


def test_operation_parameter_replaces_the_inherited_one_with_the_same_name_and_location():
    spec = {
        "paths": {
            "/x": {
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "description": "path",
                        "schema": {"type": "integer"},
                    },
                    {"name": "limit", "in": "header", "schema": {"type": "string"}},
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "get": {
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "description": "op",
                            "schema": {"type": "string"},
                        }
                    ]
                },
            }
        }
    }
    params = operation(spec, "/x", "get")["parameters"]
    by_key = {(p["name"], p["in"]): p for p in params}
    assert len(params) == 3
    assert set(by_key) == {("limit", "query"), ("limit", "header"), ("id", "path")}
    assert by_key[("limit", "query")]["description"] == "op"
    assert by_key[("limit", "query")]["schema"] == {"type": "string"}


def test_null_parameter_lists_are_tolerated():
    spec = {"paths": {"/x": {"parameters": None, "get": {"parameters": None}}}}
    assert operation(spec, "/x", "get")["parameters"] == []


PET = {"type": "object", "properties": {"name": {"type": "string"}}}
SWAGGER_2_SPEC = {
    "swagger": "2.0",
    "paths": {
        "/pets/{id}": {
            "get": {
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "type": "integer",
                        "format": "int64",
                    },
                    {"name": "status", "in": "query", "type": "string", "enum": ["a", "b"]},
                    {"name": "tags", "in": "query", "type": "array", "items": {"type": "string"}},
                ],
                "responses": {
                    "200": {"description": "ok", "schema": {"$ref": "#/definitions/Pet"}},
                    "404": {"description": "missing"},
                },
            },
            "put": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "type": "integer"},
                    {
                        "name": "body",
                        "in": "body",
                        "required": True,
                        "schema": {"$ref": "#/definitions/Pet"},
                    },
                ],
                "responses": {"204": {"description": "updated"}},
            },
        }
    },
    "definitions": {"Pet": PET},
}


def test_swagger_2_parameters_carry_their_type_on_the_parameter():
    params = {p["name"]: p for p in operation(SWAGGER_2_SPEC, "/pets/{id}", "get")["parameters"]}
    assert params["id"]["schema"] == {"type": "integer", "format": "int64"}
    assert params["status"]["schema"] == {"type": "string", "enum": ["a", "b"]}
    assert params["tags"]["schema"] == {"type": "array", "items": {"type": "string"}}


def test_swagger_2_response_schema_sits_on_the_response():
    assert operation(SWAGGER_2_SPEC, "/pets/{id}", "get")["response"] == PET


def test_swagger_2_body_parameter_becomes_the_request_body():
    put = operation(SWAGGER_2_SPEC, "/pets/{id}", "put")
    assert put["requestBody"] == PET
    assert [p["name"] for p in put["parameters"]] == ["id"]
    assert operation(SWAGGER_2_SPEC, "/pets/{id}", "get")["requestBody"] is None


def test_properties_named_like_schema_keywords_are_kept():
    spec = {
        "paths": {
            "/x": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Doc"}}
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "Doc": {
                    "type": "object",
                    "example": {"example": "a sample, dropped"},
                    "examples": [{"example": "samples, dropped"}],
                    "xml": {"name": "Doc"},
                    "properties": {
                        "example": {"type": "string", "example": "sample of a property"},
                        "examples": {"type": "string"},
                        "xml": {"type": "string", "xml": {"attribute": True}},
                        "name": {"type": "string", "example": "Ana", "examples": ["Ana", "Bia"]},
                    },
                }
            }
        },
    }
    assert operation(spec, "/x", "post")["requestBody"] == {
        "type": "object",
        "properties": {
            "example": {"type": "string"},
            "examples": {"type": "string"},
            "xml": {"type": "string"},
            "name": {"type": "string"},
        },
    }


def test_a_property_called_properties_holds_a_schema_not_a_name_map():
    schema = {
        "type": "object",
        "properties": {
            "properties": {
                "type": "object",
                "example": {"dropped": True},
                "properties": {"example": {"type": "string", "example": "x"}},
            }
        },
    }
    assert openapi._resolve({}, schema, depth=10) == {
        "type": "object",
        "properties": {
            "properties": {"type": "object", "properties": {"example": {"type": "string"}}}
        },
    }


def test_unresolvable_parameter_refs_are_kept_apart():
    elsewhere = {"$ref": "other.yaml#/parameters/A"}
    spec = {
        "paths": {
            "/x": {
                "parameters": [elsewhere],
                "get": {
                    "parameters": [
                        elsewhere,  # the same pointer again replaces it
                        {"$ref": "other.yaml#/parameters/B"},
                        {"name": "q", "in": "query"},
                    ]
                },
            }
        }
    }
    params = operation(spec, "/x", "get")["parameters"]
    assert sorted(p.get("$ref") or p["name"] for p in params) == [
        "other.yaml#/parameters/A",
        "other.yaml#/parameters/B",
        "q",
    ]


def test_parameter_refs_to_something_other_than_an_object_are_left_alone():
    spec = {
        "paths": {
            "/x": {
                "get": {
                    "parameters": [{"$ref": "#/components/nothing"}, {"$ref": "#/components/list"}]
                }
            }
        },
        "components": {"nothing": None, "list": [1, 2]},
    }
    assert len(operation(spec, "/x", "get")["parameters"]) == 2


def test_json_pointers_are_unescaped():
    spec = {
        "paths": {
            "/x": {
                "post": {
                    "parameters": [{"$ref": "#/components/parameters/a~1b"}],
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/c~0d"}}
                        }
                    },
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/x~01y"}
                                }
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "parameters": {"a/b": {"name": "slash", "in": "query"}},
            "schemas": {"c~d": {"type": "string"}, "x~1y": {"type": "integer"}},
        },
    }
    post = operation(spec, "/x", "post")
    assert post["parameters"][0]["name"] == "slash"  # ~1 is "/"
    assert post["requestBody"] == {"type": "string"}  # ~0 is "~"
    assert post["response"] == {"type": "integer"}  # ~01 is "~1", not "/"


def test_null_fields_in_an_operation_are_tolerated():
    spec = {
        "paths": {
            "/x": {
                "get": {
                    "parameters": [
                        {"name": "q", "in": "query", "description": None},
                        {"name": "bare", "in": "query"},
                    ],
                    "requestBody": None,
                    "responses": {"200": None},
                }
            }
        }
    }
    got = operation(spec, "/x", "get")
    assert [(p["name"], p["description"], p["schema"]) for p in got["parameters"]] == [
        ("q", None, None),
        ("bare", None, None),
    ]
    assert got["requestBody"] is None
    assert got["response"] is None


GOOD = {"type": "object", "properties": {"id": {"type": "integer"}}}
OTHER = {"type": "object", "properties": {"other": {"type": "string"}}}
ERR = {"type": "object", "properties": {"message": {"type": "string"}}}


def openapi3(schema):
    response = {"description": "d"}
    if schema is not None:
        response["content"] = {"application/json": {"schema": schema}}
    return response


def swagger2(schema):
    response = {"description": "d"}
    if schema is not None:
        response["schema"] = schema
    return response


@pytest.mark.parametrize("build", [openapi3, swagger2], ids=["openapi3", "swagger2"])
@pytest.mark.parametrize(
    ("responses", "expected"),
    [
        ([("200", GOOD)], GOOD),
        ([("200", GOOD), ("default", ERR)], GOOD),
        ([("default", ERR), ("404", ERR), ("200", GOOD)], GOOD),
        # a DELETE that answers 204 and documents a default error has no response schema
        ([("204", None), ("default", ERR)], None),
        ([("204", None), ("404", ERR)], None),
        ([("404", ERR)], None),
        ([("default", ERR)], None),
        # the first 2xx that has a schema, 200 and 201 ahead of the rest
        ([("200", None), ("202", OTHER)], OTHER),
        ([("202", OTHER), ("201", GOOD)], GOOD),
        ([("201", OTHER), ("200", GOOD)], GOOD),
        ([("2XX", GOOD)], GOOD),
        # an empty schema is still a schema ("anything"), not a missing one
        ([("200", {}), ("202", OTHER)], {}),
    ],
)
def test_response_is_the_success_schema_and_never_an_error_one(build, responses, expected):
    op = {"responses": {code: build(schema) for code, schema in responses}}
    spec = {"paths": {"/x": {"delete": op}}}
    assert operation(spec, "/x", "delete")["response"] == expected


def test_an_empty_request_body_schema_is_kept():
    spec = {
        "paths": {
            "/x": {"post": {"requestBody": {"content": {"application/json": {"schema": {}}}}}}
        }
    }
    assert operation(spec, "/x", "post")["requestBody"] == {}


def test_null_parts_of_a_yaml_spec_do_not_break_the_summary():
    spec = {
        "info": None,
        "servers": None,
        "paths": {"/empty": None, "/x": {"get": {"summary": "s"}}},
    }
    assert summarize_spec(spec) == {
        "title": None,
        "version": None,
        "servers": [],
        "operations": [{"method": "GET", "path": "/x", "summary": "s"}],
    }


def test_null_path_items_and_media_types_do_not_break_describe_operation():
    spec = {
        "paths": {
            "/empty": None,
            "/x": {
                "post": {
                    "requestBody": {"content": {"application/json": None}},
                    "responses": {"200": {"content": {"application/json": None}}},
                }
            },
        }
    }
    assert describe_operation(spec, "/empty", None) == {"path": "/empty", "operations": {}}
    with pytest.raises(KeyError, match="not available"):
        describe_operation(spec, "/empty", "get")
    post = operation(spec, "/x", "post")
    assert post["requestBody"] is None
    assert post["response"] is None
