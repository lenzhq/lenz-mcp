"""Per-client tailoring and request identity, as one SDK middleware.

The mcp SDK 2.x has no handler dict to wrap (registration is by method string)
and no `RootModel` unions to unpack, so per-client tailoring cannot swap request
handlers. `MCPServer(middleware=[...])` is the public mechanism: one async function
around every inbound message, given the raw method and the already-serialized wire
dict.

Three things happen here, and all three are per request:

- **the client profile is bound** (`bind_client_profile`), because 2.x has no
  `request_ctx` contextvar to read it from. This is the ONE place a request is
  read to decide who the client is; every per-client decision is then a pure
  function of the profile (`client.ClientProfile`). Losing it is not cosmetic:
  the deep-check wait, the card gate and the card's delivery hint all read it,
  and a silent blank would put every client on the short wait with no card and
  no client recorded by the API;
- **`tools/list` is tailored** to what this client may see, and the decision is
  logged (`mcp_manifest`);
- **`resources/list` is filtered**, and `resources/read` is logged.

Two rules, both learned the hard way:

**Tailoring never breaks a call.** Every step is wrapped, and a failure logs and
returns the result untouched. A card is a courtesy; discovery is not.

**A failed step leaves nothing half-done.** Each tailor builds a NEW value and
assigns it only when complete, so an exception midway cannot leave one client's
manifest carrying half of another's rules.

The middleware list is marked provisional upstream ("observe and refuse; do not
make it the foundation your server stands on"), which is why the dependency pins
`mcp<2.3` and why the dual-era tests assert the wire output per client in BOTH
protocol eras rather than asserting anything about this module.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from lenz_mcp.protocol_log import log_token

logger = logging.getLogger(__name__)


async def lenz_middleware(ctx, call_next):
    """Bind the caller's profile, then tailor what it is served."""
    reset = bind_client_profile(ctx)
    try:
        # `request_id is None` is the SDK's own marker for a NOTIFICATION, and the
        # chain wraps notifications too. Nothing below applies to one: `call_next`
        # returns None, so a notification named `tools/list` would reach a tailor and
        # be logged as a failure, and one named `prompts/get` would raise into
        # `_on_notify`, which swallows it as a traceback and answers nothing. Both
        # are ERROR-log amplifiers anyone can drive. A notification is also pre-init,
        # and the SDK's own refusal is the right one for it.
        if ctx.request_id is None:
            return await call_next(ctx)
        if ctx.method == 'prompts/get':
            require_prompt_argument(ctx)
        if ctx.method == 'resources/read':
            return await _logged_resource_read(ctx, call_next)
        result = await call_next(ctx)
        if ctx.method == 'tools/list':
            return log_manifest_decision(tailor_tool_list(result))
        if ctx.method == 'resources/list':
            return tailor_resource_list(result)
        return result
    finally:
        if reset is not None:
            reset()


def bind_client_profile(ctx):
    """Resolve this request's client into one profile `lenz_mcp.client` serves.

    `ctx.request` is the Starlette request in both protocol eras (verified against
    2.2.0 on the legacy `initialize` path and the 2026 header path). A request that
    reaches middleware without one is not something we can serve a client-specific
    answer for, so it is bound as unknown and reported: see
    `client.note_identity_unresolved`.
    """
    from lenz_mcp import client

    request = getattr(ctx, 'request', None)
    if request is None:
        client.note_identity_unresolved('no request on the middleware context')
        user_agent = ''
    else:
        user_agent = request.headers.get('user-agent', '')
    declares_apps, source = _reads_the_apps_declaration(ctx)
    return client.bind_client_profile(
        client.ClientProfile.from_user_agent(
            user_agent,
            declares_apps=declares_apps,
            declaration_source=source,
            era=_era(ctx),
            client_name=_client_name(ctx),
        )
    )


def _client_name(ctx) -> str:
    """The clientInfo NAME this request carried, or ''. Never raises."""
    try:
        session = getattr(ctx, 'session', None)
        info = getattr(getattr(session, 'client_params', None), 'client_info', None)
        return str(getattr(info, 'name', '') or '')
    except Exception:  # noqa: BLE001 — a name for a log line is never worth a request
        return ''


def _era(ctx) -> str:
    """Which protocol era this request speaks, from its negotiated version."""
    from lenz_mcp import client

    version = getattr(ctx, 'protocol_version', '') or ''
    return client.ERA_MODERN if version >= client._FIRST_MODERN_VERSION else client.ERA_LEGACY


def _reads_the_apps_declaration(ctx) -> tuple[bool | None, str]:
    """Whether this request DECLARES MCP Apps support — the card's key.

    Three answers, because the middle one is ordinary and the last one is a bug:

    - a declaration on this request: the SDK's own predicate, which is the
      definition of the thing (`mcp.server.apps.client_supports_apps`: the
      extension declared AND `text/html;profile=mcp-app` among its
      `mimeTypes`). Not reimplemented here — a second copy of somebody else's
      protocol rule drifts from it;
    - NO declaration, which is not a failure: the 2026-07-28 revision puts the
      client's capabilities in every request's `_meta`, but the 2025 era
      declares them once at the handshake, and this server is
      `stateless_http=True` (`asgi.py`), so nothing survives to the next
      request. Measured against mcp 2.2.0: on the legacy `initialize` itself
      too, since the connection records the handshake's capabilities after
      middleware has already run. So on the legacy era this is EVERY request;
    - the read raised, which is ours to fix and is logged at ERROR.

    The SDK predicate answers False for "declared nothing", so the `None` case
    is told apart here, before asking it — otherwise every legacy request would
    read as a client that positively does not support cards, and the whole
    2025 era would lose the card in silence.
    """
    from lenz_mcp import client

    try:
        session = getattr(ctx, 'session', None)
        if session is None or getattr(session, 'client_capabilities', None) is None:
            return None, client.DECLARATION_ABSENT
        from mcp.server.apps import client_supports_apps

        return bool(client_supports_apps(ctx)), client.DECLARATION_FROM_REQUEST
    except Exception:  # noqa: BLE001 — a card is a courtesy, never a reason to break a request
        logger.exception('the client MCP Apps declaration could not be read; falling back to the vendor token')
        return None, client.DECLARATION_READ_FAILED


def tailor_tool_list(result: Any) -> Any:
    """What this client may see in `tools/list`.

    A client we serve the card to gets the card meta on the card tools and keeps
    the card-only tools, marked app-only. Everyone else has the card-only tools
    STRIPPED — `ui.visibility: ['app']` is a client-honored hint, not server
    enforcement, and OpenAI's Responses-API connector ignores it and lists every
    tool to its model. Two of those tools start paid checks.

    So this fails CLOSED. "Tailoring never breaks a call" still holds — a failure
    never raises — but returning the list untouched is not the harmless outcome it
    looks like: it is precisely the exposure the stripping exists to prevent. If
    anything goes wrong after we know the card-only names, they come out anyway.
    """
    try:
        from lenz_mcp import mcp_card

        tools = result.get('tools')
        if not isinstance(tools, list):
            return result
        card_only = mcp_card.CARD_ONLY_TOOL_NAMES
    except Exception:  # never break tool discovery over a per-client tweak
        logger.exception('per-client tool-list tailoring failed before it could read the card names')
        return result

    try:
        if mcp_card.card_active():
            tailored = []
            for tool in tools:
                tool = copy.deepcopy(tool)
                if tool.get('name') in mcp_card.CARD_TOOL_NAMES:
                    # deepcopy, not dict(): a shallow copy hands the response a
                    # reference to the constant's own nested dict.
                    tool['_meta'] = copy.deepcopy(mcp_card.CARD_TOOL_META)
                elif tool.get('name') in card_only:
                    tool['_meta'] = copy.deepcopy(mcp_card.CARD_ONLY_TOOL_META)
                tailored.append(tool)
        else:
            tailored = [copy.deepcopy(t) for t in tools if t.get('name') not in card_only]
        result['tools'] = tailored
    except Exception:
        logger.exception('per-client tool-list tailoring failed; stripping the card-only tools')
        try:
            result['tools'] = [t for t in tools if not (isinstance(t, dict) and t.get('name') in card_only)]
        except Exception:
            logger.exception('the card-only tools could not be stripped either')
    return result


def log_manifest_decision(result: Any) -> Any:
    """State what this client was just served, and why. Returns `result` untouched.

    After tailoring, not before: the line must describe the manifest that went
    out. Wrapped like every other step here — a decision that cannot be logged
    is still a decision, and discovery is not a courtesy.
    """
    try:
        from lenz_mcp import client, config, decisions, mcp_card

        profile = client.client_profile()
        card_on, reason = mcp_card.card_decision(profile)
        decisions.log_manifest(
            profile,
            card_on=card_on,
            reason=reason,
            wait=config.verify_wait_seconds(profile.identity),
        )
    except Exception:  # noqa: BLE001 — never break discovery over a log line
        logger.exception('the manifest decision could not be logged')
    return result


def tailor_resource_list(result: Any) -> Any:
    """Never list a card resource, to any client.

    A card is resolved by URI from the tool's `_meta.ui.resourceUri`, so it never
    needs to be browsable. A listed one can be offered by Claude Desktop under
    "+ → Connectors → Add from Lenz", where picking it pastes the raw HTML bundle
    into the chat. Reads stay served.
    """
    try:
        from lenz_mcp import mcp_card

        resources = result.get('resources')
        if not isinstance(resources, list):
            return result
        result['resources'] = [r for r in resources if not mcp_card.is_card_uri(str(r.get('uri', '')))]
    except Exception:  # never break resource discovery over a per-client tweak
        logger.exception('per-client resource-list tailoring failed')
    return result


async def _logged_resource_read(ctx, call_next):
    """Log every resource read, and WARN loudly when the URI is unknown.

    An unknown `ui://` read is otherwise invisible from our side, so a stale card
    URI in a published manifest would go unnoticed while every dashboard stays
    green: the client simply never renders anything. The URI we
    are ASKED for is the only signal that a published manifest has drifted from what
    this server registers, so it belongs in the log line — against EVERY URI we have
    published, not just the current one: "a URI we retired two versions ago" and "a
    URI we never served" are different alarms.

    The URI is client-controlled and is read here BEFORE the SDK validates it, so it
    goes through `log_token` like everything else a client sends. Raw, a newline in
    it would write a second log line that no reader could tell from one of ours.
    """
    uri = log_token(_requested_uri(ctx))
    try:
        result = await call_next(ctx)
    except Exception:
        from lenz_mcp import mcp_card, widget_resource

        logger.warning(
            'mcp_resource_read_failed uri=%s registered=%s chatgpt=%s',
            uri,
            ','.join(mcp_card.card_versions() or [mcp_card.CARD_URI]),
            widget_resource.request_is_chatgpt(),
        )
        raise
    logger.info('mcp_resource_read uri=%s', uri)
    return result


def require_prompt_argument(ctx) -> None:
    """Answer a prompt with its argument MISSING the way an empty one is answered.

    The prompts raise `MCPError` for an empty or whitespace argument, so the person
    reads "Paste the text to check." But an argument omitted entirely never reaches
    the prompt function: the SDK validates required arguments first and raises its
    own `ValueError('Missing required arguments: ...')`, which 2.x answers with a
    generic message and logs as an ERROR traceback — a Sentry event every time
    somebody opens the prompt and submits it blank.

    Both shapes are the same mistake, so both get the same sentence. Checked here
    rather than by giving the argument a default, because a default would make it
    OPTIONAL in `prompts/list`, and Claude shows that list to a person.

    Every shape is checked before it is used, because this runs BEFORE the SDK
    validates the params: a request whose `arguments` is a list, or whose `name` is
    an object, would otherwise raise AttributeError or TypeError here and be
    answered `-32603 Internal server error` with an ERROR traceback — worse than the
    `-32602` the SDK gives it, and the exact Sentry noise this function exists to
    prevent. A malformed request is not ours to answer: it is left to the SDK.
    """
    from mcp.shared.exceptions import MCPError
    from mcp_types import INVALID_PARAMS

    from lenz_mcp import server

    params = ctx.params if isinstance(ctx.params, dict) else {}
    name = params.get('name')
    prompt = server.REQUIRED_PROMPT_ARGUMENTS.get(name) if isinstance(name, str) else None
    if prompt is None:
        return
    argument, noun = prompt
    arguments = params.get('arguments')
    if arguments is not None and not isinstance(arguments, dict):
        return  # malformed: the SDK's own validation answers it
    if not str((arguments or {}).get(argument, '')).strip():
        raise MCPError(INVALID_PARAMS, f'Paste the {noun} to check.')


def _requested_uri(ctx) -> str:
    params = getattr(ctx, 'params', None)
    if isinstance(params, dict):
        return str(params.get('uri', '-'))
    return str(getattr(params, 'uri', '-'))
