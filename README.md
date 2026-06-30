<p align="center">
  <img src="assets/logo.png" alt="Lenz" width="96" height="96">
</p>

<h1 align="center">Lenz MCP Server</h1>

<p align="center">
  Fact-check claims against independent sources — from any MCP client.
</p>

<p align="center">
  <a href="https://lenz.io">Website</a> ·
  <a href="https://lenz.io/api-integration">Get an API key</a> ·
  <a href="https://lenz.io/developers">Developer docs</a>
</p>

---

[Lenz](https://lenz.io) is a fact-checking platform. It takes a factual claim, runs it
through a multi-model pipeline (research → debate → adjudication) against independent
sources, and returns a verdict with calibrated confidence. This is the official
**remote MCP server** — a hosted [Model Context Protocol](https://modelcontextprotocol.io)
endpoint that exposes Lenz's fact-checking as tools your AI assistant can call.

It verifies claims **against the open web**, independent of whatever context your model
was given — so it complements retrieval/groundedness checkers rather than replacing them.

- **Hosted, no install:** point your client at `https://lenz.io/mcp`. Nothing to run locally.
- **Auth:** a free Lenz API key (`Authorization: Bearer lenz_…`).
- **Transport:** Streamable HTTP.

## Tools

| Tool | What it does |
|------|--------------|
| **`assess`** | Fast verdict (~5–10s) via a 3-model panel. The default for checking a claim. Returns one verdict per atomic claim (True / Mostly True / Mixed / Mostly False / False) plus bucketed confidence. |
| **`verify`** | Deep, multi-step investigation (~90s, research → debate → adjudication) for high-stakes claims. Returns a `task_id` immediately; poll it with `get_verification`. Uses scarce deep-check quota. |
| **`get_verification`** | Retrieve or poll a `verify` result by `task_id`. Returns `processing`, `needs_input`, or `completed` (verdict, summary, top sources, and a link if the claim is public). |
| **`select`** | Resolve a `needs_input` verification — when a `verify` turns up multiple claims or an ambiguity, pick which claim text(s) to run. |
| **`check_usage`** | Remaining `assess` / `verify` quota and current plan for your key. |

> Verdicts are **directional, not absolute** — confidence is returned as bucketed
> language with a caveat, not a raw score. Surface it, and the link back to Lenz, to
> the user.

## Quickstart

**1. Get a free API key** at [lenz.io/api-integration](https://lenz.io/api-integration)
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

Settings → **MCP** → **Add new MCP server** → type **HTTP**, URL `https://lenz.io/mcp`,
and add a header `Authorization: Bearer <your-key>`. (Or drop the JSON above into
`.cursor/mcp.json`.)

### VS Code

```bash
code --add-mcp '{"name":"lenz","type":"http","url":"https://lenz.io/mcp","headers":{"Authorization":"Bearer ${LENZ_API_KEY}"}}'
```

### MCP Inspector (try the tools by hand)

```bash
npx @modelcontextprotocol/inspector
# Transport: Streamable HTTP · URL: https://lenz.io/mcp
# Header: Authorization: Bearer <your-key>
```

## Example

> **You:** Is it true that honey never spoils?
>
> The assistant calls `assess("Honey never spoils")` and gets back:
> *Mostly True* — high confidence. Properly sealed honey can keep effectively
> indefinitely thanks to its low moisture and acidity; the caveat is contamination or
> added water. For a sourced deep-dive it can escalate with `verify`.

## Quota

Free keys include a monthly allotment of fast `assess` checks and a smaller number of
deep `verify` checks. Call `check_usage` to see what's left, or see plans at
[lenz.io](https://lenz.io). `verify` is the expensive path — reserve it for claims that
warrant a sourced investigation.

## Links

- **Website:** [lenz.io](https://lenz.io)
- **Get a key:** [lenz.io/api-integration](https://lenz.io/api-integration)
- **Developer docs:** [lenz.io/developers](https://lenz.io/developers)
- **SDKs:** [Python](https://github.com/lenzhq/lenz-io-python) · [Node](https://github.com/lenzhq/lenz-io-node)

## Support

Questions or issues? [Open an issue](https://github.com/lenzhq/lenz-mcp/issues) or
[get in touch](https://lenz.io/contact).

## License

[Apache-2.0](LICENSE) © lenzhq. The Lenz name and logos (`assets/`) are
trademarks of lenzhq and are not granted by the license — see [NOTICE](NOTICE).
