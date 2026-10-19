"""Auth profile resolution.

Profiles supported:
- none: no credentials.
- api_key: static key from an env var, sent in a header (optionally prefixed,
  e.g. DataJud's "APIKey <key>").
- oauth2_client_credentials: token fetched from auth_config.token_url with
  client id/secret from env vars, cached in-memory until near expiry. Used by
  Serpro commercial APIs today and by Conecta gov.br once adhesion exists —
  same flow, different token endpoint.
"""

from __future__ import annotations

import base64
import os
import threading
import time

import httpx

from .catalog import ApiEntry


class MissingCredentialsError(Exception):
    pass


class TokenError(Exception):
    """The OAuth2 token endpoint failed. Messages never carry the response body."""


_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()  # the SDK runs sync tools in a thread pool


def credentials_status(entry: ApiEntry) -> dict[str, object]:
    """Report which env vars the entry needs and whether they are set."""
    required: dict[str, bool] = {}
    if entry.auth == "api_key":
        var = entry.auth_config.get("env", "")
        required[var] = bool(os.environ.get(var))
    elif entry.auth == "oauth2_client_credentials":
        for key in ("client_id_env", "client_secret_env"):
            var = entry.auth_config.get(key, "")
            if var:
                required[var] = bool(os.environ.get(var))
    ready = entry.auth == "none" or (bool(required) and all(required.values()))
    return {"auth": entry.auth, "env_vars": required, "ready": ready}


def build_headers(entry: ApiEntry, client: httpx.Client) -> dict[str, str]:
    if entry.auth == "none":
        return {}
    if entry.auth == "api_key":
        return _api_key_headers(entry)
    if entry.auth == "oauth2_client_credentials":
        return {"Authorization": f"Bearer {_oauth2_token(entry, client)}"}
    raise ValueError(f"Unsupported auth profile {entry.auth!r} for {entry.id}")


def _api_key_headers(entry: ApiEntry) -> dict[str, str]:
    var = entry.auth_config.get("env")
    key = os.environ.get(var or "")
    if not key:
        signup = entry.auth_config.get("signup_url")
        hint = f" Get one at {signup}." if signup else ""
        raise MissingCredentialsError(f"{entry.id} requires env var {var}.{hint}")
    prefix = entry.auth_config.get("value_prefix", "")
    return {entry.auth_config["header"]: f"{prefix}{key}"}


def _oauth2_token(entry: ApiEntry, client: httpx.Client) -> str:
    with _token_lock:
        cached = _token_cache.get(entry.id)
    if cached and cached[1] > time.monotonic():
        return cached[0]

    cfg = entry.auth_config
    id_var, secret_var = cfg.get("client_id_env"), cfg.get("client_secret_env")
    if not id_var or not secret_var or not cfg.get("token_url"):
        raise MissingCredentialsError(
            f"{entry.id} has no OAuth2 configuration yet (status: {entry.status}). "
            "Fill auth_config.token_url/client_id_env/client_secret_env — see the "
            "catalog override mechanism in the README."
        )
    client_id = os.environ.get(id_var)
    client_secret = os.environ.get(secret_var)
    if not client_id or not client_secret:
        raise MissingCredentialsError(
            f"{entry.id} requires env vars {id_var} and {secret_var} (status: {entry.status})."
        )

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        resp = client.post(
            cfg["token_url"],
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}"},
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload["access_token"]
        if not token or not isinstance(token, str):
            raise KeyError("access_token")
        ttl = int(payload.get("expires_in", 300))
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        raise TokenError(f"{entry.id}: token endpoint returned HTTP {status}") from None
    except httpx.HTTPError as exc:
        raise TokenError(f"{entry.id}: token request failed ({type(exc).__name__})") from None
    except (ValueError, KeyError, TypeError):
        raise TokenError(f"{entry.id}: token endpoint returned no access_token") from None
    if ttl > 60:  # short-lived tokens are not cached — serving an expired one is worse
        with _token_lock:
            _token_cache[entry.id] = (token, time.monotonic() + ttl - 30)
    return token
