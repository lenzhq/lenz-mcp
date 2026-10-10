"""Which mounting a card belongs to, stamped on the result that mounts it.

What the model has been told lives in storage that outlives a conversation, so
a later card for the same claim, in a NEW chat, found the old chat's records and
said nothing about a finished check the new chat's model had never heard of. The
result that MOUNTS a card (``mcp_card.CARD_TOOL_NAMES``) therefore carries two
values under the card's own namespace, and only that result: the card-only tools
service a card that already exists and never mint an identity.

- ``conversation``: a short hash of the host's conversation id, read from the
  tool call's ``_meta``. ChatGPT documents ``openai/session`` as an anonymized
  conversation id; Codex is read by ``threadId`` or ``sessionId``. Claude's app
  sends none, and then the key is ABSENT: the card keeps what it had. Never the
  raw id, and never ``openai/subject`` (that names the user, not the chat).
- ``call_id``: a nonce minted once per mounting result. A host showing the same
  result again shows the same id; a new tool call mints a new one.

The card reads both in ``card/src/logic/identity.js``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Mapping

logger = logging.getLogger(__name__)

CONVERSATION_META_KEYS = ('openai/session', 'threadId', 'sessionId')
_CONVERSATION_ID_MAX = 512


def conversation_hash(meta) -> str:
    """A short, stable hash of the host's conversation id, or ``''``.

    ``meta`` is the tool call's ``_meta``. Only the keys named above are read,
    only a non-empty string is taken, and only the hash leaves this function.
    """
    if not isinstance(meta, Mapping):
        return ''
    for key in CONVERSATION_META_KEYS:
        value = meta.get(key)
        if isinstance(value, str) and value.strip() and len(value) <= _CONVERSATION_ID_MAX:
            return hashlib.sha256(value.strip().encode('utf-8')).hexdigest()[:16]
    return ''


def _request_meta(ctx):
    try:
        return ctx.request_context.meta
    except Exception:  # noqa: BLE001 — no request context is no conversation id
        return None


def with_mounting_identity(result, ctx):
    """Stamp a card-MOUNTING tool's result with its conversation and call id.

    Only for a client that is served the card. Never raises and never changes a
    result it cannot stamp: a card is a courtesy, and this runs after a paid call.
    """
    if not isinstance(result, dict):
        return result
    try:
        from lenz_mcp import mcp_card

        if not mcp_card.card_active():
            return result
        stamp: dict[str, str] = {}
        conversation = conversation_hash(_request_meta(ctx))
        if conversation:
            stamp['conversation'] = conversation
        stamp['call_id'] = secrets.token_hex(8)
        existing = result.get(mcp_card.CARD_RESULT_NAMESPACE)
        result[mcp_card.CARD_RESULT_NAMESPACE] = {**(existing if isinstance(existing, dict) else {}), **stamp}
    except Exception:  # noqa: BLE001
        logger.exception('the card mounting identity could not be stamped')
    return result
