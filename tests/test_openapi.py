"""Tests for OpenAPI spec fetching and summarizing. No network: httpx.MockTransport only."""

import json
import re

import httpx
import pytest

from govbr_apis_mcp import openapi
from govbr_apis_mcp.openapi import describe_operation, fetch_spec, summarize_spec

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
    assert len(list(cache_home.glob("*.json"))) == 2


def test_cache_file_name_is_a_hash_inside_the_cache_dir(cache_home):
    path = openapi.cache_path("../../etc/passwd", SPEC_URL)
    assert path.parent == cache_home
    assert re.fullmatch(r"[0-9a-f]{64}\.json", path.name)


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
    assert len(params) == 3  # the pointer into another file is kept, just not resolvable


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
                    "properties": {
                        "example": {"type": "string", "example": "sample of a property"},
                        "examples": {"type": "string"},
                        "xml": {"type": "string", "xml": {"attribute": True}},
                        "name": {"type": "string", "example": "Ana"},
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
