"""govbr-apis-mcp — MCP server.

Gateway pattern: instead of one MCP tool per government endpoint (hundreds of
tools would blow up client context), five generic tools operate over a local
catalog: search/list to find an API, describe to load its OpenAPI on demand,
call to invoke it with the right auth profile.
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any
from urllib.parse import unquote

import httpx
from mcp.server.mcpserver import MCPServer

from . import auth as auth_module
from .catalog import CATEGORIES, STATUSES, ApiEntry, Catalog
from .openapi import describe_operation, fetch_spec, summarize_spec

MAX_BODY_CHARS = 60_000
DEFAULT_LIST_LIMIT = 40
SENSITIVE_PARAM_HINTS = ("key", "token", "secret", "senha", "password", "chave")
ALLOWED_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
# Headers the caller may not set: they would override the catalog-managed credential,
# redirect the request to another virtual host, tamper with its framing, or defeat the
# response size cut (Accept-Encoding stays "identity" so streaming really stops at
# MAX_BODY_CHARS instead of after inflating a compressed chunk). X-Forwarded-* is
# matched by prefix and the entry's own credential header is added per call.
PROTECTED_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "host",
        "cookie",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "te",
        "trailer",
        "keep-alive",
        "forwarded",
        "accept-encoding",
    }
)
HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # an RFC 9110 token
HEADER_VALUE_RE = re.compile(r"[\t\x20-\x7e]*")  # printable ASCII: no CR, LF or NUL

mcp = MCPServer("govbr-apis-mcp")
_catalog: Catalog | None = None
# follow_redirects stays False: these requests carry credentials and must not be
# steered to another host by a 3xx. Spec fetches (openapi.py) opt in explicitly.
_client = httpx.Client(timeout=30, headers={"User-Agent": "govbr-apis-mcp/0.2"})


def get_catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        _catalog = Catalog.load()
    return _catalog


@mcp.tool()
def search_catalog(query: str, category: str | None = None) -> dict[str, Any]:
    """Search the government API catalog by keyword (accent-insensitive; matches
    name, provider, tags and description).

    category filters to: 'public' (free, callable today), 'meia-entrada'
    (half-price ticket eligibility sources), 'serpro' (commercial, needs contract)
    or 'conecta' (requires public-sector adhesion, not callable yet).
    """
    try:
        matches = get_catalog().search(query, category=category)
    except ValueError as exc:
        return {"error": str(exc)}
    shown = matches[:DEFAULT_LIST_LIMIT]
    return {
        "total_matches": len(matches),
        "shown": len(shown),
        "truncated": len(matches) > len(shown),
        "results": [e.summary() for e in shown],
    }


@mcp.tool()
def list_apis(
    category: str | None = None, status: str | None = None, limit: int = DEFAULT_LIST_LIMIT
) -> dict[str, Any]:
    """List catalog entries, most useful with a filter.

    category: public | meia-entrada | serpro | conecta.
    status: available | unstable | manual_only | requires_contract | requires_public_entity.
    Note the catalog holds ~95 Conecta stubs that are not callable yet, so an
    unfiltered listing is mostly noise — filter by category or status.
    """
    try:
        entries = get_catalog().list(category=category, status=status)
    except ValueError as exc:
        return {
            "error": str(exc),
            "valid_categories": list(CATEGORIES),
            "valid_statuses": list(STATUSES),
        }
    shown = entries[: max(1, limit)]
    return {
        "total": len(entries),
        "shown": len(shown),
        "truncated": len(entries) > len(shown),
        "apis": [e.summary() for e in shown],
    }


@mcp.tool()
def describe_api(api_id: str, path: str | None = None, method: str | None = None) -> dict[str, Any]:
    """Describe an API from the catalog.

    Without `path`: catalog metadata plus, when an OpenAPI spec is published, the
    full operation list (method + path + summary). With `path` (templated exactly
    as the spec lists it): parameter/request/response schemas for that operation.

    Paths from the spec are relative to the API's base_url — pass them to call_api
    unchanged.
    """
    try:
        entry = get_catalog().get(api_id)
    except KeyError as exc:
        return {"error": str(exc)}

    result: dict[str, Any] = {"api": entry.detail()}
    result["credentials"] = auth_module.credentials_status(entry)

    if entry.openapi_url:
        try:
            spec = fetch_spec(entry.id, entry.openapi_url, _client)
            result["spec"] = (
                describe_operation(spec, path, method) if path else summarize_spec(spec)
            )
        except Exception as exc:  # noqa: BLE001 — spec issues should not hide catalog metadata
            result["spec_error"] = f"{type(exc).__name__}: {exc}"
    elif path:
        result["spec_error"] = (
            "No OpenAPI spec registered for this API; see docs_url for endpoint details."
        )
    return result


@mcp.tool()
def call_api(
    api_id: str,
    path: str,
    method: str = "GET",
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Call an endpoint of a catalog API, resolving base URL and auth automatically.

    `path` is appended to the API's base_url (e.g. '/estados/mg' for ibge-localidades)
    and may carry its own query string, which is merged with `query`: a key in `query`
    replaces that key in the path, and a list value repeats it (`{"uf": ["SP", "RJ"]}`).
    It cannot escape the base URL's prefix. `method` is GET, HEAD, POST, PUT, PATCH,
    DELETE or OPTIONS. `body` is sent as JSON — only use it with methods that accept a
    payload (POST/PUT/PATCH). `headers` adds request headers; credentials, Host and
    connection-framing headers are managed by the gateway, and any you send come back
    listed in `ignored_headers`.

    Returns {status, url, body}; JSON bodies come back parsed, others as text,
    truncated at 60k chars. Redirects are not followed: a 3xx result carries the target
    in `location`, to call yourself if it is on the same API. Entries flagged
    `pii: true` return personal data (CPF/CNPJ) into this conversation — call those
    only when the user asked for it.
    """
    try:
        entry = get_catalog().get(api_id)
    except KeyError as exc:
        return {"error": str(exc)}

    if not entry.callable:
        return {
            "error": f"{entry.id} is not callable (status: {entry.status}).",
            "hint": entry.notes or entry.docs_url,
        }

    # Validate everything the caller controls before touching credentials, so a bad
    # call never costs an OAuth2 token request.
    method = method.upper()
    if method not in ALLOWED_METHODS:
        return {
            "error": f"method {method!r} is not allowed.",
            "allowed_methods": list(ALLOWED_METHODS),
        }
    if body is not None and method in ("GET", "HEAD", "DELETE"):
        return {
            "error": f"body is not supported with {method}; "
            "use query for parameters, or POST if the API expects a payload."
        }
    try:
        request_headers, ignored = _caller_headers(entry, headers)
        url, params = _build_target(entry.base_url, path, query)
        # Dry run: surfaces what httpx cannot encode (query, body) before any credential.
        _client.build_request(method, url, params=params, json=body, headers=request_headers)
    except (ValueError, httpx.InvalidURL) as exc:
        return {"error": str(exc)}

    # The managed credential goes last, so it always wins.
    try:
        request_headers.update(auth_module.build_headers(entry, _client))
    except auth_module.MissingCredentialsError as exc:
        return {"error": str(exc), "credentials": auth_module.credentials_status(entry)}
    except auth_module.TokenError as exc:
        return {"error": str(exc), "token_url": entry.auth_config.get("token_url")}

    try:
        result = _send(method, url, params, body, request_headers)
    except httpx.HTTPError as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "url": url}

    if ignored:
        result["ignored_headers"] = ignored
    return result


@mcp.tool()
def credentials_status(api_id: str | None = None) -> list[dict[str, Any]]:
    """Show which APIs have credentials configured. Without api_id, reports every
    catalog entry that requires auth, so you can see what is ready to call."""
    catalog = get_catalog()
    try:
        entries = (
            [catalog.get(api_id)]
            if api_id
            else [e for e in catalog.list() if e.auth != "none" and e.category != "conecta"]
        )
    except KeyError as exc:
        return [{"error": str(exc)}]
    return [{"id": e.id, "status": e.status, **auth_module.credentials_status(e)} for e in entries]


def _caller_headers(
    entry: ApiEntry, headers: dict[str, str] | None
) -> tuple[httpx.Headers, list[str]]:
    """Gateway default headers plus the caller's, minus the protected ones.

    httpx.Headers compares names case-insensitively, so `Chave-Api-Dados` and
    `CHAVE-API-DADOS` cannot both ride along next to the managed credential. Returns
    the headers and the sorted names that were ignored; a header httpx or the wire
    would refuse raises ValueError.
    """
    merged = httpx.Headers({"Accept": "application/json", "Accept-Encoding": "identity"})
    managed = (entry.auth_config.get("header") or "").lower()
    ignored: list[str] = []
    for name, value in (headers or {}).items():
        lowered = name.lower()
        if lowered in PROTECTED_HEADERS or lowered.startswith("x-forwarded-") or lowered == managed:
            ignored.append(name)
        elif HEADER_NAME_RE.fullmatch(name) and HEADER_VALUE_RE.fullmatch(value):
            merged[name] = value
        else:
            raise ValueError(
                f"header {name!r} is not valid — names must be HTTP tokens and "
                "values printable ASCII (no line breaks)."
            )
    return merged, sorted(ignored)


def _has_dot_segment(path: str) -> bool:
    """True if `path` has a '.' or '..' segment however a chain of proxies and servers
    might read it: percent-decoded repeatedly, '\\' taken as '/', ';params' dropped."""
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    else:  # still changing after three rounds: nothing legitimate is encoded that deep
        return True
    return any(
        segment.split(";", 1)[0] in (".", "..") for segment in path.replace("\\", "/").split("/")
    )


def _build_target(
    base_url: str, path: str, query: dict[str, Any] | None
) -> tuple[str, httpx.QueryParams]:
    """Join path onto base_url, merging any embedded query string.

    httpx replaces (never merges) a URL's query when `params` is given, so a path
    like PTAX's "/CotacaoDolarDia(dataCotacao=@d)?@d='08-06-2026'" would silently
    lose its parameter. The two are merged as httpx.QueryParams, which keeps repeated
    keys (?uf=SP&uf=RJ); a key in `query` replaces the embedded one entirely.
    Traversal is rejected by looking for dot segments under every decoding a proxy
    chain might apply, then checking the normalized URL still sits under the base prefix.
    """
    raw_path, _, embedded = path.partition("?")
    if _has_dot_segment(raw_path):
        raise ValueError(
            f"path {path!r} contains '..' or '.' segments — "
            "pass a path relative to the API's base URL."
        )
    base = httpx.URL(base_url)
    try:
        candidate = httpx.URL(str(base).rstrip("/") + "/" + raw_path.lstrip("/"))
    except httpx.InvalidURL as exc:  # control characters, over-long paths: not a ValueError
        raise ValueError(f"path is not a valid URL path: {exc}") from None

    # Second line of defence: compare after percent-decoding *and* collapsing dot
    # segments. The comparison needs a segment boundary, or /api-de-dados-admin would
    # pass for a base of /api-de-dados (an empty prefix accepts everything).
    prefix = base.path.rstrip("/")
    normalized = posixpath.normpath(unquote(candidate.path))
    if candidate.host != base.host or not (
        normalized == prefix or normalized.startswith(prefix + "/")
    ):
        raise ValueError(
            f"path {path!r} escapes the API's base URL ({base_url}); "
            "use a path relative to it, without '..' segments."
        )

    params = httpx.QueryParams(embedded)
    if query:
        params = params.merge(query)
    return str(candidate.copy_with(query=None)), params


def _send(
    method: str, url: str, params: httpx.QueryParams, body: Any, headers: httpx.Headers
) -> dict[str, Any]:
    """Stream the response so an oversized payload is cut at the wire, not in memory."""
    with _client.stream(method, url, params=params, json=body, headers=headers) as resp:
        chunks: list[str] = []
        size = 0
        for chunk in resp.iter_text():
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BODY_CHARS:
                break
        text = "".join(chunks)
        content_type = resp.headers.get("content-type")
        request_url = str(resp.request.url)

    truncated = len(text) > MAX_BODY_CHARS
    parsed: Any
    if truncated:
        parsed = {
            "text": text[:MAX_BODY_CHARS],
            "truncated": True,
            "hint": "Response exceeded 60k chars — narrow it with query filters or pagination.",
        }
    elif "json" in (content_type or ""):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = {"text": text, "parse_error": "content-type said JSON but body did not parse"}
    else:
        parsed = text

    result = {
        "status": resp.status_code,
        "url": _redact(request_url),
        "content_type": content_type,
        "body": parsed,
    }
    location = resp.headers.get("location")
    if location and 300 <= resp.status_code < 400:
        result["location"] = _redact(location)
    return result


def _redact(url: str) -> str:
    """Blank out query values whose name suggests a credential, so the echoed URL
    never carries a key into the transcript."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:  # a server-supplied Location can be anything
        return "<invalid URL>"
    if not parsed.query:
        return url
    safe = [
        (k, "***" if any(h in k.lower() for h in SENSITIVE_PARAM_HINTS) else v)
        for k, v in parsed.params.multi_items()
    ]
    return str(parsed.copy_with(params=safe))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
