# govbr-apis-mcp

[![CI](https://github.com/thalisantunes/govbr-apis-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/thalisantunes/govbr-apis-mcp/actions/workflows/ci.yml)

<!-- mcp-name: io.github.thalisantunes/govbr-apis-mcp -->

MCP server that turns Brazilian government APIs into Claude-callable tools through a
**catalog-driven gateway**: instead of one tool per endpoint (hundreds — would blow up
client context), five generic tools operate over a local YAML catalog of 116 APIs.

Only the entries that declare an `openapi_url` have a spec to load: today the Câmara dos
Deputados and the Portal da Transparência, 2 of the 116. For those, `describe_api` loads
the OpenAPI spec on demand. Every other callable entry (see the "Callable today?" column
below) is reached through `call_api` alone, using the endpoints in its description and
`docs_url`.

## Catalog tiers

| Tier | File | Access model | Callable today? |
|------|------|--------------|-----------------|
| `public` | `src/govbr_apis_mcp/catalog_data/public.yaml` | Free — no auth or free API key (IBGE, Banco Central, Câmara, Senado, Portal da Transparência, DataJud, Ipeadata, dados.gov.br…) | Yes |
| `serpro` | `src/govbr_apis_mcp/catalog_data/serpro.yaml` | Serpro commercial (loja.serpro.gov.br) — OAuth2 client credentials per contracted API, paid per call | With a Serpro contract: set the `SERPRO_*` variables |
| `meia-entrada` | `src/govbr_apis_mcp/catalog_data/meia_entrada.yaml` | Eligibility sources for half-price tickets (student CIE, ID Jovem, PcD, e-MEC) — see [docs/meia-entrada.md](docs/meia-entrada.md) and the [POC](poc/README.md) | CIE yes (DNE and FESN); the rest is manual |
| `conecta` | `src/govbr_apis_mcp/catalog_data/conecta.yaml` | [Conecta gov.br](https://www.gov.br/conecta/catalogo/) — **restricted to public-sector entities**; 95 APIs indexed as stubs for discovery | Not yet (adhesion pending) |

The Conecta tier exists so the catalog reflects the whole landscape now, and becomes
callable later by filling in `base_url` + credentials — the OAuth2 client-credentials
flow is already implemented (Serpro uses the same one).

## Tools

- `search_catalog(query, category?)` — accent-insensitive keyword search; reports total
  matches and whether the list was truncated.
- `list_apis(category?, status?, limit?)` — enumerate entries, callable ones first (by the
  catalog's status, not by whether your credentials are set; filter it: ~95 Conecta stubs
  otherwise dominate the output).
- `describe_api(api_id, path?, method?)` — catalog metadata + OpenAPI operation list;
  with `path`, parameter/request/response schemas ($refs inlined, depth-limited). Reads
  OpenAPI 3 and Swagger 2.0, as JSON or YAML.
- `call_api(api_id, path, method?, query?, body?, headers?)` — invoke an endpoint; base URL
  and auth resolved from the catalog entry.
  - `method` is GET, HEAD, POST, PUT, PATCH, DELETE or OPTIONS; anything else returns an
    error with `allowed_methods`. A `body` is accepted only with POST, PUT and PATCH.
  - `path` is relative to the `base_url`. `.` and `..` segments (after up to three rounds of
    percent-decoding, with `\` read as `/` and `;` parameters removed) and any path that
    leaves the `base_url` prefix are rejected. So is a `#` (a fragment is never sent; write a
    literal one as `%23`) or a control character.
  - A query string embedded in `path` is merged with `query`; a key in `query` replaces the
    embedded one, and a list value repeats the key.
  - Caller headers are matched case-insensitively, with `_` equal to `-`. `Authorization`,
    `Proxy-Authorization`, `Host`, `Cookie` and `Accept-Encoding`, plus connection, proxy,
    forwarding and method-override headers, are dropped and reported in `ignored_headers`
    (the full list is in `server.py`). The managed credential is applied last and always wins.
    A header with an invalid name or value is not ignored: it fails the call with an `error`.
  - Method, body, headers and path are validated before any OAuth2 token is requested, and
    invalid input comes back as `{"error": ...}`.
  - A `3xx` is not followed: the response carries its `location` (redacted, resolved
    against the requested URL). Response bodies are cut at 60,000 characters.
- `credentials_status(api_id?)` — which env vars each API needs and whether they're set,
  plus `callable`: whether `call_api` would accept the entry right now (`available` and
  `unstable` entries; `requires_contract` ones only once their env vars are set).

## POC: meia-entrada de estudante

Fluxo funcional ponta a ponta (código da CIE → decisão de elegibilidade), incluindo
modo demo sem credencial: [`poc/README.md`](poc/README.md).

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Register in Claude Code (`.mcp.json` or `claude mcp add`):

```json
{
  "mcpServers": {
    "govbr-apis": {
      "command": "/path/to/govbr-apis-mcp/.venv/bin/govbr-apis-mcp",
      "env": {
        "TRANSPARENCIA_API_KEY": "...",
        "DATAJUD_API_KEY": "..."
      }
    }
  }
}
```

From PyPI: `uvx govbr-apis-mcp` (set `"command": "uvx"` and `"args": ["govbr-apis-mcp"]`
in the entry above; no checkout needed). Each release is published to PyPI and to the MCP
Registry automatically (`publish.yml`).

Or run it in a container. The server speaks stdio, so keep `-i`:

```bash
docker build -t govbr-apis-mcp . && docker run -i --rm -e TRANSPARENCIA_API_KEY=... govbr-apis-mcp
```

## Credentials

All secrets come from env vars declared per entry in the catalog (`auth_config`).
Nothing is stored in the repo. `.env.example` lists the variables. Entries with a free key
carry a `signup_url`; Serpro credentials come with the contract, at loja.serpro.gov.br.
`credentials_status` reports readiness.

The server does not read `.env` files, and MCP clients start it with a minimal environment,
so an `export` in your shell may never reach it. Put the variables in the client's `env`
block (the example above, or `claude mcp add -e KEY=value`). For a container, copy
`.env.example` to a git-ignored file such as `.env.local`, fill in the copy and pass it
with `docker run --env-file .env.local`. Never put real values in `.env.example`: it is
versioned.

API keys (the value together with any prefix) are stripped of surrounding whitespace and
must be printable ASCII; otherwise the call fails with `credential in <VAR> contains
invalid characters`, without echoing the value. OAuth2 client ids and secrets are only
stripped: they travel base64-encoded, so any text works. The access token the endpoint
returns must be printable ASCII too. When the token endpoint fails, the call returns an
`error` and the `token_url`, with neither the response body nor any secret.

Auth profiles: `none`, `api_key` (static header, optional value prefix — e.g.
DataJud's `APIKey <key>`), `oauth2_client_credentials` (token endpoint + id/secret,
in-memory token cache; used by Serpro today, by Conecta after adhesion).

## Extending the catalog

Add an entry to a YAML file in `src/govbr_apis_mcp/catalog_data/`. The directory is package
data, so the wheel, `uvx` and the Docker image all carry the catalog. Minimum fields:
`id`, `name`, `provider`, `category`, `status`, `auth`. Add `openapi_url` when the API
publishes a spec — `describe_api` then works with zero code. Regenerate the Conecta
tier with `.venv/bin/python scripts/scrape_conecta.py`. It leaves the file alone when the
page yields no APIs, an id the catalog would reject, or fewer than half the current
entries (`--force` overrides only the last check).

Entries are validated on load: ids must be filename-safe, URLs must be `https` on an
allowlisted host (`*.gov.br`, `*.leg.br`, `*.jus.br` plus a few named hosts), and a hostname
may only contain `[a-z0-9.-]` (write internationalized names in punycode). This is a
security boundary, not style — the catalog decides where your credentials are sent. Add a
host with `GOVBR_EXTRA_ALLOWED_HOSTS` only if you trust it with them; the same hostname
rule applies to it.

To override entries without forking (e.g. filling in Conecta base URLs after adhesion),
point `GOVBR_EXTRA_CATALOG_DIR` at a directory of `.yaml` or `.yml` files whose same-id
entries win. A directory that does not exist or holds no entries is an error, and so is an
id repeated inside it; an empty value counts as unset. `GOVBR_CATALOG_DIR` replaces the
base catalog entirely and should be treated as trusted input.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

CI runs ruff on Python 3.12 and the test suite on 3.11–3.14, then builds the wheel and
loads the catalog from a clean install.

## Security notes

- Responses are never written to disk. Only OpenAPI specs are cached, in
  `$XDG_CACHE_HOME/govbr-apis-mcp/` (or `~/.cache/govbr-apis-mcp/`) for 24 hours, under a
  file name derived from `sha256(api_id + "\n" + openapi_url)`: never the id itself, and a
  changed `openapi_url` never serves the old spec. OAuth2 tokens live in memory only.
- `call_api` does not follow redirects — credentialed requests cannot be steered to
  another host by a `3xx`; the response carries the (redacted) `location` instead. It always
  sends `Accept-Encoding: identity`, so a server has no reason to compress. The
  60,000-character cut counts decoded text, though: a server that compresses anyway can
  still inflate a small chunk before the cut (an accepted risk; see the roadmap).
- Cookies an API sets are never stored or sent back, so one API cannot plant a cookie that
  another would receive.
- Spec fetches follow redirects only by hand: at most 5 hops, and every URL (the first
  and each `Location`) must be `https` on an allowlisted host before it is requested.
  The body is capped at 5 MiB, JSON and YAML are accepted (YAML with aliases, including
  merge keys that rely on them, is refused), and the document must carry `openapi` or
  `swagger`. A compressed response is refused and the whole download has a 60-second
  deadline; both come back as `spec_error`.
- The `httpx` and `httpcore` loggers stay at WARNING, so httpx logs no request URL (query
  string included).
- Entries flagged `pii: true` return personal data (CPF, name, photo) into the client
  transcript. Today: `meiaentrada-cie-validador`, `fesn-cie-validador` (callable without
  any credential: whoever holds a use code gets the holder's data) and the three Serpro
  entries.

## Roadmap

- [ ] Conecta adhesion as a public-sector entity → fill gateway base URLs + credentials
- [ ] Per-API OpenAPI URLs for Conecta entries (linked from each detail page)
- [ ] Serpro trial-mode toggle (call `-trial` base URLs with the published demo token)
- [ ] Per-call cost guard for paid Serpro APIs
- [ ] Bounded decompression for compressed upstream responses
- [ ] Pleito à SNJ para a API `conecta-id-jovem` (admite instituição privada)
- [ ] Fallback documental para CIE fora da base DNE (DCEs e entidades estaduais)

## License

MIT - see [LICENSE](LICENSE).
