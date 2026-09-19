# Dev probe connector

A throwaway MCP server for short click-throughs in Claude. It is **not** lenz-mcp:
it is never deployed, nothing in the app imports it, and it has no auth, so run it
only while you test and stop it afterwards.

It answers these questions:

1. **How long can one tool call run in a remote connector?** `sleep_probe` sleeps N
   seconds.
2. **Does Claude render a remote connector's card?** `card_probe` returns an MCP Apps
   card (a `ui://` resource), and the card prints what Claude told it (host,
   capabilities, theme, styles, size).
3. **What can the card's buttons do?** Put a message into the chat, call a tool
   that is not read-only, tell the model something, open a link, go fullscreen.
4. **Does the real Lenz card work in Claude, in every state?** `card_fixture` (section 7).

**Run 1, 2026-09-17:** Claude Desktop cut tool calls at ~240 s — on the LEGACY
protocol this probe spoke then; the card renders and follows the theme. **Run 2** is
section 6 below. **On the modern protocol (2026-07-28), which Claude negotiates with
lenz-mcp, 150 s completes and 210 s is cut** (2026-09-18, with a direct 210 s
control call that completed; the exact ceiling in between is unmeasured).
`VERIFY_WAIT_SECONDS_BY_USER_AGENT` holds Claude's whole call under the 150 s.

Every request is logged to the terminal as one JSON line, including what Claude
declares when it connects. Everything Claude answers the card's buttons is shown in
the card's log and copied to the terminal as `card.*` lines (through `card_log`, an
app-only tool the model is not meant to see).

## 1. Start it

From the repo root:

```bash
mkdir -p out && MCP_PROBE_PORT=8799 uv run python scripts/probe/server.py | tee out/probe.log
```

It listens on `http://127.0.0.1:8799/mcp`. Without `MCP_PROBE_PORT` it uses 8765;
pick any free port and use the same one in step 2.

## 2. Expose it

Claude calls custom connectors from Anthropic's servers, so localhost is not
reachable. In a second terminal:

```bash
ngrok http 8799
```

Copy the `https://….ngrok-free.app` forwarding URL. The connector URL is that
address plus `/mcp`, for example `https://abcd-1-2-3-4.ngrok-free.app/mcp`.

(Any HTTPS tunnel works, for example `cloudflared tunnel --url http://localhost:8799`.)

## 3. Add it to Claude (remove it first if it is already there)

Claude caches the card by its URI AND the tool list per connector. A probe that
changed since you last added it is invisible until the connector is refreshed, so
**remove and re-add the connector first**, even if the name and URL are unchanged.

In Claude (Desktop or claude.ai), open Settings, go to Connectors, and add a custom
connector:

- Name: `lenz-probe`
- URL: the tunnel URL ending in `/mcp`
- No OAuth

Start a new chat and make sure `lenz-probe` is enabled for it.

## 4. Timeout probe (run 1)

1. `Call the lenz-probe sleep_probe tool with seconds 30.`
2. The same with `50`, then `70`, `90`, `120`, `180`.

For each one, write down whether a result came back and what Claude showed if it
did not. `client.gave_up` with `after` in the terminal is the second Claude hung up.
Then take the first N that failed and try `… with seconds <N> and progress true.`

## 5. Card probe (run 1)

`Call the lenz-probe card_probe tool.` Write down whether a card appeared, its
Host, Theme, Style variables and Container lines, whether the Theme line changes
when Claude switches light/dark, and whether the card fits its content.

## 6. Card buttons (run 2, about five minutes)

In a new chat: `Call the lenz-probe card_probe tool.`

The card title must read **Lenz probe card (v2)** with seven numbered buttons. If
it shows the old card, the connector was not refreshed: go back to step 3.

First copy the **Capabilities** line (what Claude says the card may do). Then
press the buttons in this order. For each, write down what Claude showed (any
approval prompt: copy its text), and the reply line the card logged underneath.

1. **Ping (card_ping, read-only).** Approval prompt or not?
2. **Call spend_probe directly.** `spend_probe` is marked NOT read-only. Approval
   prompt or not, and its text? Note the `result_token` in the card's log (e.g.
   `spend-3fa2c1`).
   - Then type in the chat: `What did the spend_probe tool just return?`
     Does Claude know, and does it quote the same `result_token`?
3. **Tell Claude the secret word (ui/update-model-context).** The card shows its
   secret word; Claude has never seen it. Does anything appear in the chat?
   - Then type: `What is the Lenz probe card's secret word?`
     Does Claude answer with the word the card shows?
4. **Ask Claude to run a check (ui/message).** Does a message appear in the chat as
   if you had typed it? Did Claude ask you to confirm first? Does Claude then call
   `spend_probe` (a new `spend_probe.called` line with note `from the card button`
   in the terminal)?
5. **Open lenz.io (ui/open-link).** Did lenz.io open? Was there a confirmation?
6. **Fullscreen (ui/request-display-mode).** What `mode` did the reply say, and did
   the card actually go fullscreen?
7. **Back inline.** Same questions.

## 7. The Lenz card, every state (run 3, about ten minutes, no credits)

This is the real card lenz-mcp serves, built with a **Dev fixture** switcher on top
(the card lenz-mcp serves has no switcher; a test checks that). Its states come from
synthetic responses in the Lenz API's exact shape, run through lenz-mcp's own code.
The button does not reach
Lenz: the probe answers it with a made-up check that takes about ten seconds.

The dev card is committed (`src/lenz_mcp/card/dist-dev/`), so nothing needs building.
Only after changing the card's source, rebuild it (Node 20+), from the repo root:
`cd src/lenz_mcp/card && npm ci && npm run build:dev && cd -`.

Then steps 1 to 3 as usual (**remove and re-add the connector**: the tool list changed).

1. Type: `Use the lenz-probe card_fixture tool.` A Lenz quick-check card appears,
   with a `Dev fixture` menu above it set to **Live**.
2. **Live run.** Press **Check against sources**. It should show "Checking against
   sources", go through the steps, and turn into the checked result in about ten
   seconds. Write down: did Claude ask for approval before the button started?
3. **Did the result reach Claude?** Type: `What did the Lenz card just find?`
   Claude should answer from the card (the chocolate claim, True, 10/10) without
   calling a tool. Note whether it called one.
4. **Reopen.** Scroll away and back, or reload the chat. Does the card come back
   showing the result, without running again? (The terminal shows
   `fake_get.recovered`, not a new `fake_start.called`.)
5. **Every state.** Pick states from the menu, or press **Next** to step through
   them all. For states marked "press the button", press it. Nothing here calls a
   tool. Look at each in light and dark (switch Claude's theme): anything cut off,
   hard to read, or out of line?
6. **Are the card's tools hidden from Claude?** Type: `Which lenz-probe tools can
   you use?` Claude should list `card_fixture` and the other probe tools, but NOT
   `start_verification_widget` or `get_verification_widget`.
7. **Links.** In a checked result, click a source title. Did it open? Was there a
   confirmation? Then **Ask a follow-up**: does the text appear in the chat box
   without being sent?

## 8. Record the results

Fill in a copy of `results-template.md` and keep the log beside it
(`out/probe.log`). Then stop the server and the tunnel, and remove the
connector from Claude.

## 9. Still unverified in real Claude (the card's assumption list)

The card is built and tested against a stub host that implements the MCP Apps spec
(the card's `tests/harness.mjs`). Everything below is an assumption about what
Claude *actually* does that no test can settle. Keep this list current as the card
grows: one row per assumption, what it rests on, how to check it in section 7, and
what we do if it turns out false. A "no" is a known fallback, never a redesign.

| # | Assumption | What it rests on | How to check (section 7) | If it fails |
|---|---|---|---|---|
| 1 | An **unlisted** `ui://` resource still renders when a tool result names it in `_meta.ui.resourceUri`. | Run 1 rendered a card whose resource WAS listed. Unlisted is inferred: the card is read by URI, and listing it would also offer it under "+", where picking it pastes raw HTML into the chat. | Step 1: `card_fixture` shows a card. | List the resource again and accept that it appears under "+". One line in `mcp_card.py` (`install_per_client_resource_list` stops filtering). |
| 2 | Tools marked `_meta.ui.visibility: ["app"]` are hidden from the model. | The MCP Apps spec; run 2 showed the model never saw a card-initiated call's result. | Step 6: ask which lenz-probe tools Claude can use. | The tools stay listed but the model can call them. Their descriptions already say "Called by the Lenz card, never by the assistant"; the start tool refuses without the card flag, and it is the API that enforces credits and ownership. |
| 3 | `localStorage` works in the card's sandbox, and is scoped so one conversation does not read another's records. | The spec's `domain` note. Not measured. | Step 4: reopen after a run; the terminal shows `fake_get.recovered`, not a new `fake_start.called`. | No recovery: a reopened card shows the quick verdict and the button again (the designed fallback). A press within 24 h replays the same check at no new charge. Records already expire (24 h / 7 days) and name their claim. |
| 4 | `ui/update-model-context` reaches the model for the REAL card, as it did for the probe card. | Run 2: Claude answered with the probe card's secret word. | Step 3: ask what the card found; Claude should answer without calling a tool. | "Ask a follow-up" carries the verification id, so the model can fetch the result with `get_verification`. The card loses nothing else. |
| 5 | Claude can tell that a card rendered, reliably enough for an instruction about what to write beside a card. | Nothing. This is the open question in section 10. | Read what Claude writes beside the card across several checks. | Leave Claude's text alone and keep the full `presentation` guidance for hosts without cards. |
| 6 | Claude caches the card by URI and the tool list per connector, so a new bundle needs a new URI and the connector re-added. | Run 1 measured exactly this. | Bump `cardVersion` + `CARD_URI`, rebuild, re-add the connector. | Nothing to do: every published URI keeps serving its own committed bundle, so old chats stay correct. |
| 7 | A card's direct `tools/call` runs with no approval prompt, even for a tool that is not read-only. | Run 2 measured it with `spend_probe`. | Step 2: press the button and watch for a prompt. | Swap the button's handler to the chat route (`ui/message`). The card's design does not change. |
| 8 | Claude's style variables are `light-dark()` values, so the card must set `color-scheme` from the theme. | Runs 1 and 2 measured it; the card does it. | Switch Claude's theme with a card on screen. | Already handled; if a host sends no variables, the card's Ledger fallbacks apply. |
| 9 | The inline frame is about 735 px wide on desktop and about 360 px on mobile. | Run 1 measured the desktop width. | Look at the card on both. | The card is fluid and checked at 320, 360 and 735 px; nothing breaks, the layout just reflows. |

## 10. ChatGPT: what pass 1 measured, and the one open question (2026-09-17)

Measured with `card_probe` (the standard MCP Apps card: `_meta.ui.resourceUri` on
the tool, resource as `text/html;profile=mcp-app`, no `openai/` keys):

- It renders. ChatGPT reads the card resource again after the tool call, not only
  at connect, and the `ui/initialize` handshake works.
- `hostCapabilities`: openLinks, logging, serverTools, serverResources, message,
  updateModelContext, sandbox (microphone permission). No downloadFile, which
  Claude has.
- A card's own `tools/call` needs no approval and arrives as `openai-mcp/1.0.0`;
  the model's calls carry `openai-mcp/1.0.0 (Codex)`. Our UA gate reads the
  leading token, so both read as ChatGPT.
- A card call's `_meta` carries `openai/userAgent`, `openai/locale` and
  `openai/userLocation` with city, region, timezone and coordinates. Nothing may
  store that wholesale: the connector's `protocol_log.py` records key NAMES only, never
  values, and must stay that way.
- `ui/notifications/host-context-changed` arrives in a storm: about 25 within a
  second of mount. The card treats it as cheap and idempotent (no server call, no
  re-handshake, no re-render loop); `npm run test:host` covers 30 in 100 ms.
- Tool calls are cut at 120 s, and progress notifications do not extend it. Card
  polls are short, so the card is fine; a model-run `verify_claim` wait for this
  UA must stay under it (it is 100 s; Claude's is 130 s, sized to the 150 s
  proven on the modern protocol).

The card's buttons in ChatGPT (18:53-19:00), which decide the per-host adapter:

- A card's own tool call, read-only or not, runs with no approval prompt in under
  two seconds, and the result reaches only the card. Asked afterwards, the model
  did not know a token the card had received. The direct-button design holds.
- `ui/update-model-context` is DECLARED, returns success, and is never delivered:
  asked twice, the model never knew the pushed word. For ChatGPT the adapter must
  treat it as ABSENT, whatever the capability flag says. How the model learns a
  card's result is therefore a per-host answer, not one mechanism.
- `ui/message` is SENT as a user turn at once (Claude prefills the composer and
  waits). So in ChatGPT the card's route is one message that CARRIES the result
  and asks for nothing to be run. Three consequences for the card: the text must
  read as a human chat message, since the user sees it; it is sent once per
  completed check and never on a re-mount, or a reopened chat re-posts it (the
  card's "already pushed" record is per card and persisted, which covers it); and
  a 20-row list must never post 20 messages — one per check the user started,
  nothing for the quick rows.
- A second card in the same chat causes no new `resources/read`: the card file is
  reused per chat. A card stays in the conversation after it sends a message.

The real Lenz card in ChatGPT (run 3, section 7's click-through):

- `card_fixture` rendered clean in light and dark, and the deep check ran end to
  end from the card's own button in both. The layout needed nothing: the theme
  seam's Ledger defaults carry a host that publishes no variables we map.
- Older cards in the same chat stay live: scroll back, press a button, it works.
  So a card must never assume it is the newest thing on screen — which is why a
  reopened card adopts its stored run instead of starting one.
- Opening a source link asks the user to confirm, and fullscreen works.
- What ChatGPT wrote beside the card was already what we would ask for: it
  described the check without narrating the card or repeating the verdict.

**And the finding that decides the adapter:** after the card's deep check completed,
ChatGPT told the user **"no full verification was run"** — because
`ui/update-model-context` is never delivered (above) and the model only ever saw
the quick result. A silent failure is the worst case here: the card shows a
sourced verdict while the assistant contradicts it in the same turn. So for
ChatGPT a card-sent `ui/message` carrying the result is **required**, not a
fallback, and the adapter owns the choice: the card asks the host to "tell the
model this", and the adapter delivers it by whatever mechanism that host has.
The stub-host flow that pins it: a host which accepts `updateModelContext` and
drops it must still see the result delivered by message, exactly once, and still
nothing on a re-mount.

### What the adapter does for ChatGPT

- One card, two hosts. The card is a standard MCP Apps resource: no
  `text/html+skybridge` mime, no `openai/outputTemplate` and no
  `openai/widgetAccessible`. The card polls `get_verification_widget`, which
  carries the same app-only meta as its two siblings.
- A connection whose manifest names a card URI that is no longer served needs
  the connector re-added. A read of such a URI answers a clean JSON-RPC
  not-found and logs the URI asked for; both halves are pinned by tests.
- `ui/update-model-context` is treated as ABSENT for ChatGPT whatever it
  declares. The card says the result in the chat instead, once per completed
  check, coalesced per card, never on a re-mount. Which mechanism a card uses is
  the SERVER's answer (`_card.deliver` on the three card-only tools), never the
  card's guess: ChatGPT declares the capability it drops, and a dropped push
  still returns success, so nothing in the card could tell.
- The chat message carries no page text at all — not even `key_finding`. A
  `ui/message` arrives as the USER's turn, the most trusted position in a
  conversation; the context snapshot may quote sources behind an
  untrusted-evidence header, a user turn may not.
- "Ask a follow-up" is not offered on ChatGPT: it prefills half a sentence for
  the user to finish, and a host that sends immediately would post the fragment.
- The deep-check wait for `openai-mcp` is 100 s (measured cut 119.8 s, and the
  whole call must land under ~110 s).

### Writing beside a card: what is measured

- The model cannot know whether a card actually rendered. Same tool, same
  result: one chat drew the card and one showed text only, and nothing told the
  model which.
- Beside a card that did render, the model was already brief without being told
  to be (section 10 above).

So guidance about what to write must be safe when no card renders. A text
result that drops the reasoning leaves the user with a bare verdict, which is
worse than a card and text that repeat each other.

### Open questions for ChatGPT

| Open question | What is established | Fallback |
|---|---|---|
| **Are app-only tools hidden from the MODEL in ChatGPT?** `ui.visibility: ['app']` is the standard hint and Claude honours it; ChatGPT declares `experimental["openai/visibility"]`, so it may want its own key. Unmeasured. Check in a live click-through: ask ChatGPT to list the Lenz tools it can call — `start_verification_widget`, `get_verification_widget` and `select_claims_widget` should not appear. | The card's own `tools/call` works without any `openai/` key (measured), so the BUTTONS are fine either way. Only the hiding is in question. | Add the OpenAI visibility key back for that host only. A visible-but-harmless card tool is a much better failure than dead buttons, which is why `openai/widgetAccessible` is not declared defensively. The start tool also refuses when the flag is off, and the API enforces credits and ownership whoever calls it. |
| **Does ChatGPT's CSP label read "on"/enforced, and does the card still work?** Developer mode showed a label reading "CSP off" against a card that declared only `connectDomains` and `resourceDomains`. All four keys SEP-1865 defines are now declared explicitly and empty (`connectDomains`, `resourceDomains`, `frameDomains`, `baseUriDomains`) — empty means no external access, and the card loads nothing external, which a test checks against every published bundle. | The spec's key names and that empty is the secure default (ext-apps 2026-01-26). The card's own inline CSP is already enforced in the stub host and the gallery with no console errors, so the bundle genuinely needs nothing. What the HOST reads and labels is not established. | If ChatGPT reads only `openai/widgetCSP`, that goes in the adapter's per-host resource meta, never in the shared card. |
| ChatGPT sometimes renders TEXT ONLY for a card tool. The first run showed no `resources/read` after the call; a later run read the card a second after it. Cause unknown: model or mode, chat state (earlier calls and errors in the same chat), first use of a just-added connector, or time since connect. Untested: vary one at a time to find it. | Only the symptom, and that the same tool and card render in another chat. | Unchanged, and this is why the rule exists: the tool's TEXT result must stand on its own. A card is never the only path to an answer. |
