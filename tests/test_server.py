"""Tests for the MCP tool layer — URL construction, header precedence, redaction."""

import pytest

from govbr_apis_mcp import server
from govbr_apis_mcp.catalog import Catalog

BASE = "https://api.portaldatransparencia.gov.br/api-de-dados"


def test_embedded_query_is_merged_not_dropped():
    """httpx replaces a URL's query when params is given; PTAX-style paths must survive."""
    url, params = server._build_target(
        "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata",
        "/CotacaoDolarDia(dataCotacao=@d)?@d='08-06-2026'",
        {"$format": "json"},
    )
    assert params == {"@d": "'08-06-2026'", "$format": "json"}
    assert url.endswith("/CotacaoDolarDia(dataCotacao=@d)")


def test_path_only_query_survives_without_dict():
    _, params = server._build_target(BASE, "/x?a=1&b=2", None)
    assert params == {"a": "1", "b": "2"}


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


def test_normal_path_is_preserved():
    url, params = server._build_target(BASE, "servidores", None)
    assert url == f"{BASE}/servidores"
    assert params == {}


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


def test_call_api_unknown_id_returns_error_dict(monkeypatch):
    monkeypatch.setattr(server, "_catalog", Catalog.load())
    out = server.call_api("nao-existe-xyz", "/x")
    assert "error" in out and "Unknown API id" in out["error"]


def test_protected_headers_cannot_override_auth(monkeypatch):
    """A caller-supplied Authorization/Host must never reach the wire."""
    monkeypatch.setattr(server, "_catalog", Catalog.load())
    monkeypatch.setenv("DATAJUD_API_KEY", "REAL-KEY")
    captured = {}

    def fake_send(method, url, params, body, headers):
        captured.update(headers)
        return {"status": 200, "url": url, "content_type": None, "body": "ok"}

    monkeypatch.setattr(server, "_send", fake_send)
    out = server.call_api(
        "datajud",
        "/api_publica_tjsp/_search",
        method="POST",
        body={"query": {}},
        headers={"Authorization": "Bearer ATTACKER", "Host": "internal", "X-Trace": "keep"},
    )
    assert captured["Authorization"] == "APIKey REAL-KEY"
    assert "Host" not in captured
    assert captured["X-Trace"] == "keep"
    assert out["ignored_headers"] == ["Authorization", "Host"]


def test_search_catalog_reports_truncation(monkeypatch):
    monkeypatch.setattr(server, "_catalog", Catalog.load())
    out = server.search_catalog("consulta")
    assert out["total_matches"] >= out["shown"]
    assert out["truncated"] is (out["total_matches"] > out["shown"])


def test_list_apis_invalid_filter_explains(monkeypatch):
    monkeypatch.setattr(server, "_catalog", Catalog.load())
    out = server.list_apis(category="Public")
    assert "error" in out and "public" in out["valid_categories"]
