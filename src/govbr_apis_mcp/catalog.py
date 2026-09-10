"""Catalog loading, validation and search.

The catalog is a set of YAML files in the repo's catalog/ directory. Each file has
an `apis:` list; ids must be unique across files (a duplicate is an error, not a
merge). GOVBR_EXTRA_CATALOG_DIR adds a second directory whose entries override
same-id entries from the base catalog — that is the supported way to fill in
credentials/base URLs (e.g. Conecta after adhesion) without forking the repo.

Entries are validated on load: ids are filename-safe and every URL must be https
on an allowlisted government host. The catalog decides where credentials are sent,
so a malicious entry would otherwise exfiltrate them to an arbitrary host.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

DEFAULT_CATALOG_DIR = Path(__file__).resolve().parents[2] / "catalog"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

CATEGORIES = ("public", "serpro", "conecta", "meia-entrada")
STATUSES = (
    "available",  # callable now
    "unstable",  # callable, but the upstream is flaky
    "manual_only",  # a validation route exists, but not as a callable API
    "requires_contract",  # needs a commercial contract (Serpro)
    "requires_public_entity",  # needs Conecta adhesion as a public-sector body
)
AUTH_PROFILES = ("none", "api_key", "oauth2_client_credentials")

ALLOWED_HOST_SUFFIXES = (".gov.br", ".leg.br", ".jus.br")
ALLOWED_HOSTS = {
    "brasilapi.com.br",
    "api-prod.meiaentrada.org.br",
    "www.meiaentrada.org.br",
    "meiaentrada.org.br",
    "validade.fesn.org.br",
}
URL_FIELDS = ("base_url", "openapi_url", "docs_url")


def extra_allowed_hosts() -> set[str]:
    """Hosts added by the operator via GOVBR_EXTRA_ALLOWED_HOSTS (comma-separated)."""
    raw = os.environ.get("GOVBR_EXTRA_ALLOWED_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def host_allowed(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    if host in ALLOWED_HOSTS or host in extra_allowed_hosts():
        return True
    return any(host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def fold(text: str) -> str:
    """Lowercase and strip accents, so 'municípios' and 'municipios' both match."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


@dataclass
class ApiEntry:
    id: str
    name: str
    provider: str
    category: str
    status: str
    auth: str
    description: str = ""
    base_url: str | None = None
    openapi_url: str | None = None
    docs_url: str | None = None
    auth_config: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    notes: str | None = None
    community: bool = False
    pii: bool = False

    @property
    def callable(self) -> bool:
        return self.base_url is not None and self.status in ("available", "unstable")

    def validate(self, source: str) -> None:
        where = f"{source}:{self.id}"
        if not ID_RE.match(self.id):
            raise ValueError(
                f"{where}: invalid id — must match {ID_RE.pattern} "
                "(ids become cache lookup keys and must not contain path separators)"
            )
        for name, value, allowed in (
            ("category", self.category, CATEGORIES),
            ("status", self.status, STATUSES),
            ("auth", self.auth, AUTH_PROFILES),
        ):
            if value not in allowed:
                raise ValueError(f"{where}: {name}={value!r} not in {list(allowed)}")

        for field_name in URL_FIELDS:
            url = getattr(self, field_name)
            if url is None:
                continue
            if not url.startswith("https://"):
                raise ValueError(f"{where}: {field_name} must use https — got {url!r}")
            if not host_allowed(url):
                raise ValueError(
                    f"{where}: {field_name} host is not allowlisted ({url!r}). "
                    "Add it to GOVBR_EXTRA_ALLOWED_HOSTS only if you trust it with "
                    "the credentials this entry uses."
                )

        token_url = self.auth_config.get("token_url")
        if token_url and (not token_url.startswith("https://") or not host_allowed(token_url)):
            raise ValueError(f"{where}: auth_config.token_url not https/allowlisted ({token_url!r})")
        if self.auth == "api_key" and not self.auth_config.get("header"):
            raise ValueError(f"{where}: auth=api_key requires auth_config.header")
        if self.auth == "oauth2_client_credentials" and self.callable and not token_url:
            raise ValueError(f"{where}: callable oauth2 entry requires auth_config.token_url")
        if self.status in ("available", "unstable") and not self.base_url:
            raise ValueError(f"{where}: status={self.status} requires base_url")

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "category": self.category,
            "status": self.status,
            "auth": self.auth,
            "description": self.description,
        }
        if self.community:
            out["community"] = True
        if self.pii:
            out["pii"] = True
        return out

    def detail(self) -> dict[str, Any]:
        out = self.summary()
        out.update(
            {
                "base_url": self.base_url,
                "openapi_url": self.openapi_url,
                "docs_url": self.docs_url,
                "tags": self.tags,
                "notes": self.notes,
            }
        )
        if self.auth != "none":
            out["auth_config"] = dict(self.auth_config)
        return {k: v for k, v in out.items() if v not in (None, [], {})}


class Catalog:
    def __init__(self, entries: dict[str, ApiEntry]):
        self._entries = entries

    @classmethod
    def load(cls, catalog_dir: str | Path | None = None) -> Catalog:
        base_dir = Path(catalog_dir or os.environ.get("GOVBR_CATALOG_DIR") or DEFAULT_CATALOG_DIR)
        entries = cls._load_dir(base_dir, allow_override=False)
        if not entries:
            raise FileNotFoundError(f"No catalog YAML files found in {base_dir}")

        extra_dir = os.environ.get("GOVBR_EXTRA_CATALOG_DIR")
        if extra_dir:
            entries.update(cls._load_dir(Path(extra_dir), allow_override=True))
        return cls(entries)

    @staticmethod
    def _load_dir(directory: Path, allow_override: bool) -> dict[str, ApiEntry]:
        entries: dict[str, ApiEntry] = {}
        for path in sorted(directory.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for raw in data.get("apis", []):
                try:
                    entry = ApiEntry(**raw)
                except TypeError as exc:
                    raise ValueError(f"{path.name}: invalid entry fields — {exc}") from None
                entry.validate(path.name)
                if entry.id in entries and not allow_override:
                    raise ValueError(f"Duplicate API id {entry.id!r} in {path.name}")
                entries[entry.id] = entry
        return entries

    def get(self, api_id: str) -> ApiEntry:
        try:
            return self._entries[api_id]
        except KeyError:
            close = [e.id for e in self.search(api_id)[:5]]
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise KeyError(f"Unknown API id {api_id!r}.{hint}") from None

    def list(self, category: str | None = None, status: str | None = None) -> list[ApiEntry]:
        if category is not None and category not in CATEGORIES:
            raise ValueError(f"Unknown category {category!r}. Valid values: {list(CATEGORIES)}")
        if status is not None and status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}. Valid values: {list(STATUSES)}")
        out = list(self._entries.values())
        if category:
            out = [e for e in out if e.category == category]
        if status:
            out = [e for e in out if e.status == status]
        return sorted(out, key=lambda e: (e.category, e.id))

    def search(self, query: str, category: str | None = None) -> list[ApiEntry]:
        """Accent-insensitive token search over name, id, provider, tags and description."""
        tokens = [t for t in fold(query).split() if t]
        scored: list[tuple[float, ApiEntry]] = []
        for entry in self.list(category=category):
            name = fold(f"{entry.name} {entry.id}")
            meta = fold(f"{' '.join(entry.tags)} {entry.provider}")
            description = fold(entry.description)
            score = 0.0
            for token in tokens:
                if token in name:
                    score += 3
                elif token in meta:
                    score += 2
                elif token in description:
                    score += 1
            if score:
                scored.append((score + (0.5 if entry.callable else 0), entry))
        scored.sort(key=lambda pair: -pair[0])
        return [entry for _, entry in scored]
