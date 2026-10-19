import pytest
import yaml

from govbr_apis_mcp import auth
from govbr_apis_mcp.catalog import DEFAULT_CATALOG_DIR, STATUSES, ApiEntry, Catalog
from govbr_apis_mcp.openapi import describe_operation, summarize_spec


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return Catalog.load()


def test_catalog_loads_all_categories(catalog: Catalog):
    categories = {e.category for e in catalog.list()}
    assert categories == {"public", "serpro", "conecta", "meia-entrada"}
    assert len(catalog.list(category="conecta")) >= 90


def test_default_catalog_dir_has_the_four_tier_files():
    assert DEFAULT_CATALOG_DIR.is_dir()
    assert sorted(path.name for path in DEFAULT_CATALOG_DIR.glob("*.yaml")) == [
        "conecta.yaml",
        "meia_entrada.yaml",
        "public.yaml",
        "serpro.yaml",
    ]


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
                            "application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}
                        }
                    }
                },
            }
        }
    },
    "components": {
        "schemas": {"Thing": {"type": "object", "properties": {"name": {"type": "string"}}}}
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


# --- catalog directories, validation and ordering -----------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("GOVBR_CATALOG_DIR", "GOVBR_EXTRA_CATALOG_DIR", "GOVBR_EXTRA_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def demo_entry(**overrides) -> dict:
    entry = {
        "id": "demo",
        "name": "Demo",
        "provider": "Demo",
        "category": "public",
        "status": "available",
        "auth": "none",
        "base_url": "https://api.demo.gov.br/v1",
    }
    entry.update(overrides)
    return entry


def write_catalog(directory, filename, *entries) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(yaml.safe_dump({"apis": list(entries)}), encoding="utf-8")


def test_missing_extra_catalog_dir_is_an_error(tmp_path, clean_env):
    clean_env.setenv("GOVBR_EXTRA_CATALOG_DIR", str(tmp_path / "nope"))
    with pytest.raises(ValueError, match="GOVBR_EXTRA_CATALOG_DIR.*not an existing directory"):
        Catalog.load()


def test_extra_catalog_dir_without_entries_is_an_error(tmp_path, clean_env):
    (tmp_path / "extra").mkdir()
    (tmp_path / "extra" / "notes.txt").write_text("not a catalog", encoding="utf-8")
    clean_env.setenv("GOVBR_EXTRA_CATALOG_DIR", str(tmp_path / "extra"))
    with pytest.raises(ValueError, match="GOVBR_EXTRA_CATALOG_DIR.*no catalog entries"):
        Catalog.load()


def test_extra_catalog_accepts_yml_and_overrides_base_entries(tmp_path, clean_env):
    write_catalog(
        tmp_path / "extra",
        "mine.yml",
        demo_entry(id="ibge-localidades", name="Overridden"),
        demo_entry(id="brand-new"),
    )
    clean_env.setenv("GOVBR_EXTRA_CATALOG_DIR", str(tmp_path / "extra"))
    catalog = Catalog.load()
    assert catalog.get("ibge-localidades").name == "Overridden"
    assert catalog.get("brand-new").base_url == "https://api.demo.gov.br/v1"
    assert catalog.get("brasilapi").name  # base entries the extra dir does not touch survive


def test_duplicate_ids_across_files_of_the_extra_dir_are_an_error(tmp_path, clean_env):
    write_catalog(tmp_path / "extra", "a.yaml", demo_entry())
    write_catalog(tmp_path / "extra", "b.yml", demo_entry(name="Second"))
    clean_env.setenv("GOVBR_EXTRA_CATALOG_DIR", str(tmp_path / "extra"))
    with pytest.raises(ValueError, match="Duplicate API id 'demo'"):
        Catalog.load()


def test_duplicate_ids_inside_one_extra_file_are_an_error(tmp_path, clean_env):
    write_catalog(tmp_path / "extra", "a.yaml", demo_entry(), demo_entry(name="Again"))
    clean_env.setenv("GOVBR_EXTRA_CATALOG_DIR", str(tmp_path / "extra"))
    with pytest.raises(ValueError, match="Duplicate API id 'demo'"):
        Catalog.load()


def test_base_dir_reads_yml_too_and_duplicates_across_extensions_fail(tmp_path, clean_env):
    base = tmp_path / "base"
    write_catalog(base, "one.yaml", demo_entry())
    write_catalog(base, "two.yml", demo_entry(id="other"))
    assert sorted(e.id for e in Catalog.load(base).list()) == ["demo", "other"]
    write_catalog(base, "three.yml", demo_entry())
    with pytest.raises(ValueError, match="Duplicate API id 'demo'"):
        Catalog.load(base)


REJECTED_ENTRIES = [
    # auth=api_key needs the env var name, or the key can never be found
    ({"auth": "api_key", "auth_config": {"header": "X-Key"}}, "requires auth_config.env"),
    # re.match with "$" lets a trailing newline through
    ({"id": "ibge\n"}, "invalid id"),
]


@pytest.mark.parametrize(("overrides", "message"), REJECTED_ENTRIES)
def test_validate_rejects(overrides, message, clean_env):
    entry = ApiEntry(**demo_entry(**overrides))
    with pytest.raises(ValueError, match=message):
        entry.validate("test.yaml")


def test_list_puts_callable_entries_first(catalog: Catalog):
    entries = catalog.list()
    callables = sum(e.callable for e in entries)
    assert 0 < callables < len(entries)
    assert all(e.callable for e in entries[:callables])
    assert not any(e.callable for e in entries[callables:])
    for block in (entries[:callables], entries[callables:]):
        assert block == sorted(block, key=lambda e: (e.category, e.id))
