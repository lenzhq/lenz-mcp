"""The coarse "is this OpenAI" check.

Per-client tailoring (what ``tools/list`` and ``resources/list`` answer for a
given client, and the logging of resource reads) lives in
``src/lenz_mcp/middleware.py``.

There is no separate ChatGPT widget. ChatGPT renders the standard MCP Apps card,
measured 2026-09-17 to need no ``openai/`` key to render and none for a card's
own ``tools/call`` either, so ONE card serves both hosts (``mcp_card.py``) and
nothing here emits an OpenAI-namespaced key.

The module holds one function.
``request_is_chatgpt`` is the COARSE vendor check — true for any OpenAI client,
app or API connector — and it has three readers: two WITHHOLD an upgrade link
(``server._quota_exhausted`` and the rate-limit branch of ``server._error_result``)
and one labels a failed resource read in the log (``middleware.py``). Withholding a payment link from the wrong OpenAI
surface is safe; granting anything is not, which is why every gate that gives a
client something reads ``client.client_identity()`` instead.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def request_is_chatgpt() -> bool:
    """True for ANY OpenAI MCP client: the ChatGPT app or its API connector.

    Deliberately COARSE, and the only place that wants to be. Two of its three
    readers suppress an upgrade link (``server._quota_exhausted`` and the
    rate-limit branch of ``server._error_result``; the third only labels a log
    line in ``middleware.py``): OpenAI's app guidelines do not allow steering
    a user to an external payment page, and withholding a
    link is the safe direction for every OpenAI surface, measured or not.

    Anything that GIVES a client something — the card, its tools, the long
    deep-check wait — must use ``mcp_card.request_is_chatgpt_app()`` or
    ``client.client_identity()`` instead. The two OpenAI clients share this
    token and do not share their limits: measured 2026-09-18 the API connector
    cuts a tool call at 59.8 s against the app's 119.8 s, and it lists app-only
    tools to its model where the app hides them. Coarse is right for taking a
    link away and wrong for handing over a tool that spends money.
    """
    try:
        from lenz_mcp.client import client_user_agent

        return 'openai-mcp' in client_user_agent().lower()
    except Exception:  # noqa: BLE001 — best-effort; default to the plain description
        return False
