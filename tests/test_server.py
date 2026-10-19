"""Tests for the MCP tool layer — URL construction, header precedence, redaction."""

import dataclasses

import httpx
import pytest

from govbr_apis_mcp import __version__, auth, server
from govbr_apis_mcp.catalog import ApiEntry, Catalog

BASE = "https://api.portaldatransparencia.gov.br/api-de-dados"
ALLOWED_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


@pytest.fixture
def catalog(monkeypatch):
    loaded = Catalog.load()
    monkeypatch.setattr(server, "_catalog", loaded)
    return loaded


@pytest.fixture
def wire(monkeypatch):
    """Put a MockTransport behind the gateway's shared client.

    `wire(responder)` returns the list of requests that reached the "wire". The default
    responder answers 200 with a small JSON body.
    """

    def install(responder=lambda request: httpx.Response(200, json={"ok": True})):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return responder(request)

        client = httpx.Client(
            transport=httpx.MockTransport(handler), headers=server._client.headers
        )
        monkeypatch.setattr(server, "_client", client)
        return requests

    return install


@pytest.fixture
def oauth_api(monkeypatch):
    """A callable OAuth2 entry, so tests can watch token traffic (Serpro needs a contract)."""
    entry = ApiEntry(
        id="oauth-demo",
        name="OAuth demo",
        provider="test",
        category="public",
        status="available",
        auth="oauth2_client_credentials",
        base_url="https://api.demo.gov.br/v1",
        auth_config={
            "token_url": "https://auth.demo.gov.br/token",
            "client_id_env": "DEMO_CLIENT_ID",
            "client_secret_env": "DEMO_CLIENT_SECRET",
        },
    )
    monkeypatch.setattr(server, "_catalog", Catalog({entry.id: entry}))
    monkeypatch.setattr(auth, "_token_cache", {})
    monkeypatch.setenv("DEMO_CLIENT_ID", "id")
    monkeypatch.setenv("DEMO_CLIENT_SECRET", "secret")
    return entry


def token_then_api(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/token":
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    return httpx.Response(200, json={"ok": True})


def test_embedded_query_is_merged_not_dropped():
    """httpx replaces a URL's query when params is given; PTAX-style paths must survive."""
    url, params = server._build_target(
        "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata",
        "/CotacaoDolarDia(dataCotacao=@d)?@d='08-06-2026'",
        {"$format": "json"},
    )
    assert params == httpx.QueryParams({"@d": "'08-06-2026'", "$format": "json"})
    assert url.endswith("/CotacaoDolarDia(dataCotacao=@d)")


def test_path_only_query_survives_without_dict():
    _, params = server._build_target(BASE, "/x?a=1&b=2", None)
    assert params == httpx.QueryParams({"a": "1", "b": "2"})


@pytest.mark.parametrize(
    "path",
    ["../../etc/passwd", "/../other-product/v9", "/%2e%2e/%2e%2e/secret", "/a/../../escape"],
)
def test_traversal_is_rejected(path):
    with pytest.raises(ValueError, match="escapes the API's base URL|contains '\\.\\.'"):
        server._build_target(BASE, path, None)


@pytest.mark.parametrize("path", ["//evil.com/x", "https://evil.com/x", "\\\\evil.com/x"])
def test_host_cannot_be_swapped(path):
    url, _ = server._build_target(BASE, path, None)
    assert url.startswith(BASE)


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/%252e%252e/api-de-dados-admin/x", id="double-encoded"),
        pytest.param("/%25252e%25252e/x", id="triple-encoded"),
        pytest.param("/%2525252e%2525252e/x", id="encoded-too-deep-to-trust"),
        pytest.param("/..;/..;/x", id="path-parameters"),
        pytest.param("/..%5c..%5cx", id="encoded-backslash"),
        pytest.param("/..\\..\\x", id="backslash"),
        pytest.param("/a/./b", id="dot-segment"),
        pytest.param("/a/%2e/b", id="encoded-dot-segment"),
    ],
)
def test_encoded_traversal_variants_are_rejected(path):
    """A proxy in front of the API may decode more than once, so decode until stable."""
    with pytest.raises(ValueError, match="escapes the API's base URL|contains '\\.\\.'"):
        server._build_target(BASE, path, None)


def test_prefix_guard_has_a_segment_boundary(monkeypatch):
    """Second line of defence: if the dot-segment check ever misses an encoding, a path
    that normalizes to a sibling of the base prefix is still refused."""
    monkeypatch.setattr(server, "_has_dot_segment", lambda path: False)
    with pytest.raises(ValueError, match="escapes the API's base URL"):
        server._build_target(BASE, "/%252e%252e/api-de-dados-admin/x", None)


def test_path_that_repeats_the_prefix_text_is_still_a_child():
    """The path is appended to the base, so /api-de-dados-admin lands *under* it."""
    url, _ = server._build_target(BASE, "/api-de-dados-admin/x", None)
    assert url == f"{BASE}/api-de-dados-admin/x"


@pytest.mark.parametrize(
    "path",
    ["", "/", "viagens", "/viagens?x=1", "/a.b/c..d/.hidden", "/a%20b", "/V(K='X_Y')"],
)
def test_ordinary_paths_pass_the_guard(path):
    url, _ = server._build_target(BASE, path, None)
    assert url.startswith(BASE)


def test_unusable_path_characters_are_a_value_error():
    with pytest.raises(ValueError, match="not a valid URL path"):
        server._build_target(BASE, "/x\x00y", None)


def test_repeated_query_keys_survive_from_path_and_caller(catalog, wire):
    requests = wire()
    out = server.call_api(
        "ibge-localidades",
        "/municipios?uf=SP&uf=RJ&pagina=1",
        query={"pagina": "2", "ano": ["2024", "2025"]},
    )
    sent = requests[0].url.params
    assert sent.get_list("uf") == ["SP", "RJ"]
    assert sent.get_list("pagina") == ["2"]  # the caller's key replaces the embedded one
    assert sent.get_list("ano") == ["2024", "2025"]
    assert httpx.URL(out["url"]).params.get_list("uf") == ["SP", "RJ"]


def test_caller_query_replaces_every_embedded_value_of_a_key():
    _, params = server._build_target(BASE, "/x?uf=SP&uf=RJ", {"uf": "MG"})
    assert list(params.multi_items()) == [("uf", "MG")]


def test_redact_keeps_repeated_values():
    out = httpx.URL(server._redact("https://x.gov.br/a?uf=SP&uf=RJ&token=1&token=2"))
    assert out.params.get_list("uf") == ["SP", "RJ"]
    assert out.params.get_list("token") == ["***", "***"]


def test_redact_survives_an_unparseable_url():
    assert server._redact("http://[::1") == "<invalid URL>"


def test_redirects_are_reported_not_followed(catalog, wire):
    requests = wire(
        lambda request: httpx.Response(
            301, headers={"Location": "https://servicodados.ibge.gov.br/new?key=SECRET&a=1"}
        )
    )
    out = server.call_api("ibge-localidades", "/estados")
    assert out["status"] == 301
    assert len(requests) == 1
    location = httpx.URL(out["location"])
    assert (location.host, location.path) == ("servicodados.ibge.gov.br", "/new")
    assert (location.params.get("key"), location.params.get("a")) == ("***", "1")
    assert "SECRET" not in str(out)


def test_relative_redirect_location_is_redacted_too(catalog, wire):
    wire(lambda request: httpx.Response(302, headers={"Location": "/login?token=abc"}))
    out = server.call_api("ibge-localidades", "/estados")
    assert httpx.URL(out["location"]).params.get("token") == "***"
    assert "abc" not in str(out)


def test_malformed_redirect_location_does_not_break_the_call(catalog, wire):
    """httpx itself rejects a bad Location on 301/302/303/307/308; a 300 reaches us as is."""
    wire(lambda request: httpx.Response(300, headers={"Location": "http://[::1"}))
    out = server.call_api("ibge-localidades", "/estados")
    assert (out["status"], out["location"]) == (300, "<invalid URL>")


def test_redirect_status_without_location_has_no_location_key(catalog, wire):
    wire(lambda request: httpx.Response(304))
    out = server.call_api("ibge-localidades", "/estados")
    assert out["status"] == 304
    assert "location" not in out


def test_normal_path_is_preserved():
    url, params = server._build_target(BASE, "servidores", None)
    assert url == f"{BASE}/servidores"
    assert params == httpx.QueryParams()


def test_redact_masks_credential_like_params():
    out = server._redact("https://x.gov.br/a?chave-api=SEGREDO&token=abc&pagina=1")
    assert "SEGREDO" not in out and "abc" not in out
    assert "pagina=1" in out


def test_redact_keeps_urls_without_query():
    assert server._redact("https://x.gov.br/a") == "https://x.gov.br/a"


def test_call_api_rejects_body_on_get(monkeypatch):
    monkeypatch.setattr(server, "_catalog", Catalog.load())
    out = server.call_api("ibge-localidades", "/estados", method="GET", body={"a": 1})
    assert "body is not supported" in out["error"]


def test_call_api_blocks_not_callable(monkeypatch):
    monkeypatch.setattr(server, "_catalog", Catalog.load())
    out = server.call_api("conecta-consulta-cnpj", "/basica/1")
    assert "not callable" in out["error"]


SERPRO_CNPJ = "serpro-consulta-cnpj"
SERPRO_ENV = ("SERPRO_CNPJ_CLIENT_ID", "SERPRO_CNPJ_CLIENT_SECRET")


def test_requires_contract_api_is_callable_once_its_credentials_are_set(catalog, wire, monkeypatch):
    monkeypatch.setattr(auth, "_token_cache", {})
    for var in SERPRO_ENV:
        monkeypatch.setenv(var, "x")
    requests = wire(
        lambda request: (
            httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
            if request.url.path == "/token"
            else httpx.Response(200, json={"cnpj": "00000000000191"})
        )
    )
    out = server.call_api(SERPRO_CNPJ, "/basica/00000000000191")
    assert out["status"] == 200
    assert out["body"] == {"cnpj": "00000000000191"}
    assert [(r.url.host, r.url.path) for r in requests] == [
        ("gateway.apiserpro.serpro.gov.br", "/token"),
        ("gateway.apiserpro.serpro.gov.br", "/consulta-cnpj-df/v2/basica/00000000000191"),
    ]
    assert requests[1].headers["authorization"] == "Bearer tok"


def test_requires_contract_api_without_credentials_says_which_are_missing(
    catalog, wire, monkeypatch
):
    monkeypatch.delenv("SERPRO_CNPJ_CLIENT_ID", raising=False)
    monkeypatch.setenv("SERPRO_CNPJ_CLIENT_SECRET", "x")
    requests = wire()
    out = server.call_api(SERPRO_CNPJ, "/basica/00000000000191")
    assert out["error"] == "serpro-consulta-cnpj is not callable (status: requires_contract)."
    assert "SERPRO_CNPJ_CLIENT_ID" in out["hint"]
    assert "SERPRO_CNPJ_CLIENT_SECRET" not in out["hint"]  # that one is set
    assert requests == []


def test_requires_contract_api_without_base_url_stays_not_callable(monkeypatch, wire):
    for var in SERPRO_ENV:
        monkeypatch.setenv(var, "x")
    entry = dataclasses.replace(Catalog.load().get(SERPRO_CNPJ), base_url=None)
    monkeypatch.setattr(server, "_catalog", Catalog({entry.id: entry}))
    requests = wire()
    out = server.call_api(SERPRO_CNPJ, "/basica/00000000000191")
    assert "not callable" in out["error"]
    assert requests == []


def test_credentials_status_says_whether_call_api_accepts_the_entry(catalog, monkeypatch):
    for var in SERPRO_ENV:
        monkeypatch.delenv(var, raising=False)
    [item] = server.credentials_status(SERPRO_CNPJ)
    assert (item["ready"], item["callable"]) == (False, False)
    for var in SERPRO_ENV:
        monkeypatch.setenv(var, "x")
    [item] = server.credentials_status(SERPRO_CNPJ)
    assert (item["ready"], item["callable"]) == (True, True)


def test_reports_the_package_version():
    """serverInfo.version used to reach the client empty, and the User-Agent said 0.2."""
    assert server.mcp.version == __version__
    assert server._client.headers["user-agent"] == f"govbr-apis-mcp/{__version__}"


def test_call_api_unknown_id_returns_error_dict(monkeypatch):
    monkeypatch.setattr(server, "_catalog", Catalog.load())
    out = server.call_api("nao-existe-xyz", "/x")
    assert "error" in out and "Unknown API id" in out["error"]


def test_protected_headers_cannot_override_auth(catalog, wire, monkeypatch):
    """A caller-supplied Authorization/Host must never reach the wire."""
    monkeypatch.setenv("DATAJUD_API_KEY", "REAL-KEY")
    requests = wire()
    out = server.call_api(
        "datajud",
        "/api_publica_tjsp/_search",
        method="POST",
        body={"query": {}},
        headers={"Authorization": "Bearer ATTACKER", "Host": "internal", "X-Trace": "keep"},
    )
    sent = requests[0]
    assert sent.headers.get_list("authorization") == ["APIKey REAL-KEY"]
    assert sent.headers.get_list("host") == ["api-publica.datajud.cnj.jus.br"]
    assert sent.headers.get_list("x-trace") == ["keep"]
    assert out["ignored_headers"] == ["Authorization", "Host"]


def test_caller_headers_cannot_duplicate_the_managed_credential(catalog, wire, monkeypatch):
    """httpx sends every header line it is given, so case variants of the credential
    header have to be dropped, not just kept apart from it."""
    monkeypatch.setenv("TRANSPARENCIA_API_KEY", "REAL-KEY")
    requests = wire()
    out = server.call_api(
        "portal-transparencia",
        "/api-de-dados/viagens",
        headers={"Chave-Api-Dados": "a", "CHAVE-API-DADOS": "b"},
    )
    assert requests[0].headers.get_list("chave-api-dados") == ["REAL-KEY"]
    assert out["ignored_headers"] == ["CHAVE-API-DADOS", "Chave-Api-Dados"]


@pytest.mark.parametrize(
    "name",
    [
        "Cookie",
        "Proxy-Authorization",
        "Content-Length",
        "Transfer-Encoding",
        "Connection",
        "Upgrade",
        "TE",
        "Trailer",
        "Keep-Alive",
        "Forwarded",
        "X-Forwarded-For",
        "x-forwarded-host",
    ],
)
def test_framing_and_proxy_headers_never_reach_the_wire(name, catalog, wire):
    requests = wire()
    out = server.call_api("ibge-localidades", "/estados", headers={name: "evil"})
    assert "evil" not in requests[0].headers.get_list(name)
    assert out["ignored_headers"] == [name]


def test_content_length_is_the_real_one(catalog, wire, monkeypatch):
    monkeypatch.setenv("DATAJUD_API_KEY", "REAL-KEY")
    requests = wire()
    server.call_api(
        "datajud",
        "/api_publica_tjsp/_search",
        method="POST",
        body={"query": {}},
        headers={"Content-Length": "1", "Transfer-Encoding": "chunked"},
    )
    sent = requests[0]
    assert sent.headers.get_list("content-length") == [str(len(sent.content))]
    assert sent.headers.get_list("transfer-encoding") == []


def test_accept_encoding_is_identity_and_not_overridable(catalog, wire):
    """The 60k cut runs on decoded text: with compression one chunk can inflate to MBs."""
    requests = wire()
    out = server.call_api("ibge-localidades", "/estados", headers={"accept-encoding": "gzip"})
    assert requests[0].headers.get_list("accept-encoding") == ["identity"]
    assert out["ignored_headers"] == ["accept-encoding"]


def test_ordinary_caller_headers_still_pass(catalog, wire):
    requests = wire()
    out = server.call_api(
        "ibge-localidades",
        "/estados",
        headers={"accept": "application/xml", "X-Trace": "keep", "x-trace": "keep2"},
    )
    assert requests[0].headers.get_list("accept") == ["application/xml"]
    assert requests[0].headers.get_list("x-trace") == ["keep2"]
    assert "ignored_headers" not in out


@pytest.mark.parametrize("method", ALLOWED_METHODS)
def test_allowed_methods_reach_the_wire(method, catalog, wire):
    requests = wire()
    out = server.call_api("ibge-localidades", "/estados", method=method.lower())
    assert out["status"] == 200
    assert requests[0].method == method


@pytest.mark.parametrize("method", ["TRACE", "connect", "PROPFIND", "GET /x HTTP/1.1", ""])
def test_other_methods_are_refused_before_any_request(method, catalog, wire):
    requests = wire()
    out = server.call_api("ibge-localidades", "/estados", method=method)
    assert "not allowed" in out["error"]
    assert out["allowed_methods"] == ALLOWED_METHODS
    assert requests == []


def test_oauth_api_gets_a_bearer_token(oauth_api, wire):
    requests = wire(token_then_api)
    out = server.call_api("oauth-demo", "/things")
    assert out["status"] == 200
    assert [r.url.path for r in requests] == ["/token", "/v1/things"]
    assert requests[1].headers["authorization"] == "Bearer tok"


def test_token_failure_is_reported_as_a_tool_error(oauth_api, wire):
    requests = wire(lambda request: httpx.Response(401, text="LEAKY-BODY"))
    out = server.call_api("oauth-demo", "/things")
    assert out == {
        "error": "oauth-demo: token endpoint returned HTTP 401",
        "token_url": "https://auth.demo.gov.br/token",
    }
    assert [r.url.path for r in requests] == ["/token"]  # the API itself was never called


def test_missing_credentials_are_reported_with_their_status(catalog, wire, monkeypatch):
    monkeypatch.delenv("TRANSPARENCIA_API_KEY", raising=False)
    requests = wire()
    out = server.call_api("portal-transparencia", "/api-de-dados/viagens")
    assert "TRANSPARENCIA_API_KEY" in out["error"]
    assert out["credentials"]["ready"] is False
    assert requests == []


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"body": {"a": 1}}, id="body-on-get"),
        pytest.param({"method": "TRACE"}, id="bad-method"),
        pytest.param({"path": "/../other"}, id="traversal"),
        pytest.param({"path": "/things\n"}, id="control-char-in-path"),
        pytest.param({"headers": {"X-A": "a\r\nB: c"}}, id="header-injection"),
    ],
)
def test_invalid_calls_do_not_cost_a_token_request(kwargs, oauth_api, wire):
    """Caller input is validated before credentials: a bad call must not hit the token endpoint."""
    requests = wire(token_then_api)
    out = server.call_api("oauth-demo", **{"path": "/things", **kwargs})
    assert "error" in out
    assert requests == []


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"path": "/x\x00y"}, id="nul-in-path"),
        pytest.param({"path": "/x\ny"}, id="newline-in-path"),
        pytest.param({"path": "/" + "a" * 70_000}, id="path-too-long"),
        pytest.param({"path": "/x", "headers": {"X-Name": "José"}}, id="non-ascii-header-value"),
        pytest.param({"path": "/x", "headers": {"X-Nömé": "a"}}, id="non-ascii-header-name"),
        pytest.param({"path": "/x", "headers": {"X Name": "a"}}, id="space-in-header-name"),
        pytest.param({"path": "/x", "headers": {"X-A": "a\r\nB: c"}}, id="crlf-in-header-value"),
        pytest.param({"path": "/x", "query": {"a": "\ud800"}}, id="lone-surrogate-in-query"),
        pytest.param(
            {"path": "/x", "method": "POST", "body": {"a": float("nan")}}, id="nan-in-body"
        ),
    ],
)
def test_invalid_input_comes_back_as_an_error_dict(kwargs, catalog, wire):
    """Every bad caller input is a {"error": ...} result, never an exception from the tool."""
    requests = wire()
    out = server.call_api("ibge-localidades", **kwargs)
    assert "error" in out
    assert requests == []


def test_search_catalog_reports_truncation(monkeypatch):
    monkeypatch.setattr(server, "_catalog", Catalog.load())
    out = server.search_catalog("consulta")
    assert out["total_matches"] >= out["shown"]
    assert out["truncated"] is (out["total_matches"] > out["shown"])


def test_list_apis_invalid_filter_explains(monkeypatch):
    monkeypatch.setattr(server, "_catalog", Catalog.load())
    out = server.list_apis(category="Public")
    assert "error" in out and "public" in out["valid_categories"]
