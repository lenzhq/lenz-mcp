# Contributing to lenz-mcp

This repository is the Lenz MCP server: the connector Claude, ChatGPT and other
MCP clients use to check the factual claims in a text with Lenz. It is a thin
layer over the public Lenz API. Every tool call becomes one or more calls to
`https://lenz.io/api/v1`, authenticated with the caller's API key or, for OAuth
clients, on the signed-in user's behalf.

## Layout

| Path | What it is |
|---|---|
| `src/lenz_mcp/` | The server: tools, prompts, the per-client tool list, OAuth, the wire protocol |
| `src/lenz_mcp/card/` | The result card (MCP Apps), a Preact bundle built to `dist/` |
| `tests/` | The server's tests, run with no network and no Lenz account |
| `evals/tool_choice/` | Which tool a model picks for a request, and when it picks none |
| `scripts/smoke.py` | The end-to-end check a deployment runs against a live server |
| `scripts/probe/` | A local server for measuring what an MCP host actually does |

## Running the tests

```bash
uv sync --group dev
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

The tests drive the real ASGI app against a stubbed Lenz API. The per-client
goldens in `tests/goldens/` record exactly what each model sees: the tool list,
descriptions and server instructions. A wording change shows up as a diff there.
Regenerate them deliberately with `UPDATE_MCP_GOLDENS=1 uv run pytest`, and
review the diff as the change it is.

## The card

```bash
cd src/lenz_mcp/card
npm ci
npm test            # unit tests
npm run build       # rebuilds dist/ for the current card version
npm run test:host   # the built card inside a stub MCP Apps host (needs Chromium)
```

A published card version never changes. Hosts cache a card by its URI
(`ui://lenz/card-vN`) and old conversations re-mount whatever it names, so
every version in `dist/versions.json` keeps being served. To change the card,
bump `cardVersion` in `package.json` and `CARD_URI` in `src/lenz_mcp/mcp_card.py`,
then build. `npm run check:published` fails on an edit to a published bundle.

## The tool-choice eval

The eval makes paid calls to model vendors, so it runs by hand, not in CI:

```bash
uv sync --group eval
ANTHROPIC_API_KEY=... OPENAI_API_KEY=... uv run python -m evals.tool_choice
```

Pass `--prices FILE` (a JSON map of model-name prefix to per-million-token input
and output prices) to have the spend summary priced. A structural test checks the
cases without calling any model.

## Running your own server

```bash
docker build --build-arg LENZ_MCP_VERSION=dev -t lenz-mcp .
docker run -p 8080:8080 -e FRONTEND_URL=https://lenz.io lenz-mcp
```

The server listens on `/mcp`. Callers authenticate with a Lenz API key in the
`Authorization` header; OAuth is off unless `MCP_OAUTH_ENABLED=True` and an
AuthKit domain are set. `bash scripts/container_test.sh` builds the image and
runs the smoke against it.

With OAuth on, each tool call needs a Lenz API credential for the signed-in
user. `LENZ_OAUTH_EXCHANGE=True` obtains it with an OAuth 2.0 token exchange
(RFC 8693) at the Lenz API's token endpoint, authenticating as the server's own
OAuth client:

| Variable | What it is |
|---|---|
| `LENZ_OAUTH_EXCHANGE` | `True` to exchange tokens; anything else keeps the signed service assertion (`MCP_SERVICE_SIGNING_KEYS`) |
| `LENZ_OAUTH_CLIENT_ID` | The server's OAuth client id at the Lenz API |
| `LENZ_OAUTH_CLIENT_SECRET` | That client's secret. Keep it in a secret store, not in plain configuration |
| `LENZ_TOKEN_ENDPOINT` | Optional. Defaults to `{MCP_API_BASE_URL}/oauth/token` |

Each tool asks only for the scopes it uses (`exchange.TOOL_SCOPES`), and one
exchanged token serves a whole tool call, however long it waits.

Error reporting is off unless you set `SENTRY_DSN`. With it set, the server
reports errors to that Sentry project with `send_default_pii=True`, so events
can include request details such as headers and the caller's IP address. That
data goes only to the Sentry project you configured.

## Pull requests

- Squash merges take the pull request's title, so the title follows the
  conventional-commit form: `fix(card): ...`, `feat: ...`, `docs: ...`.
- `ci-ok` must pass: lint, types, tests on Python 3.11 and 3.12, the card, the
  wheel, the container test and the secret scan.
- Comments say why the code is the way it is. Keep links to private notes,
  scratch paths and pull-request numbers out of them; `scripts/scrub_check.py`
  checks for the common ones.
- Report security issues privately, as `SECURITY.md` describes, not in a pull
  request.
