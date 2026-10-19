"""OpenAPI spec fetching, caching and compact summarization.

Specs are fetched on demand from the catalog entry's openapi_url and cached on
disk (~/.cache/govbr-apis-mcp, 24h TTL) so describe_api stays cheap. Cache file
names are hashes of the api id and spec URL — never the id itself, which would let
a catalog entry read or overwrite arbitrary .json files on disk — so editing an
entry's openapi_url takes effect at once instead of after the TTL.

Redirects are followed by hand: every hop, the first URL included, must be https on
an allowlisted host before it is requested, and bodies are capped at MAX_SPEC_BYTES.

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
HTTP_METHODS = ("get", "post", "put", "patch", "delete")


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
        except (json.JSONDecodeError, OSError):
            cache_file.unlink(missing_ok=True)  # corrupted cache repairs itself

    spec = _parse_spec(_download(api_id, openapi_url, client))
    text = json.dumps(spec, default=str)  # YAML can carry dates, which JSON cannot
    _write_atomic(cache_file, text)
    return json.loads(text)  # what a later cache hit returns: dates and int keys as strings


def _download(api_id: str, url: str, client: httpx.Client) -> bytes:
    """GET a spec, following redirects by hand so that every hop is checked before it is
    requested, and reading at most MAX_SPEC_BYTES."""
    current = httpx.URL(url)
    for _ in range(MAX_REDIRECTS + 1):
        if current.scheme != "https" or not host_allowed(str(current)):
            raise ValueError(
                f"Spec fetch for {api_id} refused {current}: spec and redirect URLs "
                "must be https on an allowlisted host"
            )
        with client.stream("GET", current, follow_redirects=False) as resp:
            if resp.next_request is None:
                resp.raise_for_status()
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > MAX_SPEC_BYTES:
                        raise ValueError(
                            f"Spec fetch for {api_id}: spec too large (over {MAX_SPEC_BYTES} bytes)"
                        )
                return bytes(body)
            current = resp.next_request.url
    raise ValueError(f"Spec fetch for {api_id} followed more than {MAX_REDIRECTS} redirects")


def _parse_spec(body: bytes) -> dict[str, Any]:
    """Specs come as JSON or YAML, and servers rarely label them right: sniff the body."""
    try:
        spec = json.loads(body)
    except ValueError:  # JSONDecodeError and UnicodeDecodeError
        try:
            spec = yaml.safe_load(body)
        except yaml.YAMLError:
            spec = None
    if not isinstance(spec, dict) or not ({"openapi", "swagger"} & spec.keys()):
        raise ValueError("not an OpenAPI document")
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
        for method, op in methods.items():
            if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                continue
            operations.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "summary": op.get("summary") or (op.get("description") or "")[:120] or None,
                }
            )
    info = spec.get("info", {})
    return {
        "title": info.get("title"),
        "version": info.get("version"),
        "servers": [s.get("url") for s in spec.get("servers", []) if isinstance(s, dict)],
        "operations": operations,
    }


def describe_operation(spec: dict[str, Any], path: str, method: str | None) -> dict[str, Any]:
    """Detail of one path (or one operation): parameters, request body, response schema."""
    methods = (spec.get("paths") or {}).get(path)
    if methods is None:
        available = list((spec.get("paths") or {}).keys())
        raise KeyError(
            f"Path {path!r} not in spec — use the templated path exactly as listed "
            f"(e.g. '/viagens/{{id}}', not '/viagens/123'). Available: {available[:40]}"
        )

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
        merged[(param.get("name"), param.get("in"))] = param
    return list(merged.values())


def _describe_param(spec: dict[str, Any], param: dict[str, Any]) -> dict[str, Any]:
    schema = param.get("schema")
    if schema is None:  # Swagger 2.0 declares the type on the parameter itself
        schema = {k: param[k] for k in ("type", "format", "enum", "items") if k in param} or None
    return {
        "name": param.get("name"),
        "in": param.get("in"),
        "required": param.get("required", False),
        "schema": _resolve(spec, schema, depth=1),
        "description": (param.get("description") or "")[:200] or None,
    }


def _body_schema(op: dict[str, Any], body_param: dict[str, Any] | None) -> dict[str, Any] | None:
    content = (op.get("requestBody") or {}).get("content") or {}
    for media in ("application/json", *content):
        if media in content:
            return content[media].get("schema")
    return (body_param or {}).get("schema")  # Swagger 2.0: the body is a parameter


def _response_schema(op: dict[str, Any]) -> dict[str, Any] | None:
    responses = op.get("responses") or {}
    for code in ("200", "201", "default", *responses):
        if code in responses:
            response = responses[code] or {}
            content = response.get("content") or {}
            for media in ("application/json", *content):
                if media in content:
                    return content[media].get("schema")
            if "schema" in response:  # Swagger 2.0 puts the schema on the response itself
                return response["schema"]
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
    anything below it. A pointer that does not resolve leaves the node as it was."""
    ref = node.get("$ref") if isinstance(node, dict) else None
    if isinstance(ref, str):
        try:
            return _lookup_ref(spec, ref)
        except KeyError:
            pass
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
