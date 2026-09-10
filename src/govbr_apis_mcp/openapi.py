"""OpenAPI spec fetching, caching and compact summarization.

Specs are fetched on demand from the catalog entry's openapi_url and cached on
disk (~/.cache/govbr-apis-mcp, 24h TTL) so describe_api stays cheap. Cache file
names are hashes of the api id — never the id itself, which would let a catalog
entry read or overwrite arbitrary .json files on disk.

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

from .catalog import host_allowed

CACHE_TTL_SECONDS = 24 * 3600
HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    path = Path(base) / "govbr-apis-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_path(api_id: str) -> Path:
    return _cache_dir() / f"{hashlib.sha256(api_id.encode()).hexdigest()}.json"


def fetch_spec(api_id: str, openapi_url: str, client: httpx.Client) -> dict[str, Any]:
    cache_file = cache_path(api_id)
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < CACHE_TTL_SECONDS:
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache_file.unlink(missing_ok=True)  # corrupted cache repairs itself

    resp = client.get(openapi_url, follow_redirects=True)
    resp.raise_for_status()
    if not host_allowed(str(resp.url)):
        raise ValueError(f"Spec fetch for {api_id} redirected off-allowlist to {resp.url}")
    spec = resp.json()
    _write_atomic(cache_file, json.dumps(spec))
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
                    "summary": op.get("summary") or op.get("description", "")[:120] or None,
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
        params = [*shared_params, *op.get("parameters", [])]
        out["operations"][m.upper()] = {
            "summary": op.get("summary") or op.get("description"),
            "parameters": [
                {
                    "name": p.get("name"),
                    "in": p.get("in"),
                    "required": p.get("required", False),
                    "schema": _resolve(spec, p.get("schema"), depth=1),
                    "description": (p.get("description") or "")[:200] or None,
                }
                for p in params
            ],
            "requestBody": _resolve(spec, _body_schema(op), depth=3),
            "response": _resolve(spec, _response_schema(op), depth=3),
        }
    return out


def _body_schema(op: dict[str, Any]) -> dict[str, Any] | None:
    content = (op.get("requestBody") or {}).get("content") or {}
    for media in ("application/json", *content):
        if media in content:
            return content[media].get("schema")
    return None


def _response_schema(op: dict[str, Any]) -> dict[str, Any] | None:
    responses = op.get("responses") or {}
    for code in ("200", "201", "default", *responses):
        if code in responses:
            content = (responses[code] or {}).get("content") or {}
            for media in ("application/json", *content):
                if media in content:
                    return content[media].get("schema")
    return None


def _resolve(spec: dict[str, Any], schema: Any, depth: int) -> Any:
    """Inline $refs to a limited depth so schemas are readable without the full spec."""
    if schema is None:
        return None
    if depth < 0:
        return {"truncated": True} if isinstance(schema, (dict, list)) else schema
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str):
            if not ref.startswith("#/"):
                return {"unresolved_ref": ref}
            target: Any = spec
            for part in ref[2:].split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                if not isinstance(target, dict) or key not in target:
                    return {"unresolved_ref": ref}
                target = target[key]
            return _resolve(spec, target, depth - 1)
        return {
            k: _resolve(spec, v, depth - 1)
            for k, v in schema.items()
            if k not in ("xml", "example", "examples")
        }
    if isinstance(schema, list):
        return [_resolve(spec, item, depth - 1) for item in schema]
    return schema
