# Changelog

All notable changes to this project are documented in this file. Versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-10-19

A hardening release. The gateway, the spec fetch, the catalog loader and the meia-entrada
checks now fail closed.

### Packaging and CI

- The catalog ships inside the package, in `src/govbr_apis_mcp/catalog_data/`. Wheels,
  `uvx` and the Docker image used to fail with `FileNotFoundError`; now they find it.
- Package data also covers `*.yml` catalog files.
- New CI runs ruff, the tests on Python 3.11 to 3.14, and a wheel smoke test. The smoke
  test installs only the wheel and loads the catalog from outside the checkout.
- Publishing is gated on lint, format, tests and a wheel smoke test. It runs only for a
  release or from `main`.
- `twine check --strict` runs in CI and in the publish workflow.
- The Docker image copies only what the build needs and runs as an unprivileged user.
- License metadata follows PEP 639 (`License-Expression: MIT`).
- `server.json` declares all ten credential variables. `mcp` is bounded to `<3`.
- `tzdata` is a dependency, because `America/Sao_Paulo` needs a time zone database.

### Gateway

- `call_api` accepts GET, HEAD, POST, PUT, PATCH, DELETE and OPTIONS. Any other method
  returns an error with `allowed_methods`.
- Caller headers are compared case-insensitively, with `_` equal to `-`. `Authorization`,
  `Proxy-Authorization`, `Host`, `Cookie` and `Accept-Encoding`, plus connection, proxy,
  forwarding and method-override headers, are dropped and listed in `ignored_headers`.
  The full list is in `server.py`.
- The header of the managed credential is blocked for callers too, and the credential is
  applied last, so it always wins.
- `Accept-Encoding: identity` is fixed, so the 60,000-character cut is made on the
  streamed body.
- The method, body, headers and path are validated before any OAuth2 token is requested.
  Invalid input comes back as `{"error": ...}`.
- `path` is relative to the `base_url`. Dot segments are rejected after up to three rounds
  of decoding, with `\` read as `/` and `;` parameters removed. A path that leaves the
  `base_url` prefix is rejected.
- When a query string in `path` is merged with `query`, a list value repeats the key and a
  key from the caller replaces the embedded one.
- A `3xx` response, which is still not followed, now carries a redacted `location`,
  resolved against the requested URL.
- Credentials read from the environment are stripped and must be printable ASCII. An
  invalid value fails with `credential in <VAR> contains invalid characters`, and the value
  is never echoed.
- A failing token endpoint returns the error and the `token_url`, with no response body
  and no secret.
- `credentials_status` reports `callable`. Serpro entries are callable once their
  `SERPRO_*` variables are set.
- `serverInfo.version` and the `User-Agent` come from `__version__`.
- The `httpx` and `httpcore` loggers stay at WARNING, so httpx logs no request URL (query
  string included).

### Spec fetch and catalog

- `describe_api` follows redirects by hand, for at most 5 hops. Each URL, the first and
  every `Location`, must be `https` on an allowlisted host before it is requested.
- Spec bodies are capped at 5 MiB. A spec can be JSON or YAML and must carry `openapi` or
  `swagger`. YAML with aliases (including merge keys that rely on them) is refused.
- The spec cache file name derives from `sha256(api_id + "\n" + openapi_url)`, so changing
  a URL never serves an old spec.
- `describe_api` reads Swagger 2.0 (`type` parameters, response `schema`, `in: body`) and
  `$ref` parameters.
- `GOVBR_EXTRA_CATALOG_DIR` accepts `.yaml` and `.yml`. A directory that is missing or
  holds no entries is an error, and so is an id repeated inside it. An empty value counts
  as unset.
- Catalog URLs must be `https`, and hostnames may only contain `[a-z0-9.-]`. The same rule
  applies to `GOVBR_EXTRA_ALLOWED_HOSTS`. Internationalized names go in punycode.
- `list_apis` lists callable entries first.
- `pii: true` also flags the three Serpro entries.

### Meia-entrada

- One reader parses every date. It accepts `YYYY-MM-DD` with an optional valid time,
  `YYYYMMDD` and `DD/MM/YYYY`, and only days that exist in the calendar.
- Expiry dates are compared as dates, never as text. A `YYYYMMDD` expiry used to sort after
  any ISO date.
- A missing or unreadable validity refuses the card instead of being skipped.
- "Today" is the civil date in America/Sao_Paulo. UTC is already tomorrow from 21:00 in
  Brasília.
- FESN must answer with the use code that was asked for. The use code is validated before
  it reaches the URL. A 404 means "not found" only when it carries the expected error object.
- A response with no usable verdict is "not validated" (`validated=false`), not "not
  eligible". That covers network and gateway errors, non-2xx statuses, truncated or
  unreadable bodies, DNE `codigoRetorno` 5 and an answer without a verdict.
- When both bases fail, the combined decision names no provider and keeps both reasons.
- Input is validated before any network call, in `auto` mode too.
- Negative decisions no longer return personal data in `raw`.
- The POC honors `--data-evento` for FESN and asks for `--nascimento` and `--cnpj` only
  for DNE. Exit code 3 means "not validated". `--demo --json` prints one valid JSON document.

### Docs and data

- Documented that only the entries that declare an `openapi_url` (2 of the 116) load a
  spec, plus the gateway rules, the spec fetch limits, the catalog layout, the credential
  rules and the `uvx` and Docker installs.
- The package, registry and module descriptions call the gateway catalog-driven and say it
  loads OpenAPI specs on demand, instead of calling it spec-driven.
- `.env.example` says the server does not read `.env` files.
- `docs/meia-entrada.md` and `poc/README.md` document the eligibility rules, the exit codes
  and FESN as a source of personal data.
- Fixed the `docs_url` of the INSS PcD certificate and of INMET, which returned 404, and of
  BCB SGS, which pointed at the page of a single series.
- The FESN entry describes the full verdict rules. The ID Jovem note no longer calls it
  the only Conecta API open to private institutions.
- The Conecta scraper writes its output atomically. It never replaces the catalog with an
  empty result, an invalid id or fewer than half the current entries (`--force` skips only
  the last check). It also unescapes HTML entities and warns about a card with no agency
  (órgão) block.

## [0.1.0] - 2026-09-10

First release.

- A gateway with five tools: `search_catalog`, `list_apis`, `describe_api`, `call_api` and
  `credentials_status`.
- A YAML catalog of 116 APIs in four tiers: public, Serpro, meia-entrada and Conecta gov.br
  (95 stubs).
- Auth profiles: none, static API key and OAuth2 client credentials.
- Catalog entries are validated on load: filename-safe ids and `https` URLs on allowlisted
  hosts.
- OpenAPI specs are fetched on demand and cached for 24 hours.
- Meia-entrada: validators for the DNE and FESN student cards, with a runnable POC.
- An MCP registry `server.json`, a PyPI trusted-publishing workflow and a Dockerfile.
