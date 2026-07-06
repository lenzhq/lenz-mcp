---
name: lenz-fact-check
description: >-
  Fact-check factual claims against independent web sources using the Lenz MCP
  server. Use when the user asks whether something is true, wants a claim /
  paragraph / article / document checked for factual accuracy, or asks you to
  check your own previous answer for hallucinations before they rely on it.
  Requires the Lenz MCP (https://lenz.io/mcp) connected.
---

# Lenz Fact-Check

Fact-check factual claims against **independent web sources** using Lenz's hosted
MCP tools. Lenz runs a claim through a multi-model pipeline (research → debate →
adjudication) and returns a verdict with bucketed confidence. It checks claims
against the open web, independent of whatever context the model was given — so it
complements groundedness/faithfulness checkers, it does not replace them.

## Prerequisite: the Lenz MCP must be connected

This skill drives the **Lenz MCP server** (`https://lenz.io/mcp`) and its tools:
`assess`, `verify`, `get_verification`, `select`, `ask`, `check_usage`. If those
tools are not available, do **not** try to fact-check by other means — tell the
user to connect Lenz first (OAuth for clients that support it, or a free API key),
per https://github.com/lenzhq/lenz-mcp, then retry.

## When to use

- The user asks whether a specific claim is true or false.
- The user asks to check a paragraph, article, or document for factual accuracy.
- The user asks you to check **your own** prior response for hallucinations before
  they act on it.

## Workflow

1. **Extract the atomic claims.** Break the input into discrete, individually
   checkable factual statements — one assertion each. Skip opinions, predictions,
   recommendations, and subjective statements; Lenz checks facts, not judgments.
   If there is no checkable factual claim, say so plainly and stop.

2. **Assess each claim** with `assess` (fast, ~5–10s). It returns a verdict
   (True / Mostly True / Mixed / Mostly False / False) and a bucketed confidence
   per claim. If `assess` reports the claim is **ambiguous** with candidate
   readings, pick the reading that matches the user's intent (or ask which they
   mean), then re-assess that reading.

3. **Escalate to `verify` only when warranted.** `verify` is a deep, sourced,
   ~90s investigation that uses scarce quota — reserve it for claims that are
   consequential (health, safety, legal, financial, reputational), came back
   **Mixed or low-confidence** from `assess`, or that the user explicitly wants
   investigated. Do **not** spend `verify` on trivial or clearly-true claims.
   `verify` returns a `task_id`; poll `get_verification(task_id)` until its status
   is `completed`. If it returns `needs_input` (multiple claims or an ambiguity),
   use `select` to choose which claim text(s) to run. To dig further into a
   finished `verify`, use `ask` with its `verification_id`.

4. **Present the results.** Per claim: state the claim, the verdict, and the
   confidence in plain language. **Lead with the claims that are false or
   uncertain** — that's what the user needs. For deep `verify` results, include the
   executive summary and the top sources. When a result is public, include its
   Lenz link so the user can see the reasoning.

## Guardrails

- **Directional, not absolute.** Confidence is bucketed (high / medium / low), not
  a calibrated probability. Never present a verdict as certain — surface the
  confidence, keep the caveat, and link back to Lenz.
- **Protect `verify` quota.** Default to `assess`; escalate deliberately. If you're
  unsure how much deep-check quota is left, call `check_usage` first.
- **Say when nothing is checkable.** If the input is all opinion / prediction /
  subjective, tell the user there's no factual claim to verify rather than forcing
  a verdict.
- **Multiple claims:** check each and give a per-claim verdict; don't collapse a
  mixed set into one blanket "true" or "false."

## Example

> **User:** Double-check this before I publish: "The Great Wall of China is the
> only man-made object visible from the Moon with the naked eye."
>
> → `assess("The Great Wall of China is visible from the Moon with the naked eye")`
> → **False** (high confidence).
>
> **You:** That's **False** (high confidence) — a persistent myth. The Wall is far
> too narrow to resolve unaided from low Earth orbit, let alone the Moon; no
> man-made structure is visible from the Moon with the naked eye. Treat this as
> directional; see the full reasoning on Lenz. [link]
