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

## When a host changes how it presents itself

Three things this server does depend on which client is asking: which tools it
is listed, how its card hands the result to the model, and how long a deep
check may wait inside one tool call. Hosts change how they present themselves —
a new User-Agent suffix, a different capability set, a different protocol era —
and when that happens a decision can quietly start coming out differently.

Two of the three are keyed on facts a host restates on every request (its
declared MCP Apps support, its vendor token), so they follow such a change on
their own. The wait is keyed on the client's full identity, deliberately: a
wait that is too long is a hard timeout the user sees, so an unrecognised
client gets the short default rather than inheriting a longer row. That is the
case that needs a repair, and the repair is one line.

The server states every one of these decisions in its log
(`src/lenz_mcp/decisions.py`), so start there:

```
mcp_manifest identity=… client=… era=… declared=… vendor=… card=on|off reason=… wait=…
mcp_card_delivery identity=… deliver=message|context
mcp_verify_wait_exhausted identity=… wait=… tool=…
```

- **A known client is on the default wait** — `wait=45` for a host that should
  have its own row, or a steady stream of `mcp_verify_wait_exhausted` from one
  identity. Add the identity to `client.KNOWN_IDENTITIES` and a row to
  `config.VERIFY_WAIT_SECONDS_BY_IDENTITY`; a test fails if you do only one of
  those. Use the same app's measured ceiling when the surface is the same one
  under a new name, and measure with `scripts/probe/` when it is genuinely new.
- **`card=off reason=no_declaration` for a host that should render one.** The
  host has stopped declaring MCP Apps support. Re-measure with `scripts/probe/`
  before changing anything: the declaration is the client's own statement about
  what it can render, and overriding it is how a card gets sent to a host that
  will not draw it.
- **`card=off reason=read_failed_token`** is ours, not theirs — the declaration
  could not be read at all. The ERROR line beside it carries the traceback.
- **The card renders but the model contradicts it** ("no full verification was
  run", beside a sourced verdict). Check `mcp_card_delivery` for that identity:
  a host that accepts the silent context push and drops it needs
  `deliver=message`, keyed on the vendor token in
  `mcp_card.MESSAGE_DELIVERY_VENDOR_TOKENS`.

A change here ships as a release: a PR, then a tag, then a pin bump on the API
side.

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
user, and it gets one with an OAuth 2.0 token exchange (RFC 8693) at the Lenz
API's token endpoint, authenticating as the server's own OAuth client. There is
no other credential path: **a deployment with OAuth on and no client
credentials cannot serve an OAuth caller** — every tool call answers that the
service is unavailable. API keys are unaffected and need none of this.

| Variable | What it is |
|---|---|
| `LENZ_OAUTH_CLIENT_ID` | Required with OAuth on. The server's OAuth client id at the Lenz API |
| `LENZ_OAUTH_CLIENT_SECRET` | Required with OAuth on. That client's secret. Keep it in a secret store, not in plain configuration |
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
