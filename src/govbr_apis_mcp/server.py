"""govbr-apis-mcp — MCP server.

Gateway pattern: instead of one MCP tool per government endpoint (hundreds of
tools would blow up client context), five generic tools operate over a local
catalog: search/list to find an API, describe to load its OpenAPI on demand,
call to invoke it with the right auth profile.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
from typing import Any
from urllib.parse import unquote

import httpx
from mcp.server.mcpserver import MCPServer

from . import __version__
from . import auth as auth_module
from .catalog import CATEGORIES, STATUSES, ApiEntry, Catalog
from .openapi import describe_operation, fetch_spec, summarize_spec

MAX_BODY_CHARS = 60_000
DEFAULT_LIST_LIMIT = 40
SENSITIVE_PARAM_HINTS = ("key", "token", "secret", "senha", "password", "chave")
ALLOWED_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
# Headers the caller may not set: they would override the catalog-managed credential,
# redirect the request to another virtual host, tamper with its framing, make a proxy in
# front of the API route, rewrite or re-method the request, or vouch for the client's IP,
# or defeat the response size cut (Accept-Encoding stays "identity" so streaming really
# stops at MAX_BODY_CHARS instead of after inflating a compressed chunk). X-Forwarded-*
# is matched by prefix and the entry's own credential header is added per call. Names are
# compared with '_' read as '-', as CGI/WSGI/PHP backends do.
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
        "x-http-method-override",
        "x-http-method",
        "x-method-override",
        "x-original-url",
        "x-rewrite-url",
        "x-real-ip",
        "x-client-ip",
        "x-originating-ip",
        "true-client-ip",
        "cf-connecting-ip",
        "client-ip",
        "proxy-connection",
        "via",
        "max-forwards",
    }
)
HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # an RFC 9110 token

mcp = MCPServer("govbr-apis-mcp", version=__version__)
_catalog: Catalog | None = None
# follow_redirects stays False: these requests carry credentials, so a 3xx must not steer them.
# Redirect handling for spec fetches lives in openapi.py.
_client = httpx.Client(timeout=30, headers={"User-Agent": f"govbr-apis-mcp/{__version__}"})


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
    """Call an endpoint of a catalog API; base URL and credentials are resolved for you.

    `path` is relative to the API's base_url (e.g. '/estados/mg') and may carry a query
    string; `query` overrides its keys, and a list value repeats a key. `method` is GET,
    HEAD, POST, PUT, PATCH, DELETE or OPTIONS; send `body` (JSON) only with POST/PUT/PATCH.
    Credential, Host, proxy and framing headers in `headers` are dropped and listed in
    `ignored_headers`.

    Returns {status, url, body} (JSON parsed, text cut at 60k chars); a 3xx is not followed
    and carries its absolute `location`. Problems come back as {error}. Entries flagged
    `pii: true` return personal data (CPF/CNPJ): call them only when the user asked.
    """
    try:
        entry = get_catalog().get(api_id)
    except KeyError as exc:
        return {"error": str(exc)}

    if not _callable_now(entry):
        hint = entry.notes or entry.docs_url
        if entry.status == "requires_contract" and entry.base_url is not None:
            env_vars = auth_module.credentials_status(entry)["env_vars"]
            missing = [var for var, is_set in env_vars.items() if not is_set]
            if missing:
                setup = f"Set {', '.join(missing)} to use your contract credentials."
                hint = " ".join(filter(None, [setup, hint]))
        return {
            "error": f"{entry.id} is not callable (status: {entry.status}).",
            "hint": hint,
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
    except httpx.LocalProtocolError as exc:
        # h11 quotes the header it rejects, and that header can be the credential
        message = "the HTTP library rejected the request as malformed."
        return {"error": f"{type(exc).__name__}: {message}", "url": url}
    except httpx.HTTPError as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "url": url}

    if ignored:
        result["ignored_headers"] = ignored
    return result


@mcp.tool()
def credentials_status(api_id: str | None = None) -> list[dict[str, Any]]:
    """Show which APIs have credentials configured. Without api_id, reports every
    catalog entry that requires auth, so you can see what is ready to call.

    `ready` says whether the credential env vars are set; `callable` whether call_api
    accepts the entry right now (a requires_contract entry, such as Serpro's, only does
    once they are).
    """
    catalog = get_catalog()
    try:
        entries = (
            [catalog.get(api_id)]
            if api_id
            else [e for e in catalog.list() if e.auth != "none" and e.category != "conecta"]
        )
    except KeyError as exc:
        return [{"error": str(exc)}]
    return [
        {
            "id": e.id,
            "status": e.status,
            **auth_module.credentials_status(e),
            "callable": _callable_now(e),
        }
        for e in entries
    ]


def _callable_now(entry: ApiEntry) -> bool:
    """Whether call_api accepts the entry: available/unstable ones, plus requires_contract
    ones once their credentials are in the environment (a Serpro contract is just that)."""
    if entry.callable:
        return True
    return (
        entry.status == "requires_contract"
        and entry.base_url is not None
        and bool(auth_module.credentials_status(entry)["ready"])
    )


def _caller_headers(
    entry: ApiEntry, headers: dict[str, str] | None
) -> tuple[httpx.Headers, list[str]]:
    """Gateway default headers plus the caller's, minus the protected ones.

    httpx.Headers compares names case-insensitively, so `Chave-Api-Dados` and
    `CHAVE-API-DADOS` cannot both ride along next to the managed credential; `_` is read
    as `-` too. Returns the headers and the sorted names that were ignored; a header
    httpx or the wire would refuse raises ValueError.
    """
    merged = httpx.Headers({"Accept": "application/json", "Accept-Encoding": "identity"})
    managed = (entry.auth_config.get("header") or "").lower().replace("_", "-")
    ignored: list[str] = []
    for name, value in (headers or {}).items():
        if not HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"header name {name!r} is not valid — it must be an HTTP token.")
        canonical = name.lower().replace("_", "-")
        if (
            canonical in PROTECTED_HEADERS
            or canonical.startswith("x-forwarded-")
            or canonical == managed
        ):
            ignored.append(name)
        elif auth_module.HEADER_VALUE_RE.fullmatch(value):
            merged[name] = value
        else:
            raise ValueError(
                f"header {name!r} has an invalid value — printable ASCII, no line breaks "
                "and no whitespace at either end."
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
    Traversal is rejected by looking for dot segments under up to three decodings (a
    proxy chain may decode more than once), then checking the normalized URL still sits
    under the base prefix.
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
    elif text and "json" in (content_type or ""):  # an empty body (HEAD, 204) is not bad JSON
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
        try:
            result["location"] = _redact(str(resp.url.join(location)))
        except httpx.InvalidURL:  # httpx only vets Location on 301/302/303/307/308
            result["location"] = "<invalid URL>"
    return result


def _redact(url: str) -> str:
    """Blank out query values whose name suggests a credential, so the echoed URL
    never carries a key into the transcript."""
    parsed = httpx.URL(url)
    if not parsed.query:
        return url
    safe = [
        (k, "***" if any(h in k.lower() for h in SENSITIVE_PARAM_HINTS) else v)
        for k, v in parsed.params.multi_items()
    ]
    return str(parsed.copy_with(params=safe))


def main() -> None:
    # The SDK turns on INFO logging, and httpx then writes "HTTP Request: GET <url>" to
    # stderr with the full query string, bypassing _redact.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()
