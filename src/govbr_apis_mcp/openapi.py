"""OpenAPI spec fetching, caching and compact summarization.

Specs are fetched on demand from the catalog entry's openapi_url and cached on
disk (~/.cache/govbr-apis-mcp, 24h TTL) so describe_api stays cheap. Cache file
names are hashes of the api id and spec URL — never the id itself, which would let
a catalog entry read or overwrite arbitrary .json files on disk — so editing an
entry's openapi_url takes effect at once instead of after the TTL.

Redirects are followed by hand: every hop, the first URL included, must be https on
an allowlisted host before it is requested. The download is bounded three ways: bodies
are capped at MAX_SPEC_BYTES counted on the wire (compressed responses are refused, as
a few bytes of gzip inflate to gigabytes before any cap could look), and the whole thing
has a deadline, since the client's timeout only covers each socket operation.

Summaries are deliberately compact: a full spec (Portal da Transparência's is
~185 KB) would crowd out the MCP client's context.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from .catalog import host_allowed

CACHE_TTL_SECONDS = 24 * 3600
MAX_SPEC_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
DOWNLOAD_DEADLINE_SECONDS = 60
HTTP_METHODS = ("get", "post", "put", "patch", "delete")
_TOO_DEEP = "spec too deeply nested"


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    path = Path(base) / "govbr-apis-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_path(api_id: str, openapi_url: str) -> Path:
    key = f"{api_id}\n{openapi_url}"
    return _cache_dir() / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


def fetch_spec(api_id: str, openapi_url: str, client: httpx.Client) -> dict[str, Any]:
    cache_file = cache_path(api_id, openapi_url)
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < CACHE_TTL_SECONDS:
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):  # not JSON, not UTF-8, unreadable: fetch it again
            cache_file.unlink(missing_ok=True)

    text, spec = _round_trip(_parse_spec(_download(api_id, openapi_url, client)))
    _write_atomic(cache_file, text)
    return spec


def _round_trip(spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The spec as JSON text, and as the dict a later cache hit reads back from that text.

    YAML can carry what JSON cannot: dates and int keys come out as strings, so the first
    call and every cache hit see the same thing."""
    try:
        text = json.dumps(spec, default=str)
        return text, json.loads(text)
    except TypeError:  # a key such as the YAML date 2024-01-01 cannot become a JSON key
        raise ValueError("unsupported key types in spec") from None
    except RecursionError:
        raise ValueError(_TOO_DEEP) from None


def _download(api_id: str, url: str, client: httpx.Client) -> bytes:
    """GET a spec, following redirects by hand so that every hop is checked before it is
    requested, and reading at most MAX_SPEC_BYTES within DOWNLOAD_DEADLINE_SECONDS."""
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
    try:
        current = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise ValueError(
            f"Spec fetch for {api_id}: invalid spec URL {_clip(url)!r} ({_clip(exc)})"
        ) from None
    for _ in range(MAX_REDIRECTS + 1):
        _check_deadline(api_id, deadline)
        if current.scheme != "https" or not host_allowed(str(current)):
            raise ValueError(
                f"Spec fetch for {api_id} refused {_clip(current)}: spec and redirect URLs "
                "must be https on an allowlisted host"
            )
        try:
            with client.stream(
                "GET", current, headers={"Accept-Encoding": "identity"}, follow_redirects=False
            ) as resp:
                if resp.next_request is None:
                    resp.raise_for_status()
                    return _read_capped(api_id, resp, deadline)
                current = resp.next_request.url
        except (httpx.InvalidURL, httpx.RemoteProtocolError, UnicodeError) as exc:
            # httpx builds the redirect target itself, and fails on a Location it cannot parse
            if isinstance(exc, httpx.RemoteProtocolError) and "location header" not in str(exc):
                raise  # a genuine protocol error (dropped connection...), not a bad redirect
            raise ValueError(
                f"Spec fetch for {api_id}: invalid redirect from {_clip(current)} ({_clip(exc)})"
            ) from None
    raise ValueError(f"Spec fetch for {api_id} followed more than {MAX_REDIRECTS} redirects")


def _clip(value: object, limit: int = 200) -> str:
    """What an error message quotes from the wire: a server can send tens of thousands of
    characters of URL."""
    text = str(value)
    return (
        text if len(text) <= limit else f"{text[:limit]}... ({len(text) - limit} more characters)"
    )


def _check_deadline(api_id: str, deadline: float) -> None:
    if time.monotonic() > deadline:
        raise ValueError(
            f"Spec fetch for {api_id}: spec download exceeded {DOWNLOAD_DEADLINE_SECONDS} s"
        )


def _read_capped(api_id: str, resp: httpx.Response, deadline: float) -> bytes:
    encoding = resp.headers.get("content-encoding")
    if encoding and encoding.strip().lower() != "identity":
        raise ValueError(
            f"Spec fetch for {api_id}: compressed spec responses are not supported "
            f"(Content-Encoding: {encoding})"
        )
    body = bytearray()
    for chunk in resp.iter_raw():  # bytes as they travel: nothing is inflated before counting
        body += chunk
        if len(body) > MAX_SPEC_BYTES:
            raise ValueError(
                f"Spec fetch for {api_id}: spec too large (over {MAX_SPEC_BYTES} bytes)"
            )
        _check_deadline(api_id, deadline)
    return bytes(body)


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses YAML aliases. Nested anchors, each listing the previous one
    ten times, stay tiny on the wire and multiply on every level once the document is
    serialized, so the size cap on the download cannot protect against them."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("YAML aliases are not supported in OpenAPI documents")
        return super().compose_node(parent, index)


def _load_yaml(body: bytes) -> Any:
    try:
        return yaml.load(body, Loader=_NoAliasLoader)
    except yaml.YAMLError:
        return None
    except RecursionError:
        raise ValueError(_TOO_DEEP) from None


def _parse_spec(body: bytes) -> dict[str, Any]:
    """Specs come as JSON or YAML, and servers rarely label them right: sniff the body.

    Known limitation: PyYAML types scalars by YAML 1.1, so in a YAML spec `on`, `off`, `yes`
    and `no` come out as booleans, 1.10 as 1.1 and 010 as 8. JSON specs are not affected."""
    problem = "not an OpenAPI document"
    try:
        spec = json.loads(body)
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        problem += f" (not JSON: {exc})"
        spec = _load_yaml(body)
    except RecursionError:
        raise ValueError(_TOO_DEEP) from None
    if not isinstance(spec, dict) or not ({"openapi", "swagger"} & spec.keys()):
        raise ValueError(problem)
    return spec


def _write_atomic(target: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def summarize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """One line per operation: method, path, summary."""
    operations = []
    for path, methods in (spec.get("paths") or {}).items():
        for method, op in (methods or {}).items():
            if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                continue
            operations.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "summary": op.get("summary") or (op.get("description") or "")[:120] or None,
                }
            )
    info = spec.get("info") or {}
    return {
        "title": info.get("title"),
        "version": info.get("version"),
        "servers": [s.get("url") for s in (spec.get("servers") or []) if isinstance(s, dict)],
        "operations": operations,
    }


def describe_operation(spec: dict[str, Any], path: str, method: str | None) -> dict[str, Any]:
    """Detail of one path (or one operation): parameters, request body, response schema."""
    paths = spec.get("paths") or {}
    if path not in paths:
        available = list(paths)
        raise KeyError(
            f"Path {path!r} not in spec — use the templated path exactly as listed "
            f"(e.g. '/viagens/{{id}}', not '/viagens/123'). Available: {available[:40]}"
        )
    methods = paths[path] or {}  # an empty path item is null in YAML

    # Parameters may be declared once for the whole path item and inherited by each
    # operation (valid OpenAPI); merge them in so they are not lost.
    shared_params = methods.get("parameters", []) if isinstance(methods, dict) else []
    wanted = [method.lower()] if method else [m for m in methods if m.lower() in HTTP_METHODS]

    out: dict[str, Any] = {"path": path, "operations": {}}
    for m in wanted:
        op = methods.get(m)
        if op is None:
            raise KeyError(f"Method {m.upper()} not available for {path!r}")
        params = _merge_params(spec, shared_params, op.get("parameters"))
        body_param = next((p for p in params if p.get("in") == "body"), None)  # Swagger 2.0
        out["operations"][m.upper()] = {
            "summary": op.get("summary") or op.get("description"),
            "parameters": [_describe_param(spec, p) for p in params if p.get("in") != "body"],
            "requestBody": _resolve(spec, _body_schema(op, body_param), depth=3),
            "response": _resolve(spec, _response_schema(op), depth=3),
        }
    return out


def _merge_params(spec: dict[str, Any], inherited: Any, own: Any) -> list[dict[str, Any]]:
    """Path-level plus operation-level parameters, $refs followed. A parameter is
    identified by (name, in): the operation's own declaration replaces the inherited one."""
    merged: dict[tuple[Any, Any], dict[str, Any]] = {}
    for raw in (*(inherited or []), *(own or [])):
        param = _deref(spec, raw)
        key = (param.get("name"), param.get("in"))
        if key == (None, None):  # a pointer we could not follow: tell them apart by its text
            key = (param.get("$ref"), None)
        merged[key] = param
    return list(merged.values())


def _describe_param(spec: dict[str, Any], param: dict[str, Any]) -> dict[str, Any]:
    schema = param.get("schema")
    if schema is None:  # Swagger 2.0 declares the type on the parameter itself
        schema = {k: param[k] for k in ("type", "format", "enum", "items") if k in param} or None
    described = {
        "name": param.get("name"),
        "in": param.get("in"),
        "required": param.get("required", False),
        "schema": _resolve(spec, schema, depth=1),
        "description": (param.get("description") or "")[:200] or None,
    }
    if isinstance(param.get("$ref"), str):  # a pointer that could not be followed: say which
        described["$ref"] = param["$ref"]
    return described


def _content_schema(content: Any) -> Any:
    """The schema of an OpenAPI 3 `content` map: application/json when it is there, else the
    first media type. A media type can be empty (null in YAML)."""
    content = content or {}
    for media in ("application/json", *content):
        if media in content:
            return (content[media] or {}).get("schema")
    return None


def _body_schema(op: dict[str, Any], body_param: dict[str, Any] | None) -> dict[str, Any] | None:
    schema = _content_schema((op.get("requestBody") or {}).get("content"))
    if schema is None:  # Swagger 2.0: the body is a parameter
        schema = (body_param or {}).get("schema")
    return schema


def _response_schema(op: dict[str, Any]) -> dict[str, Any] | None:
    """The schema of the first 2xx response that has one, 200 and 201 ahead of the rest.

    An error or `default` response does not describe what a call returns, so without a 2xx
    schema there is none: a DELETE that answers 204 must not show its error body instead."""
    responses = op.get("responses") or {}
    success = [code for code in responses if str(code).startswith("2")]
    for code in sorted(success, key=lambda code: {"200": 0, "201": 1}.get(str(code), 2)):
        response = responses[code] or {}
        schema = _content_schema(response.get("content"))
        if schema is None:
            schema = response.get("schema")  # Swagger 2.0 puts the schema on the response
        if schema is not None:
            return schema
    return None


def _lookup_ref(spec: dict[str, Any], ref: str) -> Any:
    """Follow a local '#/a/b' JSON pointer; KeyError when it leads nowhere."""
    if not ref.startswith("#/"):
        raise KeyError(ref)
    target: Any = spec
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or key not in target:
            raise KeyError(ref)
        target = target[key]
    return target


def _deref(spec: dict[str, Any], node: Any) -> Any:
    """Follow a node's own $ref (a parameter may point into components) without inlining
    anything below it. A pointer that does not lead to an object leaves the node as it was."""
    ref = node.get("$ref") if isinstance(node, dict) else None
    if isinstance(ref, str):
        try:
            target = _lookup_ref(spec, ref)
        except KeyError:
            return node
        if isinstance(target, dict):
            return target
    return node


def _resolve(spec: dict[str, Any], schema: Any, depth: int, names: bool = False) -> Any:
    """Inline $refs to a limited depth so schemas are readable without the full spec.

    `names` marks a `properties` map, whose keys are property names rather than schema
    keywords: a property called "example" must survive dropping example/examples/xml."""
    if schema is None:
        return None
    if depth < 0:
        return {"truncated": True} if isinstance(schema, (dict, list)) else schema
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str):
            try:
                target = _lookup_ref(spec, ref)
            except KeyError:
                return {"unresolved_ref": ref}
            return _resolve(spec, target, depth - 1)
        return {
            k: _resolve(spec, v, depth - 1, names=k == "properties" and not names)
            for k, v in schema.items()
            if names or k not in ("xml", "example", "examples")
        }
    if isinstance(schema, list):
        return [_resolve(spec, item, depth - 1) for item in schema]
    return schema
