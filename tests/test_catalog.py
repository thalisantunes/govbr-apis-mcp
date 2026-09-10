
import pytest

from govbr_apis_mcp import auth
from govbr_apis_mcp.catalog import STATUSES, Catalog
from govbr_apis_mcp.openapi import describe_operation, summarize_spec


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return Catalog.load()


def test_catalog_loads_all_categories(catalog: Catalog):
    categories = {e.category for e in catalog.list()}
    assert categories == {"public", "serpro", "conecta", "meia-entrada"}
    assert len(catalog.list(category="conecta")) >= 90


def test_ids_unique_and_wellformed(catalog: Catalog):
    entries = catalog.list()
    assert len({e.id for e in entries}) == len(entries)
    for e in entries:
        assert e.auth in ("none", "api_key", "oauth2_client_credentials"), e.id
        assert e.status in STATUSES, e.id


def test_callable_entries_have_base_url(catalog: Catalog):
    for e in catalog.list(status="available"):
        assert e.base_url, f"{e.id} is available but has no base_url"


def test_search_finds_ibge(catalog: Catalog):
    results = catalog.search("municipios ibge")
    assert results and results[0].id.startswith("ibge-")


def test_search_scopes_by_category(catalog: Catalog):
    results = catalog.search("cpf", category="conecta")
    assert results and all(e.category == "conecta" for e in results)


def test_get_unknown_id_suggests(catalog: Catalog):
    with pytest.raises(KeyError, match="ibge"):
        catalog.get("ibge")


def test_api_key_credentials_status(catalog: Catalog, monkeypatch):
    entry = catalog.get("portal-transparencia")
    monkeypatch.delenv("TRANSPARENCIA_API_KEY", raising=False)
    assert auth.credentials_status(entry)["ready"] is False
    monkeypatch.setenv("TRANSPARENCIA_API_KEY", "x")
    assert auth.credentials_status(entry)["ready"] is True


def test_api_key_header_prefix(catalog: Catalog, monkeypatch):
    entry = catalog.get("datajud")
    monkeypatch.setenv("DATAJUD_API_KEY", "abc")
    headers = auth.build_headers(entry, client=None)
    assert headers == {"Authorization": "APIKey abc"}


def test_missing_api_key_raises_with_hint(catalog: Catalog, monkeypatch):
    entry = catalog.get("portal-transparencia")
    monkeypatch.delenv("TRANSPARENCIA_API_KEY", raising=False)
    with pytest.raises(auth.MissingCredentialsError, match="TRANSPARENCIA_API_KEY"):
        auth.build_headers(entry, client=None)


SPEC = {
    "info": {"title": "Demo", "version": "1"},
    "paths": {
        "/things/{id}": {
            "get": {
                "summary": "Get a thing",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Thing"}
                            }
                        }
                    }
                },
            }
        }
    },
    "components": {
        "schemas": {
            "Thing": {"type": "object", "properties": {"name": {"type": "string"}}}
        }
    },
}


def test_summarize_spec():
    summary = summarize_spec(SPEC)
    assert summary["operations"] == [
        {"method": "GET", "path": "/things/{id}", "summary": "Get a thing"}
    ]


def test_describe_operation_resolves_refs():
    detail = describe_operation(SPEC, "/things/{id}", "get")
    response = detail["operations"]["GET"]["response"]
    assert response["properties"]["name"]["type"] == "string"


def test_describe_operation_unknown_path():
    with pytest.raises(KeyError, match="not in spec"):
        describe_operation(SPEC, "/nope", None)
