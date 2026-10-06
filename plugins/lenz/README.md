# Lenz for Claude Code

Lenz checks the factual claims in a draft or an answer against independent sources: a quick verdict with a confidence for each claim, or a deep check that shows the key finding and its sources.

This plugin adds the Lenz server (`https://lenz.io/mcp`) and the `lenz-fact-check` skill. Install it:

```bash
claude plugin marketplace add lenzhq/lenz-mcp
claude plugin install lenz@lenz
```

The first time Lenz is used, Claude Code asks you to sign in to your Lenz account (or run `/mcp`). No API key is needed. Free accounts include monthly credits.

Then ask in plain language, for example: "Check the claims in this draft with Lenz."

More: [lenz.io/integrations/mcp-server](https://lenz.io/integrations/mcp-server) · [Privacy](https://lenz.io/privacy) · [Terms](https://lenz.io/terms)
