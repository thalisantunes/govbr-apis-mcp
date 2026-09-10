# govbr-apis-mcp

MCP server that turns Brazilian government APIs into Claude-callable tools through a
**spec-driven gateway**: instead of one tool per endpoint (hundreds — would blow up
client context), five generic tools operate over a local YAML catalog and load each
API's OpenAPI spec on demand.

## Catalog tiers

| Tier | File | Access model | Callable today? |
|------|------|--------------|-----------------|
| `public` | `catalog/public.yaml` | Free — no auth or free API key (IBGE, Banco Central, Câmara, Senado, Portal da Transparência, DataJud, Ipeadata, dados.gov.br…) | Yes |
| `serpro` | `catalog/serpro.yaml` | Serpro commercial (loja.serpro.gov.br) — OAuth2 client credentials per contracted API, paid per call | With a Serpro contract |
| `meia-entrada` | `catalog/meia_entrada.yaml` | Eligibility sources for half-price tickets (student CIE, ID Jovem, PcD, e-MEC) — see [docs/meia-entrada.md](docs/meia-entrada.md) | CIE yes; the rest is manual |
| `conecta` | `catalog/conecta.yaml` | [Conecta gov.br](https://www.gov.br/conecta/catalogo/) — **restricted to public-sector entities**; 95 APIs indexed as stubs for discovery | Not yet (adhesion pending) |

The Conecta tier exists so the catalog reflects the whole landscape now, and becomes
callable later by filling in `base_url` + credentials — the OAuth2 client-credentials
flow is already implemented (Serpro uses the same one).

## Tools

- `search_catalog(query, category?)` — accent-insensitive keyword search; reports total
  matches and whether the list was truncated.
- `list_apis(category?, status?, limit?)` — enumerate entries (filter it: ~95 Conecta stubs
  otherwise dominate the output).
- `describe_api(api_id, path?, method?)` — catalog metadata + OpenAPI operation list;
  with `path`, parameter/request/response schemas ($refs inlined, depth-limited).
- `call_api(api_id, path, method?, query?, body?, headers?)` — invoke an endpoint; base URL
  and auth resolved from the catalog entry. A query string embedded in `path` is merged with
  `query` (httpx would otherwise drop it), `..` is rejected, and caller headers cannot
  override the managed `Authorization`/`Host`.
- `credentials_status(api_id?)` — which env vars each API needs and whether they're set.

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

## Credentials

All secrets come from env vars declared per entry in the catalog (`auth_config`).
Nothing is stored in the repo. See `.env.example` for the full list; each entry's
`signup_url` says where to get a key. `credentials_status` reports readiness.

Auth profiles: `none`, `api_key` (static header, optional value prefix — e.g.
DataJud's `APIKey <key>`), `oauth2_client_credentials` (token endpoint + id/secret,
in-memory token cache; used by Serpro today, by Conecta after adhesion).

## Extending the catalog

Add an entry to a YAML file in `catalog/`. Minimum fields: `id`, `name`, `provider`,
`category`, `status`, `auth`. Add `openapi_url` when the API publishes a spec —
`describe_api` then works with zero code. Regenerate the Conecta tier with
`python scripts/scrape_conecta.py`.

Entries are validated on load: ids must be filename-safe, URLs must be `https` on an
allowlisted host (`*.gov.br`, `*.leg.br`, `*.jus.br` plus a few named hosts). This is a
security boundary, not style — the catalog decides where your credentials are sent. Add a
host with `GOVBR_EXTRA_ALLOWED_HOSTS` only if you trust it with them.

To override entries without forking (e.g. filling in Conecta base URLs after adhesion),
point `GOVBR_EXTRA_CATALOG_DIR` at a directory whose same-id entries win.
`GOVBR_CATALOG_DIR` replaces the base catalog entirely and should be treated as trusted
input.

## Security notes

- Responses are never written to disk; only OpenAPI specs are cached (file names are
  hashes of the api id) and OAuth2 tokens live in memory only.
- `call_api` does not follow redirects — credentialed requests cannot be steered to
  another host by a `3xx`. Spec fetches follow redirects but re-check the allowlist.
- Entries flagged `pii: true` return personal data (CPF, name, photo) into the client
  transcript. Today: `meiaentrada-cie-validador` and the Serpro tier.

## Roadmap

- [ ] Conecta adhesion as a public-sector entity → fill gateway base URLs + credentials
- [ ] Per-API OpenAPI URLs for Conecta entries (linked from each detail page)
- [ ] Serpro trial-mode toggle (call `-trial` base URLs with the published demo token)
- [ ] Rate-limit guard for paid (per-call) Serpro APIs
- [ ] Pleito à SNJ para a API `conecta-id-jovem` (admite instituição privada)
- [ ] Fallback documental para CIE fora da base DNE (DCEs e entidades estaduais)

## License

MIT - see [LICENSE](LICENSE).
