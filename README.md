<p align="center">
  <img src="assets/logo.png" alt="Lenz" width="96" height="96">
</p>

<h1 align="center">Lenz MCP Server</h1>

<p align="center">
  Fact-check claims against independent sources — from any MCP client.
</p>

<p align="center">
  <a href="https://lenz.io">Website</a> ·
  <a href="https://lenz.io/api-credentials">Get an API key</a> ·
  <a href="https://lenz.io/developers">Developer docs</a>
</p>

<p align="center">
  <a href="https://cursor.com/en/install-mcp?name=lenz&config=eyJ1cmwiOiJodHRwczovL2xlbnouaW8vbWNwIn0%3D"><img src="https://cursor.com/deeplink/mcp-install-dark.svg" alt="Add Lenz to Cursor" height="28"></a>
  &nbsp;
  <a href="https://vscode.dev/redirect/mcp/install?name=lenz&config=%7B%22type%22%3A%22http%22%2C%22url%22%3A%22https%3A%2F%2Flenz.io%2Fmcp%22%7D"><img src="https://img.shields.io/badge/Install_in_VS_Code-0098FF?style=for-the-badge&logo=visualstudiocode&logoColor=white" alt="Install Lenz in VS Code" height="28"></a>
</p>

<p align="center"><sub>One-click install (OAuth — no API key needed).</sub></p>

---

[Lenz](https://lenz.io) is a fact-checking platform. It takes a factual claim, runs it
through a multi-model pipeline (framing → research → debate → panel review → conclusion)
against independent sources, and returns a verdict with a confidence level. This is the official
**remote MCP server** — a hosted [Model Context Protocol](https://modelcontextprotocol.io)
endpoint that exposes Lenz's fact-checking as tools your AI assistant can call.

It verifies claims **against the open web**, independent of whatever context your model
was given — so it complements retrieval/groundedness checkers rather than replacing them.

- **Hosted, no install:** point your client at `https://lenz.io/mcp`. Nothing to run locally.
- **Auth:** **OAuth** (no key — for clients that support it) or a free Lenz **API key** (`Authorization: Bearer lenz_…`).
- **Transport:** Streamable HTTP.

## Tools

| Tool | What it does |
|------|--------------|
| **`assess_claim`** | Fast verdict (~5–10s) via a 3-model panel. The default for checking a claim. Returns one verdict per atomic claim (True / Mostly True / Mixed / Mostly False / False) plus bucketed confidence. |
| **`verify_claim`** | Deep, multi-step investigation (~90s: research → debate → panel review) for high-stakes claims. Returns a `task_id` immediately; poll it with `get_verification`. Uses scarce deep-check quota. |
| **`get_verification`** | Retrieve or poll a `verify_claim` result by `task_id`. Returns `processing`, `needs_input`, or `completed` (verdict, summary, top sources, and a link if the claim is public). |
| **`select_claims`** | Resolve a `needs_input` verification — when a `verify_claim` turns up multiple claims or an ambiguity, pick which claim text(s) to run. |
| **`ask_followup`** | Ask a grounded follow-up about a completed `verify_claim` (by its `verification_id`) — answered from the full research, debate, and panel review, not just the summary. Uses `ask_followup` quota. |
| **`check_usage`** | Remaining `assess_claim` / `verify_claim` quota and current plan for your key. |

> Verdicts are **directional, not absolute** — confidence is returned as bucketed
> language with a caveat, not a raw score. Surface it, and the link back to Lenz, to
> the user.

## Quickstart

Two ways to connect, depending on your client:

- **OAuth** — for clients that support it (e.g. Claude connectors). No key to
  paste; you sign in to Lenz and authorize the connection.
- **API key** — works with any MCP client via an `Authorization` header.

### Connect with OAuth (no API key)

**Claude (web and desktop):** Lenz is listed in the Claude connectors directory —
open **Settings → Connectors → Browse connectors**, find Lenz, and connect. One-time
OAuth sign-in, nothing to configure.

For any other client that supports OAuth for MCP, add the server with just its URL
and no headers:

```json
{
  "mcpServers": {
    "lenz": {
      "type": "http",
      "url": "https://lenz.io/mcp"
    }
  }
}
```

The first time you use it, your client walks you through a one-time sign-in: you
authenticate on Lenz's own screen and authorize the connection — no key is stored in your
config. Your assistant then fact-checks on your behalf against your Lenz account's quota.
You can revoke the connection at any time; see the [privacy policy](https://lenz.io/privacy).

### Connect with an API key

**1. Get a free API key** at [lenz.io/api-credentials](https://lenz.io/api-credentials)
(format `lenz_…`).

**2. Add the server** to your client (examples below). Authenticate with
`Authorization: Bearer <your-key>`.

### Claude Code

```bash
claude mcp add --transport http lenz https://lenz.io/mcp \
  --header "Authorization: Bearer ${LENZ_API_KEY}"
```

### Claude Desktop / any client that reads `.mcp.json`

```json
{
  "mcpServers": {
    "lenz": {
      "type": "http",
      "url": "https://lenz.io/mcp",
      "headers": {
        "Authorization": "Bearer ${LENZ_API_KEY}"
      }
    }
  }
}
```

### Cursor

**One click:** use the [**Add Lenz to Cursor**](https://cursor.com/en/install-mcp?name=lenz&config=eyJ1cmwiOiJodHRwczovL2xlbnouaW8vbWNwIn0%3D)
button above — it adds the server and signs you in via OAuth (no key to paste).

Manual: Settings → **MCP** → **Add new MCP server** → type **HTTP**, URL
`https://lenz.io/mcp`, and add a header `Authorization: Bearer <your-key>`. (Or drop the
JSON above into `.cursor/mcp.json`.)

### VS Code

**One click:** use the [**Install in VS Code**](https://vscode.dev/redirect/mcp/install?name=lenz&config=%7B%22type%22%3A%22http%22%2C%22url%22%3A%22https%3A%2F%2Flenz.io%2Fmcp%22%7D)
button above (OAuth, no key). Manual, with a key:

```bash
code --add-mcp '{"name":"lenz","type":"http","url":"https://lenz.io/mcp","headers":{"Authorization":"Bearer ${LENZ_API_KEY}"}}'
```

### ChatGPT

ChatGPT connects to Lenz as a **custom connector** over OAuth — no API key.

Settings → **Apps** → **Advanced settings** → turn **Developer mode** on →
**Add app** → URL `https://lenz.io/mcp` → **Create**, then **Sign in with Lenz**
to authorize.

*(A one-click install from the ChatGPT app directory is coming.)*

### MCP Inspector (try the tools by hand)

```bash
npx @modelcontextprotocol/inspector
# Transport: Streamable HTTP · URL: https://lenz.io/mcp
# Header: Authorization: Bearer <your-key>
```

## Example

> **You:** Is it true that honey never spoils?
>
> The assistant calls `assess_claim("Honey never spoils")` and gets back:
> *Mostly True* — high confidence. Properly sealed honey can keep effectively
> indefinitely thanks to its low moisture and acidity; the caveat is contamination or
> added water. For a sourced deep-dive it can escalate with `verify_claim`.

## Quota

Free keys include a monthly allotment of fast `assess_claim` checks and a smaller number of
deep `verify_claim` checks. Call `check_usage` to see what's left, or see plans at
[lenz.io/plans](https://lenz.io/plans). `verify_claim` is the expensive path — reserve it for
claims that warrant a sourced investigation.

**When you run out**, tools return `status: "quota_exhausted"` with a `message` and a
`manage_url` pointing at the plans page. It is not retryable — the balance is spent
until you top up or the monthly quota resets. An agent should say so plainly rather
than retrying or quietly skipping the check.

`status: "rate_limited"` is a different thing: a rate limit (today, the per-account daily
`extract` cap). That one does clear on its own, and the result carries
`retry_after_seconds` telling you when.

## Skills

Prefer a guided workflow to calling the tools yourself? The
[`lenz-fact-check`](skills/lenz-fact-check) skill turns "is this true?" into a
structured pass — it extracts the checkable claims, runs `assess_claim` on each, escalates
high-stakes ones to a deep `verify_claim`, and reports verdicts with confidence and
sources (with the directional-not-absolute caveat built in). Point your agent at
[`skills/lenz-fact-check/SKILL.md`](skills/lenz-fact-check/SKILL.md).

## Links

- **Website:** [lenz.io](https://lenz.io)
- **Get a key:** [lenz.io/api-credentials](https://lenz.io/api-credentials)
- **Developer docs:** [lenz.io/developers](https://lenz.io/developers)
- **SDKs:** [Python](https://github.com/lenzhq/lenz-io-python) · [Node](https://github.com/lenzhq/lenz-io-node)

## Support

Questions or issues? [Open an issue](https://github.com/lenzhq/lenz-mcp/issues) or
[get in touch](https://lenz.io/contact).

## License

[Apache-2.0](LICENSE) © lenzhq. The Lenz name and logos (`assets/`) are
trademarks of lenzhq and are not granted by the license — see [NOTICE](NOTICE).

## Maintainer

[@Pavel12431432](https://github.com/Pavel12431432)
