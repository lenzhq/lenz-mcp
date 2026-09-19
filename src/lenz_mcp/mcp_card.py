"""The Lenz verdict card: an MCP Apps resource.

The card is a Preact bundle built in ``src/lenz_mcp/card/`` to a committed
``dist/``, served as ``text/html;profile=mcp-app`` and pointed at by
``assess_claim``'s ``_meta.ui.resourceUri`` in the tools/list a card client
receives. Behind ``MCP_CARD_ENABLED`` (off by default), and only for the
clients the card is MEASURED in — ``Claude-User`` (claude.ai, Claude Desktop,
the directory connector) and the ChatGPT app (``card_active``).

This module owns the card's identity (URI, mime type, versions), the predicate
every card surface gates on, and the resource registration. The per-client
tools/list tailoring lives in ``middleware.py`` (``tailor_tool_list``).

Two rules from measuring Claude (scripts/probe, 2026-09-17):

- **The URI is a published identity, bumped by hand.** Claude caches the card by
  URI, and the tool list (which carries the URI) per connector. A rebuilt
  bundle under the same URI is invisible to users until they re-add the
  connector, and old chats re-mount whatever HTML their URI names. So every
  release gets a new literal ``CARD_URI``, every earlier URI keeps serving its
  own committed bundle (``dist/versions.json``), and a test fails when a bundle
  changes without a new URI.
- **The card is served on read and never listed.** Claude lists resources once
  at connect and offers every listed one under "+ → Add from Lenz", where
  picking it pastes the raw HTML into the chat. The card is read by URI when a
  tool result needs it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

logger = logging.getLogger(__name__)

# Bump BY HAND, with a new dist file and a versions.json entry, whenever the
# bundle changes. Never derived from the bundle's bytes (see the module note).
CARD_URI = 'ui://lenz/card-v4'
CARD_URI_PREFIX = 'ui://lenz/'
CARD_MIME_TYPE = 'text/html;profile=mcp-app'

# The card's directory in this package: sources, fixtures and the served dist/.
CARD_DIR = Path(__file__).resolve().parent / 'card'
DIST_DIR = CARD_DIR / 'dist'
_VERSIONS_PATH = DIST_DIR / 'versions.json'

# The client identities whose card is measured. Identities, not tokens: a
# suffix nobody has measured must not inherit a measured client's card. Claude
# sends no suffix today; if it grows one, that variant gets
# today's manifest until somebody watches the card work in it.
CLAUDE_USER_AGENT_PRODUCT = 'Claude-User'
CLAUDE_IDENTITIES = frozenset({CLAUDE_USER_AGENT_PRODUCT})

# Tools only the card calls: listed to Claude with the card on, hidden from the
# model by the MCP Apps visibility hint, stripped from every other manifest.
CARD_ONLY_TOOL_NAMES = frozenset({'start_verification_widget', 'get_verification_widget', 'select_claims_widget'})
CARD_ONLY_TOOL_META = {'ui': {'visibility': ['app']}}

# ── How the card tells the MODEL what it found ───────────────────────
# Two hosts, two mechanisms, and the card cannot tell which host it is in.
#
# Claude delivers `ui/update-model-context` silently: the model learns the
# result and the user sees nothing. ChatGPT ACCEPTS that call, answers success
# and never delivers it (measured 2026-09-17) — and then tells the user "no full
# verification was run" beside a card showing a sourced verdict. Its one working
# channel is `ui/message`, which it SENDS at once as a user turn.
#
# The card cannot infer which it is in: ChatGPT DECLARES updateModelContext, so
# the capability is no signal, and a dropped push still returns success, so
# there is nothing to detect afterwards. The SERVER knows — the card's own tool
# calls carry the client's User-Agent — so it says so, in the result of a call
# the card already makes. The card obeys it and never guesses.
#
# It rides under `_card`, the card's own namespace, so it can never collide with
# a field a model-facing schema grows later; these three tools are app-only, so
# no model sees it either way.
CARD_RESULT_NAMESPACE = '_card'
DELIVER_BY_MESSAGE = 'message'
DELIVER_BY_CONTEXT = 'context'
# The ChatGPT APP, whose card we have measured — and ONLY it. OpenAI's
# Responses-API connector shares the `openai-mcp` token but renders no card at
# all (it is how a developer wires Lenz into their own agent), and measured
# 2026-09-18 it also ignores `ui.visibility: ['app']` and lists every tool to
# its model. Matching on the token would hand that developer's model three
# card-only tools, two of which START PAID CHECKS. So the match is on the full
# identity, and an unrecognised suffix is NOT the app.
CHATGPT_APP_IDENTITIES = frozenset({'openai-mcp', 'openai-mcp (Codex)'})


def card_delivery() -> str:
    """Which mechanism THIS client's card must use to tell the model.

    Keyed on the same client identity as the deep-check wait
    (``client.client_identity()``, the token plus its suffix). An
    unknown client gets ``context``: the card then pushes only if the host
    declares it, so a host nobody has measured is never made to post messages
    into the user's own turn that it did not ask for.
    """
    try:
        if request_is_chatgpt_app():
            return DELIVER_BY_MESSAGE
    except Exception:  # noqa: BLE001 — the quieter mechanism is the safe default
        return DELIVER_BY_CONTEXT
    return DELIVER_BY_CONTEXT


def with_delivery(result: dict) -> dict:
    """Stamp a card-only tool's result with how this client must deliver."""
    if not isinstance(result, dict):
        return result
    result[CARD_RESULT_NAMESPACE] = {'deliver': card_delivery()}
    return result


# The tools whose results render as the card: the quick check and a deep check
# the model ran itself — `verify_claim` and `get_verification`
# both answer with a completed result or a submitted task_id, and the card reads
# the shape, never the tool name.
CARD_TOOL_NAMES = frozenset({'assess_claim', 'verify_claim', 'get_verification'})
CARD_TOOL_META = {'ui': {'resourceUri': CARD_URI}}

# The card opens no network connection and loads nothing: its script and styles
# are inline and pinned by the HTML's own CSP. It draws its own hairline frame,
# so the host's border is declined.
#
# ALL FOUR CSP keys are declared, explicitly empty. Per SEP-1865
# (`McpUiResourceCsp`, ext-apps specification 2026-01-26) the object defines
# exactly `connectDomains` (fetch/XHR/WebSocket), `resourceDomains` (images,
# scripts, stylesheets, fonts, media), `frameDomains` (nested iframes) and
# `baseUriDomains` (the document's base URI), and "empty or omitted = no
# external connections (secure default)". Empty is therefore the STRONGEST
# posture and also the truthful one: the card talks only to the host.
#
# Empty and omitted mean the same thing to a conforming host, so naming all four
# buys nothing technically — it is for the reader: ChatGPT's developer mode
# showed a "CSP off" label against a card that declared only two of them
# (2026-09-18), and ChatGPT apps are expected to declare their policy. An explicit four-key declaration cannot be read as an omission.
# The card requests no browser `permissions` (no camera, microphone,
# geolocation or clipboard), so that key is absent rather than empty.
CARD_RESOURCE_META = {
    'ui': {
        'csp': {
            'connectDomains': [],
            'resourceDomains': [],
            'frameDomains': [],
            'baseUriDomains': [],
        },
        'prefersBorder': False,
    }
}

_PLACEHOLDER_HTML = (
    '<!doctype html><meta charset="utf-8">'
    '<p style="font-family:system-ui,sans-serif;padding:16px">The Lenz card is not built.</p>'
)


def card_versions() -> dict[str, dict[str, str]]:
    """Every card URI ever published → its committed bundle file and sha256, oldest first."""
    try:
        return json.loads(_VERSIONS_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        logger.warning('claude card versions.json missing or unreadable at %s', _VERSIONS_PATH)
        return {}


def card_html(uri: str) -> str:
    entry = card_versions().get(uri) or {}
    try:
        return (DIST_DIR / entry['file']).read_text(encoding='utf-8')
    except (KeyError, OSError, UnicodeDecodeError):
        logger.warning('claude card bundle missing for %s — serving placeholder', uri)
        return _PLACEHOLDER_HTML


def is_card_uri(uri: str) -> bool:
    return str(uri).startswith(CARD_URI_PREFIX)


def card_enabled() -> bool:
    from lenz_mcp import config

    return bool(config.CARD_ENABLED)


def request_is_claude() -> bool:
    """The request is from a Claude client whose card is measured. Never raises.

    Keyed on the full identity (``client.client_identity``), so an unmeasured
    suffix is not Claude for the card's purposes — the same rule that keeps
    OpenAI's API connector from being mistaken for its app.
    """
    try:
        from lenz_mcp import client

        return client.client_identity() in CLAUDE_IDENTITIES
    except Exception:  # noqa: BLE001 — a card is a courtesy, never a reason to break discovery
        return False


def request_is_chatgpt_app() -> bool:
    """The request is from the ChatGPT app, the client whose card is measured.

    NOT its Responses-API connector, which shares the token (see
    CHATGPT_APP_IDENTITIES). Never raises.
    """
    try:
        from lenz_mcp import client

        return client.client_identity() in CHATGPT_APP_IDENTITIES
    except Exception:  # noqa: BLE001 — a card is a courtesy, never a reason to break discovery
        return False


def card_active() -> bool:
    """The ONE predicate every card surface gates on.

    The flag, and a client we have MEASURED the card in: Claude, and the
    ChatGPT app, which renders the same standard MCP Apps card. Any other
    client gets today's manifest — a card is only offered to a host somebody
    has actually watched it work in.
    """
    return card_enabled() and (request_is_claude() or request_is_chatgpt_app())


def register_card_resources(mcp: MCPServer) -> None:
    """Serve every published card URI with its own committed bundle."""
    for uri in card_versions() or {CARD_URI: {}}:

        def _make(bound_uri: str):
            def _card() -> str:  # pragma: no cover - thin serve-time wrapper
                return card_html(bound_uri)

            return _card

        mcp.resource(
            uri,
            name='Lenz verdict card',
            description='The Lenz verdict card (MCP Apps).',
            mime_type=CARD_MIME_TYPE,
            meta=CARD_RESOURCE_META,
        )(_make(uri))
