"""One log line per per-client decision this server makes.

Three decisions depend on who the client is — which tools it is listed, how
its card tells the model, and how long a deep check may wait inside one tool
call — and each is made from a string or a capability the CLIENT controls. When
one of those inputs changes shape, the decision silently moves, and until
2026-09-23 nothing said so: a host changed its User-Agent, three lookups
stopped matching, and the first signal was a person noticing there was no card.

So every decision states itself:

- ``mcp_manifest``, once per answered ``tools/list``: who asked, what it
  declared, whether it got the card and WHY, and the deep-check wait it would
  get. One line per answered `tools/list`.
- ``mcp_card_delivery``, once per card-only tool call.
- ``mcp_verify_wait_exhausted``, when a deep check outlasts the wait — the
  symptom of a wait that is too short, which otherwise reads only as a user
  saying "it keeps telling me it is still running".

They are separate lines, never fields on ``mcp_protocol``, so every filter
already written against that line keeps working.

Everything a client sends is reduced by ``log_token`` before it reaches a line:
a raw value could carry a newline or an ``=`` and forge a field, or a whole
second record a reader could not tell from one of ours.

The logger is ``lenz_mcp.decisions`` and is named in
``observability.INFO_LOGGERS``, because a deployment's floor for app loggers is
WARNING — a module whose success signal is an INFO line and no entry there
produces nothing at all, however carefully it was written.
"""

from __future__ import annotations

import logging

from lenz_mcp.protocol_log import log_token

logger = logging.getLogger(__name__)

#: Swallowed logging failures since the process started. A decision line must
#: never cost a client its answer, so every emitter catches — and a silently
#: broken line still has to be findable.
log_failures = 0


def _emit(message: str) -> None:
    global log_failures
    try:
        logger.info('%s', message)
    except Exception:  # noqa: BLE001 — a measurement never fails a request
        log_failures += 1


def log_manifest(profile, *, card_on: bool, reason: str, wait: float) -> None:
    """What this client was served, and on what grounds.

    ``client`` is the clientInfo NAME, not the User-Agent: one User-Agent can
    cover clients with opposite capabilities (``Claude-User`` is the Claude app
    and Claude Code, and only the first renders cards), so the name is what
    makes a line about "claude-code" legible — and it is the right key for
    anything that wants to report each client once.
    """
    _emit(
        'mcp_manifest '
        f'identity={log_token(profile.identity)} '
        f'client={log_token(profile.client_name)} '
        f'era={log_token(profile.era)} '
        f'declared={log_token(profile.declared)} '
        f'vendor={log_token(profile.vendor_token)} '
        f'card={"on" if card_on else "off"} '
        f'reason={log_token(reason)} '
        f'wait={wait:g}'
    )


def log_card_delivery(deliver: str) -> None:
    """How a card-only tool result told this client's card to reach the model.

    The one SERVER-side fact about the failure where a card shows a sourced
    verdict and the model says no check was run.
    """
    from lenz_mcp import client

    profile = client.client_profile()
    _emit(f'mcp_card_delivery identity={log_token(profile.identity)} deliver={log_token(deliver)}')


def log_wait_exhausted(*, wait: float, tool: str) -> None:
    """A deep check was still running when this client's wait ran out.

    Expected at the ceiling and harmless there — the caller gets a task_id. It
    is evidence when it is CONSTANT for one identity, which says that
    identity's row is missing or too small.
    """
    from lenz_mcp import client

    profile = client.client_profile()
    _emit(f'mcp_verify_wait_exhausted identity={log_token(profile.identity)} wait={wait:g} tool={log_token(tool)}')
