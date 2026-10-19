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
import re
import threading
import time

import httpx

from .catalog import ApiEntry

# A header value we are willing to send: printable ASCII (so no CR, LF or NUL) with no
# whitespace at either end, which h11 refuses.
HEADER_VALUE_RE = re.compile(r"(?:[\x21-\x7e](?:[\t\x20-\x7e]*[\x21-\x7e])?)?")


class MissingCredentialsError(Exception):
    """Credentials are absent or unusable. Messages never carry the credential itself."""


class TokenError(Exception):
    """The OAuth2 token endpoint failed. Messages never carry the response body."""


_token_cache: dict[str, tuple[str, float]] = {}


class _Flight:
    """A token request under way. Callers of the same API that arrive meanwhile wait for it
    and share its outcome, a failure included, instead of each asking the endpoint again."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.token = ""
        self.error: BaseException | None = None

    def outcome(self) -> str:
        self.done.wait()
        if self.error is not None:
            raise self.error
        return self.token


# The SDK runs sync tools in a thread pool: one flight per API at a time. The lock only guards
# the dict, never a request.
_flights: dict[str, _Flight] = {}
_flights_lock = threading.Lock()


def _secret(var: str | None) -> str:
    """The value of a credential env var minus surrounding whitespace: one read from a
    file usually ends in a newline, which would end up in the header."""
    return os.environ.get(var or "", "").strip()


def _invalid(var: str | None) -> str:
    return f"credential in {var} contains invalid characters"


def _api_key_value(entry: ApiEntry) -> str:
    """What an api_key entry sends: its prefix plus the key from the environment."""
    prefix = entry.auth_config.get("value_prefix", "")
    return f"{prefix}{_secret(entry.auth_config.get('env'))}"


def _encodable(value: str) -> bool:
    try:
        value.encode()
    except UnicodeEncodeError:  # a non-UTF-8 byte in an env var
        return False
    return True


def credentials_status(entry: ApiEntry) -> dict[str, object]:
    """Report which env vars the entry needs and whether they are set. `ready` also means
    usable: a value call_api would refuse makes it False, with the `reason`."""
    required: dict[str, bool] = {}
    reason = None
    if entry.auth == "api_key":
        var = entry.auth_config.get("env", "")
        required[var] = bool(_secret(var))
        if required[var] and not HEADER_VALUE_RE.fullmatch(_api_key_value(entry)):
            reason = _invalid(var)
    elif entry.auth == "oauth2_client_credentials":
        for key in ("client_id_env", "client_secret_env"):
            var = entry.auth_config.get(key, "")
            if var:
                required[var] = bool(_secret(var))
                if required[var] and not _encodable(_secret(var)):
                    reason = reason or _invalid(var)
    ready = entry.auth == "none" or (bool(required) and all(required.values()) and not reason)
    status: dict[str, object] = {"auth": entry.auth, "env_vars": required, "ready": ready}
    if reason:
        status["reason"] = reason
    return status


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
    if not _secret(var):
        signup = entry.auth_config.get("signup_url")
        hint = f" Get one at {signup}." if signup else ""
        raise MissingCredentialsError(f"{entry.id} requires env var {var}.{hint}")
    value = _api_key_value(entry)
    if not HEADER_VALUE_RE.fullmatch(value):  # h11 would quote the value in its error
        raise MissingCredentialsError(_invalid(var))
    return {entry.auth_config["header"]: value}


def _cached_token(entry_id: str) -> str | None:
    cached = _token_cache.get(entry_id)
    return cached[0] if cached and cached[1] > time.monotonic() else None


def _fetch_token(entry: ApiEntry, client: httpx.Client) -> str:
    token, ttl = _request_token(entry, client)
    if ttl > 60:  # short-lived tokens are not cached — serving an expired one is worse
        _token_cache[entry.id] = (token, time.monotonic() + ttl - 30)
    return token


def _oauth2_token(entry: ApiEntry, client: httpx.Client) -> str:
    token = _cached_token(entry.id)  # before anyone queues: a cached token never waits
    if token:
        return token
    with _flights_lock:
        flight = _flights.get(entry.id)
        leader = flight is None
        if leader:
            flight = _flights[entry.id] = _Flight()
    if not leader:
        return flight.outcome()  # a request is already under way: take its outcome
    try:
        # A flight that landed just before ours may have cached a token already.
        flight.token = _cached_token(entry.id) or _fetch_token(entry, client)
    except BaseException as exc:
        flight.error = exc  # the followers get the same failure, not a retry each
        raise
    finally:
        with _flights_lock:
            _flights.pop(entry.id, None)
        flight.done.set()
    return flight.token


def _request_token(entry: ApiEntry, client: httpx.Client) -> tuple[str, int]:
    """Fetch a token; returns it with its lifetime in seconds: 300 if the endpoint gives none,
    0 if what it gives is unusable (the token is then used once and not cached)."""
    cfg = entry.auth_config
    id_var, secret_var = cfg.get("client_id_env"), cfg.get("client_secret_env")
    if not id_var or not secret_var or not cfg.get("token_url"):
        raise MissingCredentialsError(
            f"{entry.id} has no OAuth2 configuration yet (status: {entry.status}). "
            "Fill auth_config.token_url/client_id_env/client_secret_env — see the "
            "catalog override mechanism in the README."
        )
    client_id = _secret(id_var)
    client_secret = _secret(secret_var)
    if not client_id or not client_secret:
        raise MissingCredentialsError(
            f"{entry.id} requires env vars {id_var} and {secret_var} (status: {entry.status})."
        )

    try:
        httpx.URL(cfg["token_url"])  # a bad token_url is the operator's doing, not the endpoint's
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    except httpx.InvalidURL:
        raise TokenError(f"{entry.id}: token_url is not a valid URL") from None
    except UnicodeEncodeError:  # a non-UTF-8 byte in a credential env var
        raise TokenError(
            f"{entry.id}: client credentials contain characters that cannot be encoded"
        ) from None
    try:
        resp = client.post(
            cfg["token_url"],
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}"},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        raise TokenError(f"{entry.id}: token endpoint returned HTTP {status}") from None
    except httpx.HTTPError as exc:
        raise TokenError(f"{entry.id}: token request failed ({type(exc).__name__})") from None
    except (httpx.InvalidURL, ValueError):
        # httpx builds the request a 3xx points to even though it does not follow it
        raise TokenError(
            f"{entry.id}: token endpoint returned a redirect with a malformed Location"
        ) from None
    try:
        payload = resp.json()
        token = payload["access_token"]
        if not isinstance(token, str) or not token.strip():
            raise KeyError("access_token")
    except (ValueError, KeyError, TypeError, RecursionError):  # RecursionError: nested JSON
        raise TokenError(f"{entry.id}: token endpoint returned no access_token") from None
    token = token.strip()
    if not HEADER_VALUE_RE.fullmatch(token):
        raise TokenError(
            f"{entry.id}: token endpoint returned an access_token with invalid characters"
        )
    try:
        ttl = int(float(payload.get("expires_in", 300)))
    except (TypeError, ValueError, OverflowError):
        ttl = 0  # lifetime unknown: the token is used this once and not cached
    return token, ttl
