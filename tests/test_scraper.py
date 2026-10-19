"""Tests for scripts/scrape_conecta.py. The Conecta page is canned: nothing touches the network."""

import importlib.util
import io
import urllib.request
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scrape_conecta.py"
CURRENT = "# the catalog as it is now\napis: []\n"


@pytest.fixture(scope="module")
def scraper():
    spec = importlib.util.spec_from_file_location("scrape_conecta", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def card(slug, title="Name", desc="Description", orgao="Agency"):
    block = f'<div class="orgao"><span class="orgao-title">{orgao}</span></div>' if orgao else ""
    return (
        f'<a href="https://www.gov.br/conecta/catalogo/apis/{slug}" class="api-card">'
        f'<p class="title">{title}</p><p class="desc">{desc}</p>{block}'
        '<span class="tag federal">Federal</span></a>'
    )


def scrape(scraper, monkeypatch, out, page, *flags):
    def fake_urlopen(url, timeout=None):
        return io.BytesIO(page.encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    scraper.main([*flags, str(out)])


def scrape_aborts(scraper, monkeypatch, out, page, *flags):
    with pytest.raises(SystemExit) as excinfo:
        scrape(scraper, monkeypatch, out, page, *flags)
    assert excinfo.value.code == 1


def write_catalog(path, entries):
    apis = [{"id": f"conecta-old-{n}"} for n in range(entries)]
    path.write_text(yaml.safe_dump({"apis": apis}), encoding="utf-8")


def ids(path):
    return [api["id"] for api in yaml.safe_load(path.read_text(encoding="utf-8"))["apis"]]


def test_a_page_without_cards_leaves_the_file_alone(scraper, monkeypatch, tmp_path, capsys):
    out = tmp_path / "conecta.yaml"
    out.write_text(CURRENT, encoding="utf-8")
    scrape_aborts(scraper, monkeypatch, out, "<html>the layout changed</html>")
    assert "no API cards found" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == CURRENT


def test_an_id_the_catalog_would_reject_aborts_and_is_listed(
    scraper, monkeypatch, tmp_path, capsys
):
    out = tmp_path / "conecta.yaml"
    out.write_text(CURRENT, encoding="utf-8")
    page = card("ok-one") + card("Bad_Upper") + card("caf%C3%A9")
    scrape_aborts(scraper, monkeypatch, out, page)
    err = capsys.readouterr().err
    assert "conecta-Bad_Upper" in err and "conecta-caf%C3%A9" in err
    assert "conecta-ok-one" not in err
    assert out.read_text(encoding="utf-8") == CURRENT


def test_losing_more_than_half_the_entries_aborts_unless_forced(
    scraper, monkeypatch, tmp_path, capsys
):
    out = tmp_path / "conecta.yaml"
    write_catalog(out, 4)
    before = out.read_text(encoding="utf-8")
    scrape_aborts(scraper, monkeypatch, out, card("one"))
    err = capsys.readouterr().err
    assert "yields 1 API(s)" in err and "has 4;" in err and "--force" in err
    assert out.read_text(encoding="utf-8") == before

    scrape(scraper, monkeypatch, out, card("one"), "--force")
    assert ids(out) == ["conecta-one"]


def test_keeping_half_the_entries_needs_no_force(scraper, monkeypatch, tmp_path):
    out = tmp_path / "conecta.yaml"
    write_catalog(out, 4)
    scrape(scraper, monkeypatch, out, card("one") + card("two"))
    assert ids(out) == ["conecta-one", "conecta-two"]


def test_force_does_not_write_an_empty_page(scraper, monkeypatch, tmp_path):
    out = tmp_path / "conecta.yaml"
    out.write_text(CURRENT, encoding="utf-8")
    scrape_aborts(scraper, monkeypatch, out, "<html></html>", "--force")
    assert out.read_text(encoding="utf-8") == CURRENT


def test_a_valid_page_is_written_unescaped_with_a_provider_fallback(
    scraper, monkeypatch, tmp_path, capsys
):
    out = tmp_path / "conecta.yaml"
    page = card(
        "api-one",
        title="Name &amp; Co",
        desc="Descri&ccedil;&atilde;o&nbsp;with <b>markup</b> and &lt;text&gt;",
        orgao="Ministry &amp; Co",
    ) + card("api-two", title="No agency", orgao=None)
    scrape(scraper, monkeypatch, out, page)

    first, second = yaml.safe_load(out.read_text(encoding="utf-8"))["apis"]
    assert first["name"] == "Name & Co"
    assert first["description"] == "Descrição with markup and <text>"
    assert first["provider"] == "Ministry & Co"
    assert second["provider"] == "Conecta gov.br"
    assert "warning: card 'No agency' has no orgao block" in capsys.readouterr().err
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_failed_replace_keeps_the_old_file_and_leaves_no_temp_file(
    scraper, monkeypatch, tmp_path
):
    out = tmp_path / "conecta.yaml"
    out.write_text(CURRENT, encoding="utf-8")

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(scraper.os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        scrape(scraper, monkeypatch, out, card("one"))
    assert out.read_text(encoding="utf-8") == CURRENT
    assert list(tmp_path.glob("*.tmp")) == []
