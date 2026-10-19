import pytest
import yaml

from govbr_apis_mcp import auth
from govbr_apis_mcp.catalog import (
    DEFAULT_CATALOG_DIR,
    STATUSES,
    ApiEntry,
    Catalog,
    extra_allowed_hosts,
    host_allowed,
)
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
    # every URL must be https ...
    ({"base_url": "http://api.demo.gov.br/v1"}, "base_url must use https"),
    ({"openapi_url": "http://api.demo.gov.br/spec.json"}, "openapi_url must use https"),
    ({"docs_url": "http://api.demo.gov.br/docs"}, "docs_url must use https"),
    ({"base_url": "//api.demo.gov.br/v1"}, "base_url must use https"),
    (
        {
            "auth": "oauth2_client_credentials",
            "auth_config": {"token_url": "http://a.demo.gov.br/t"},
        },
        "token_url not https/allowlisted",
    ),
    # ... and point at an allowlisted host
    ({"base_url": "https://api.evil.com/v1"}, "base_url host is not allowlisted"),
    ({"base_url": "https://notgov.br/v1"}, "base_url host is not allowlisted"),
    ({"openapi_url": "https://api.evil.com/spec.json"}, "openapi_url host is not allowlisted"),
    ({"docs_url": "https://api.evil.com/docs"}, "docs_url host is not allowlisted"),
    (
        {"auth": "oauth2_client_credentials", "auth_config": {"token_url": "https://evil.com/t"}},
        "token_url not https/allowlisted",
    ),
    # ids become cache and file names
    ({"id": "Bad Id"}, "invalid id"),
    ({"id": "../etc/passwd"}, "invalid id"),
    ({"id": ""}, "invalid id"),
    ({"id": "-leading-dash"}, "invalid id"),
    # re.match with "$" lets a trailing newline through
    ({"id": "ibge\n"}, "invalid id"),
    # closed vocabularies
    ({"category": "nope"}, "category='nope'"),
    ({"status": "nope"}, "status='nope'"),
    ({"auth": "nope"}, "auth='nope'"),
    # auth profiles need what auth.py reads
    ({"auth": "api_key", "auth_config": {"env": "X_KEY"}}, "requires auth_config.header"),
    # auth=api_key needs the env var name, or the key can never be found
    ({"auth": "api_key", "auth_config": {"header": "X-Key"}}, "requires auth_config.env"),
    ({"auth": "oauth2_client_credentials"}, "callable oauth2 entry requires auth_config.token_url"),
    # callable statuses need somewhere to call
    ({"status": "available", "base_url": None}, "status=available requires base_url"),
    ({"status": "unstable", "base_url": None}, "status=unstable requires base_url"),
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


def test_unknown_entry_field_is_rejected(tmp_path, clean_env):
    write_catalog(tmp_path / "base", "x.yaml", demo_entry(colour="red"))
    with pytest.raises(ValueError, match="invalid entry fields"):
        Catalog.load(tmp_path / "base")


def test_validate_accepts_well_formed_entries(clean_env):
    api_key = {"header": "X-Key", "env": "X_KEY"}
    oauth = {"token_url": "https://auth.demo.gov.br/token"}
    for fields in (
        {},
        {"auth": "api_key", "auth_config": api_key},
        {"auth": "oauth2_client_credentials", "auth_config": oauth},
        {"openapi_url": "https://api.demo.gov.br/spec.json", "docs_url": "https://demo.gov.br/d"},
        # a stub: nothing to call yet, so no base_url and no token_url required
        {"status": "requires_public_entity", "base_url": None, "auth": "oauth2_client_credentials"},
    ):
        ApiEntry(**demo_entry(**fields)).validate("test.yaml")


def test_extra_allowed_host_lets_an_entry_validate(clean_env):
    entry = ApiEntry(**demo_entry(base_url="https://api.example.org/v1"))
    with pytest.raises(ValueError, match="not allowlisted"):
        entry.validate("test.yaml")
    clean_env.setenv("GOVBR_EXTRA_ALLOWED_HOSTS", "api.example.org")
    entry.validate("test.yaml")


# --- host allowlist -----------------------------------------------------------------------

HOST_CASES = [
    # allowed: the suffix tuple, with any depth of subdomains
    ("https://sub.gov.br/x", True),
    ("https://a.b.sub.gov.br/x", True),
    ("https://sub.leg.br/x", True),
    ("https://sub.jus.br/x", True),
    # allowed: exact-match entries
    ("https://brasilapi.com.br/api", True),
    # case, port and userinfo do not change which host is meant
    ("https://SUB.GOV.BR/x", True),
    ("https://sub.gov.br:8443/x", True),
    ("https://evil.com@sub.gov.br/x", True),
    # the scheme is not host_allowed's job: validate and fetch_spec require https themselves
    ("http://sub.gov.br/x", True),
    # a suffix needs its dot, and a host must not merely contain an allowed name
    ("https://notgov.br/x", False),
    ("https://evilgov.br/x", False),
    ("https://gov.br.evil.com/x", False),
    ("https://sub.gov.br.evil.com/x", False),
    ("https://evil.com/sub.gov.br", False),
    ("https://evil.com/?next=sub.gov.br", False),
    ("https://evil.com/#sub.gov.br", False),
    ("https://sub.gov.br%2eevil.com/x", False),
    # userinfo can name an allowed host while the real one is another
    ("https://sub.gov.br@evil.com/x", False),
    ("https://sub.gov.br:443@evil.com/x", False),
    ("https://sub.gov.br\\@evil.com/x", False),
    # exact-match entries do not cover their subdomains
    ("https://sub.brasilapi.com.br/x", False),
    # a trailing dot (rooted FQDN) is refused: conservative, and nothing in the catalog needs it
    ("https://sub.gov.br./x", False),
    # a hostname is plain ASCII letters, digits, dots and hyphens: junk before an allowed
    # suffix is no subdomain, whatever a resolver would make of it
    ("https://evil.com;.gov.br/x", False),
    ("https://evil.com%2f.gov.br/x", False),
    ("https://evil.com\\.gov.br/x", False),
    # malformed URLs fail closed instead of raising
    ("https://[::1/x", False),
    ("https://::1]/x", False),
    # not URLs with a host at all
    ("sub.gov.br/x", False),
    ("https:///x", False),
    ("not a url", False),
    ("", False),
]


@pytest.mark.parametrize(("url", "allowed"), HOST_CASES)
def test_host_allowed(url, allowed, clean_env):
    assert host_allowed(url) is allowed


def test_extra_allowed_hosts_are_trimmed_lowercased_and_split_on_commas(clean_env):
    assert extra_allowed_hosts() == set()
    clean_env.setenv("GOVBR_EXTRA_ALLOWED_HOSTS", "")
    assert extra_allowed_hosts() == set()
    clean_env.setenv("GOVBR_EXTRA_ALLOWED_HOSTS", "  Example.org , other.test,, ,\tthird.test ")
    assert extra_allowed_hosts() == {"example.org", "other.test", "third.test"}


def test_extra_allowed_hosts_are_exact_matches_on_top_of_the_built_in_list(clean_env):
    assert not host_allowed("https://example.org/x")
    clean_env.setenv("GOVBR_EXTRA_ALLOWED_HOSTS", " Example.org , other.test ")
    assert host_allowed("https://example.org/x")
    assert host_allowed("https://EXAMPLE.ORG/x")
    assert host_allowed("https://other.test/x")
    assert host_allowed("https://sub.gov.br/x")  # the built-in list still applies
    assert not host_allowed("https://sub.example.org/x")  # exact host, no subdomains
    assert not host_allowed("https://notexample.org/x")
