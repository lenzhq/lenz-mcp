# Dev probe results

- Date and time:
- Claude surface(s): Desktop version ___ / claude.ai in ___ (browser) / mobile
- Connector removed and re-added before this run? yes / no
- Tunnel: ngrok / cloudflared / other

## What Claude declared (from the log's `initialize` line)

- protocolVersion:
- clientInfo (name, version):
- capabilities (paste):
- user-agent:

## Timeout probe (run 1)

| Seconds | Returned? | What Claude showed if not (exact text) | Log: `client.gave_up` after (s) |
|---|---|---|---|
| 30 | | | |
| 50 | | | |
| 70 | | | |
| 90 | | | |
| 120 | | | |
| 180 | | | |
| first failing N, progress true: ___ | | | |

## Card (run 1)

| | Claude Desktop | claude.ai |
|---|---|---|
| Card rendered, or text only? | | |
| Host / Theme / Style variables / Container lines | | |
| Theme line changed on light/dark switch? | | |
| Height right (not cut off, not padded)? | | |

## Card buttons (run 2)

Card title shows "(v2)"? yes / no
Capabilities line (paste verbatim):

| # | Button | Approval prompt? (exact text) | What happened in Claude | Card's reply line (verbatim) |
|---|---|---|---|---|
| 1 | Ping (card_ping, read-only) | | | |
| 2 | spend_probe directly (not read-only) | | | |
| 3 | ui/update-model-context (secret word) | | | |
| 4 | ui/message (ask Claude to run a check) | | | |
| 5 | ui/open-link lenz.io | | | |
| 6 | fullscreen | | | |
| 7 | back inline | | | |

Follow-ups:

- `What did the spend_probe tool just return?` Claude's answer: ___ . Card's
  `result_token`: ___ . Same token? yes / no
- `What is the Lenz probe card's secret word?` Claude's answer: ___ . Card's word:
  ___ . Same? yes / no
- After button 4: did the message show as a user message? Did Claude call
  `spend_probe` (terminal `spend_probe.called` with note `from the card button`)?
- Did the terminal show `card.*` lines (card_log reaching the server)? yes / no

## Anything else

(errors, prompts Claude showed, screenshots)
