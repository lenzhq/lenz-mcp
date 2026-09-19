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
  <a href="https://claude.ai/directory/connectors/lenz"><img src="https://img.shields.io/badge/Add_to_Claude-D97757?style=for-the-badge&logo=claude&logoColor=white" alt="Add Lenz to Claude" height="28"></a>
  &nbsp;
  <a href="https://cursor.com/en/install-mcp?name=lenz&config=eyJ1cmwiOiJodHRwczovL2xlbnouaW8vbWNwIn0%3D"><img src="https://cursor.com/deeplink/mcp-install-dark.svg" alt="Add Lenz to Cursor" height="28"></a>
  &nbsp;
  <a href="https://vscode.dev/redirect/mcp/install?name=lenz&config=%7B%22type%22%3A%22http%22%2C%22url%22%3A%22https%3A%2F%2Flenz.io%2Fmcp%22%7D"><img src="https://img.shields.io/badge/Install_in_VS_Code-0098FF?style=for-the-badge&logo=visualstudiocode&logoColor=white" alt="Install Lenz in VS Code" height="28"></a>
</p>

<p align="center"><sub>One-click install (OAuth — no API key needed).</sub></p>

---

[Lenz](https://lenz.io) is a fact-checking platform. It takes a factual claim, runs it
through a multi-model pipeline (reading the claim → research → debate → panel review → conclusion)
against independent sources, and returns a verdict with a confidence level. This is the official
**remote MCP server** — a hosted [Model Context Protocol](https://modelcontextprotocol.io)
endpoint that exposes Lenz's fact-checking as tools your AI assistant can call.

It verifies claims **against the open web**, independent of whatever context your model
was given — so it complements retrieval/groundedness checkers rather than replacing them.

- **Hosted, no install:** point your client at `https://lenz.io/mcp`. Nothing to run locally.
- **Auth:** **OAuth** (no key — for clients that support it) or a free Lenz **API key** (`Authorization: Bearer lenz_…`).
- **Transport:** Streamable HTTP, stateless. Supports the 2025-11-25 and 2026-07-28 protocol revisions.

## Tools

| Tool | What it does |
|------|--------------|
| **`assess_claim`** | The quick check, and the default first step for one claim or a whole text. A 3-model panel returns one row per claim: a verdict (True / Mostly True / Mixed / Mostly False / False), a bucketed confidence and, when available, a short `rationale` (the reasoning of a reviewer who agrees with the verdict) and a `dissent` (the reasoning of the reviewer farthest from it). Both are reviewers' notes, not checked sources. A low-confidence row carries `recommend_verify: true` and a `next_step` sentence. About 15-20 seconds through an assistant; no sources shown. Pass a text in `claim`, whole and unedited (Lenz finds the claims in it, up to 20), or use `claims` for up to 20 claims the user listed separately. |
| **`verify_claim`** | The deep check: reading the claim → research → debate → panel review → conclusion, for ONE claim. Returns a verdict, a 1-10 score, the key finding, a summary, warnings, and the top sources with the quote each one rests on, plus the total source count. A deep check takes about a minute to a minute and a half. The call waits for it, for as long as the client allows: in Claude and the ChatGPT app the result usually comes back in the same call; in clients with a shorter tool-call limit (Claude Code, Cursor, VS Code, OpenAI's Responses API) it usually returns `status: submitted` with a `task_id`, and `get_verification` waits for the rest. A completed deep check replaces any earlier quick verdict on the same claim. Costs ten times an `assess_claim` row, so assistants offer it and run it on the user's yes, or when the user asks for sources. `depth: "low"` researches fewer sources, with the same models, for half the credits. |
| **`get_verification`** | Wait for a running `verify_claim` by `task_id`, or fetch a completed result by its 8-character `verification_id`. Returns `processing`, `needs_input`, `failed`, `not_found` or `completed`. |
| **`select_claims`** | Resolve a `needs_input` verification: when a text holds several claims, choose which claim text(s) to run. |
| **`ask_followup`** | Ask a grounded follow-up about a completed `verify_claim` (by its `verification_id`), answered from the full research, debate and panel review, not just the summary. Costs the same as one `assess_claim` row. |
| **`list_verifications`** | The account's most recent completed deep checks, newest first. Use it to get back a result that finished after the conversation moved on. Read-only and free; quick checks are not stored. |
| **`check_usage`** | Remaining credits, the per-tool price list (`costs`, plus `cost_options` for prices that depend on a parameter such as `depth`), and the current plan. Never a prerequisite for a check. |

**Prompts.** Clients that surface MCP prompts (Claude shows them under **+**) get two: **Check this text with Lenz** and **Check your last answer with Lenz**. Each takes the text to check and starts a quick check. Lenz cannot see the conversation, so **Check your last answer** needs the answer pasted in.

> Verdicts are **directional, not absolute**: confidence is returned as bucketed
> language with a caveat, not a calibrated probability. A quick verdict is a first read;
> when a deep check disagrees with it, the deep check is the one to rely on, because it
> shows its sources and its reasoning.

## The result card

In **Claude** and the **ChatGPT app**, a check comes back as a card in the conversation as
well as text. From the card you can run the deep check on a claim (**Check against
sources**, a standard-depth `verify_claim`), open a source, or pick which claims in a draft
to check in depth. When a deep check started from the card finishes, the assistant is told
the result: silently in Claude, and by a short message in the chat in ChatGPT. Every other
client gets the same results as text; nothing depends on the card.

## Quickstart

Two ways to connect, depending on your client:

- **OAuth** — for clients that support it (e.g. Claude connectors). No key to
  paste; you sign in to Lenz and authorize the connection.
- **API key** — works with any MCP client via an `Authorization` header.

### Connect with OAuth (no API key)

**Claude (web, desktop and mobile):** Lenz is an official Claude connector.
Open the [Lenz connector page](https://claude.ai/directory/connectors/lenz) and
click **Connect to Claude**, or in the app: **Settings → Extensions → Browse
extensions**, search for Lenz and click **+**. Either way it is a one-time
OAuth sign-in with nothing to configure, and one account covers all three.

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
config. Your assistant then fact-checks on your behalf against your Lenz account's credits.
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

At this time, ChatGPT connects to Lenz as a custom app through OpenAI's
**Developer mode** — over OAuth, so there is no API key to paste. It is set up
on chatgpt.com; the ChatGPT desktop and mobile apps cannot create one.

**On a personal account (Plus or Pro).** **Settings → Plugins** → turn on
**Developer mode** at the bottom → **Plugins → Browse plugins** → **+** next to
Search → name it Lenz, URL `https://lenz.io/mcp`, Authentication **OAuth** →
tick **I understand and want to continue** → **Create** → **Sign in with
Lenz** → **Try in Chat**.

**In a Business, Enterprise or Edu workspace.** An owner or admin publishes it
once: **Workspace settings → Apps → + Create**, confirm **Enable developer
mode**, fill in the same form with **OAuth**, then **Drafts → Publish**, set
who can use it, and **Publish** again. Each member then goes to **Settings →
Plugins → Lenz → Connect** and signs in.

A custom app is off by default in every new chat: **+** and tick Lenz. Naming
Lenz in the question is what makes ChatGPT reach for it rather than answer from
memory.

### MCP Inspector (try the tools by hand)

```bash
npx @modelcontextprotocol/inspector
# Transport: Streamable HTTP · URL: https://lenz.io/mcp
# Header: Authorization: Bearer <your-key>
```

## Example

> **You:** Check with Lenz whether indeed 90% of startups fail within their first year.
>
> The assistant calls `assess_claim` and gets back *False*, high confidence, with the
> reviewers' reasoning: most new businesses survive their first year, and official
> figures put first-year closures at about one in five. It presents that as a first
> read and offers a deep check. On your yes it calls `verify_claim` (and, in clients with
> a short tool-call limit, `get_verification` to collect the result) and returns the
> verdict with its score, the key finding, the warnings and the sources behind it.

## Credits

Every tool call draws on **one pool of credits** on your Lenz account. There is no
separate budget per tool: spending on `assess_claim` reduces what is left for
`verify_claim`, and vice versa. `check_usage` returns the balance
(`credits_remaining`) alongside `costs`, the live price list — read the weight from
there rather than assuming one.

`verify_claim` is the expensive path by an order of magnitude; `assess_claim` and
`ask_followup` are the cheap ones. `verify_claim` also takes an optional `depth`:
`"low"` runs a shallower research pass (fewer sources, the same models, only slightly quicker) for
half the credits of the default `"standard"`. That price sits under
`cost_options.verify.depth.low` on `check_usage`. You are charged for the depth you
**request**; the `depth` on the completed result is the depth the verdict was
**produced** at, so a `"low"` request answered from an existing deeper check reads
`"standard"` — the echo describes the evidence, the charge follows the request.

`check_usage` also reports `assess_remaining` and `verify_remaining`, which are the
SAME balance projected into each tool's own unit — "how many of these could I still
make" — not separate allowances.

Free keys include a monthly allowance that resets each period; grants and top-ups add
non-expiring extra credits on top, spent only once the allowance is gone. See plans at
[lenz.io/plans](https://lenz.io/plans).

**When you run out**, tools return `status: "quota_exhausted"` with a `message` and,
except in ChatGPT, a `manage_url` pointing at the plans page. It is not retryable — the balance is spent
until you top up or the monthly allowance resets. An agent should say so plainly rather
than retrying or quietly skipping the check.

`status: "rate_limited"` is a different thing: a rate limit, not a spent balance. That
one does clear on its own, and the result carries `retry_after_seconds` telling you when.

## Skills

Prefer a guided workflow to calling the tools yourself? The
[`lenz-fact-check`](skills/lenz-fact-check) skill turns "is this true?" into a
structured pass: it runs a quick check on the text, recommends a deep `verify_claim` for the
claims that matter, and reports verdicts with their confidence, and sources where a deep
check ran (with the directional-not-absolute caveat built in). Point your agent at
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
