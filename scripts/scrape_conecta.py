#!/usr/bin/env python3
"""Scrape the Conecta gov.br catalog listing into src/govbr_apis_mcp/catalog_data/conecta.yaml.

The Conecta catalog page is static HTML with one <a class="api-card"> per API.
All Conecta APIs require adhesion as a public-sector entity, so every entry is
generated with status: requires_public_entity and no callable base_url yet.

The output file is replaced in one step (a temporary file next to it, then os.replace)
and only by a result the catalog would accept. A page that yields no cards, or a card
whose id fails the catalog's id rule, aborts with exit status 1 and leaves the file
untouched. So does a result with fewer than half the entries the file has now, which
looks like a layout change; --force overrides that last check only.

Usage: python scripts/scrape_conecta.py [--force] [output.yaml]
Run it where govbr_apis_mcp is importable (pip install -e .), so the id rule is shared.
"""

import argparse
import html
import os
import re
import sys
import urllib.request
from pathlib import Path

import yaml

from govbr_apis_mcp.catalog import ID_RE

CATALOG_URL = "https://www.gov.br/conecta/catalogo/"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "src/govbr_apis_mcp/catalog_data/conecta.yaml"
CARD_RE = re.compile(
    r'<a href="(?P<url>https://www\.gov\.br/conecta/catalogo/apis/[^"]+)"\s+class="api-card">'
    r".*?<p class=\"title\">(?P<title>.*?)</p>"
    r".*?<p class=\"desc\">(?P<desc>.*?)</p>"
    r"(?P<rest>.*?)</a>",
    re.DOTALL,
)
ORGAO_RE = re.compile(r'class="orgao".*?<span class="orgao-title">(.*?)</span>', re.DOTALL)
TAG_RE = re.compile(r'<span class="tag \w+">(.*?)</span>')


def clean(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\s+", " ", text).strip()


def count_entries(path: Path) -> int:
    """How many APIs the file at `path` holds now; 0 when it is missing or unreadable."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return 0
    apis = data.get("apis") if isinstance(data, dict) else None
    return len(apis) if isinstance(apis, list) else 0


def write_atomic(path: Path, text: str) -> None:
    """Replace `path` in one step, so a crash leaves the old file, never half of a new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape the Conecta gov.br catalog into YAML.")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=DEFAULT_OUT,
        help="where to write the catalog (default: the packaged conecta.yaml)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="write even when the page yields fewer than half the entries the file has now",
    )
    args = parser.parse_args(argv)
    out_path = args.output

    with urllib.request.urlopen(CATALOG_URL, timeout=30) as resp:
        page = resp.read().decode("utf-8")

    seen: set[str] = set()
    apis = []
    for m in CARD_RE.finditer(page):
        slug = m.group("url").rstrip("/").rsplit("/", 1)[-1]
        if slug in seen:
            continue
        seen.add(slug)
        name = clean(m.group("title"))
        orgao = ORGAO_RE.search(m.group("rest"))
        if not orgao:
            print(
                f"warning: card {name!r} has no orgao block; provider falls back to Conecta gov.br",
                file=sys.stderr,
            )
        tags = [clean(t).lower() for t in TAG_RE.findall(m.group("rest"))]
        apis.append(
            {
                "id": f"conecta-{slug}",
                "name": name,
                "provider": clean(orgao.group(1)) if orgao else "Conecta gov.br",
                "category": "conecta",
                "status": "requires_public_entity",
                "auth": "oauth2_client_credentials",
                "docs_url": m.group("url"),
                "description": clean(m.group("desc")),
                "tags": tags,
                "notes": (
                    "Requires adhesion to Conecta gov.br as a public-sector entity. "
                    "Gateway: https://apigateway.conectagov.estaleiro.serpro.gov.br "
                    "(per-API path; see docs_url for the adhesion process and Swagger)."
                ),
            }
        )

    if not apis:
        print(
            f"error: no API cards found at {CATALOG_URL} (did the page layout change?); "
            f"{out_path} was left untouched",
            file=sys.stderr,
        )
        sys.exit(1)
    invalid = [api["id"] for api in apis if not ID_RE.fullmatch(api["id"])]
    if invalid:
        print(
            f"error: {len(invalid)} id(s) the catalog would reject "
            f"(must match {ID_RE.pattern}): {', '.join(map(repr, invalid))}; "
            f"{out_path} was left untouched",
            file=sys.stderr,
        )
        sys.exit(1)

    current = count_entries(out_path)
    if len(apis) * 2 < current and not args.force:
        print(
            f"error: the page yields {len(apis)} API(s) but {out_path} has {current}; "
            "losing more than half looks like a layout change (re-run with --force to "
            f"write it anyway); {out_path} was left untouched",
            file=sys.stderr,
        )
        sys.exit(1)

    header = (
        "# Generated by scripts/scrape_conecta.py — do not edit by hand.\n"
        f"# Source: {CATALOG_URL}\n"
    )
    body = yaml.safe_dump({"apis": apis}, allow_unicode=True, sort_keys=False, width=100)
    write_atomic(out_path, header + body)
    print(f"{len(apis)} APIs written to {out_path}")


if __name__ == "__main__":
    main()
