"""What the MODEL sees, per client — committed, so a wording change is a diff.

One golden file per (client, card flag) holds the server `instructions` and,
for every tool that client is offered, the fields a model reads to decide what
to call: name, title, description, input and output schema, annotations, and
the KEY PATHS of its `_meta` (never the values). Plus the prompts a host shows
a person, and the card resource.

**Why this exists.** Model-visible wording is the most consequential text in
the connector and the least visible in review. A change to it can:
  - move which tool a model picks, so the tool-choice eval must be re-run;
  - change what a connector directory has on file for the listing.
A diff in `tests/goldens/` is the one place a reviewer cannot miss it.

**Built from the wire**, through `src/lenz_mcp/testing.py`, never from the
server's in-process objects — per-client tailoring happens in the request
path, so an in-process read would describe a manifest no client receives.

**Captured in the 2025 era**, and that matters for the card. Who is served one
is keyed on what the client DECLARES, and only the 2026-07-28 revision carries
a declaration per request: the 2025 handshake declares once and this server is
stateless, so nothing survives to the `tools/list` that follows. On the legacy
era the client's vendor TOKEN decides instead, which is why
`claude-code__card-on.json` — a client whose `declares_mcp_apps` is false —
still shows the card-only tools. On the modern era it does not, and that is
the one place the two eras differ on purpose (`ERA_DIVERGENT`). Everything
else is asserted EQUAL across the eras after normalisation rather than stored,
so a golden file changes only when wording does.

Regenerate after an intended change:

    UPDATE_MCP_GOLDENS=1 uv run pytest tests/test_model_view.py --no-cov
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from lenz_mcp.mcp_card import CARD_ONLY_TOOL_NAMES, CARD_URI
from lenz_mcp.testing import DECLARES_APPS, DECLARES_NO_APPS, MODERN, assembled_app

GOLDEN_DIR = Path(__file__).parent / 'goldens'
UPDATE = os.environ.get('UPDATE_MCP_GOLDENS') == '1'

# Every client whose view is committed, as (User-Agent, what it DECLARES).
#
# Two axes, because two different things decide what a client is served. The
# User-Agent carries the identity the deep-check wait keys on, where a suffix
# is load-bearing: the ChatGPT app and OpenAI's Responses-API connector both
# send `openai-mcp` and cut a tool call at 119.8 s and 59.8 s. The declaration
# is what the CARD keys on, and it is the reason one User-Agent can need two
# rows — `Claude-User` is the Claude app, which declares MCP Apps, and Claude
# Code, which does not.
#
# Every measured identity in `client.KNOWN_IDENTITIES` must appear here, plus
# the cases that are deliberately NOT in it. Pinned by
# `test_every_measured_identity_has_a_committed_view`, so an identity cannot be
# added to the registry and left with no committed view of what its model sees
# — which is the half-fix this whole change exists to make impossible.
CLIENTS: dict[str, tuple[str, dict | None]] = {
    'claude': ('Claude-User', DECLARES_APPS),
    # The same User-Agent, the opposite capabilities. Under the identity table
    # this client was served three card-only tools it cannot render.
    'claude-code': ('Claude-User', DECLARES_NO_APPS),
    'chatgpt': ('openai-mcp/1.0.0', DECLARES_APPS),
    # The suffix OpenAI began sending on 2026-09-23, which matched no row.
    'chatgpt-app': ('openai-mcp/1.0.0 (ChatGPT)', DECLARES_APPS),
    'chatgpt-codex': ('openai-mcp/1.0.0 (Codex)', DECLARES_APPS),
    # Declares the extension and renders nothing: served the card, knowingly.
    'openai-responses-api': ('openai-mcp/1.0.0 (Responses API)', DECLARES_APPS),
    # An unmeasured suffix is no longer refused the card — it is asked.
    'unknown-suffix': ('openai-mcp/1.0.0 (Something New)', DECLARES_APPS),
    # No User-Agent and no declaration: privileged nowhere.
    'no-user-agent': ('', None),
}
FLAGS = (False, True)
CASES = [(client, flag) for client in CLIENTS for flag in FLAGS]

# A client whose User-Agent is a card vendor's but which declares nothing gets
# DIFFERENT answers in the two eras, by design: the modern era reads its
# declaration and closes the card, the legacy era has none to read and falls
# back to the vendor token. Claude Code is the case. Everything else must still
# be identical across the eras, which is what the equality test is for.
ERA_DIVERGENT = {'claude-code'}

# What the 2026-07-28 revision adds to every result and the 2025 one lacks.
# Envelope, not wording: stripped before the two eras are compared. Named here,
# beside the golden, so a reader can tell "the 2026 envelope adds this" from
# "someone edited a description" without reading the migration.
ERA_ONLY_KEYS = frozenset(
    {
        'resultType',  # every modern result declares its own kind
        'ttlMs',  # modern cache hint
        'cacheScope',  # modern cache hint
        '_meta',  # carries io.modelcontextprotocol/serverInfo on every modern result
    }
)
TOOL_FIELDS = ('name', 'title', 'description', 'inputSchema', 'outputSchema', 'annotations')


def _golden_path(client: str, card: bool) -> Path:
    return GOLDEN_DIR / f'{client}__card-{"on" if card else "off"}.json'


def _meta_key_paths(meta: Any, prefix: str = '') -> list[str]:
    """Dotted key paths with each leaf's TYPE, values discarded.

    Values never reach a committed file: a client's request `_meta` carries the
    user's city, region, timezone and coordinates, and while a tool's declared
    `_meta` is ours, the rule is kept absolute so nobody has to judge which
    `_meta` they are looking at.

    The type is kept because a path alone conflated `{}`, `[]`, `null` and a
    string: `ui.visibility` becoming a string instead of a list
    changes what a host does with it and must show as a diff.
    """
    if isinstance(meta, dict):
        if not meta:
            return [f'{prefix}:object'] if prefix else []
        paths: list[str] = []
        for key in sorted(meta):
            paths.extend(_meta_key_paths(meta[key], f'{prefix}.{key}' if prefix else key))
        return paths
    kind = 'list' if isinstance(meta, list) else 'null' if meta is None else type(meta).__name__
    return [f'{prefix}:{kind}']


def _model_visible(tool: dict[str, Any]) -> bool:
    """Whether the host shows this tool to the MODEL at all.

    MCP Apps' `ui.visibility` decides it: `['app']` is card-only, hidden from the
    model, and no visibility at all is the default, visible to both. This is the
    one `_meta` fact that decides what a model can call, so it is recorded as the
    consequence rather than as a copied value — which keeps the no-values rule
    whole and still turns a flip from `['app']` to `['model']` into a diff. The
    key paths alone could not: both are `ui.visibility:list`.
    """
    visibility = ((tool.get('_meta') or {}).get('ui') or {}).get('visibility')
    return True if visibility is None else 'model' in visibility


def _tool_view(tool: dict[str, Any]) -> dict[str, Any]:
    view = {field: tool[field] for field in TOOL_FIELDS if field in tool}
    view['meta_keys'] = _meta_key_paths(tool.get('_meta') or {})
    view['model_visible'] = _model_visible(tool)
    return view


def _prompt_view(prompt: dict[str, Any]) -> dict[str, Any]:
    # Claude shows a prompt's title to a PERSON in its menu, and its arguments'
    # descriptions too — so they are as review-sensitive as a tool description.
    # Without `arguments` here, an argument's wording could change with no
    # diff at all.
    view = {field: prompt[field] for field in ('name', 'title', 'description') if field in prompt}
    view['arguments'] = sorted(
        (
            {field: argument[field] for field in ('name', 'description', 'required') if field in argument}
            for argument in prompt.get('arguments') or []
        ),
        key=lambda argument: argument.get('name', ''),
    )
    return view


def _card_resource(wire, *, era: str | None = None) -> dict[str, Any] | None:
    """The card resource, read by URI.

    NOT from `resources/list`: the card URIs are deliberately unlisted (a listed
    resource shows up in Claude's "+" menu and pastes raw HTML), so a golden
    built from the list alone would record "no card" and never notice one
    appearing.
    """
    kwargs = {'era': era, 'name': CARD_URI} if era else {}
    response = wire.rpc('resources/read', {'uri': CARD_URI}, **kwargs)
    message = wire.reply(response)
    if 'error' in message:
        return None
    contents = (message.get('result') or {}).get('contents') or []
    return {'uri': CARD_URI, 'mimeTypes': sorted({c.get('mimeType', '') for c in contents})}


def _capture(
    user_agent: str,
    card: bool,
    *,
    era: str | None = None,
    raw: dict | None = None,
    capabilities: dict | None = None,
) -> dict[str, Any]:
    """What this client is served. `capabilities` is what it DECLARES.

    Only the 2026-07-28 era carries a declaration — the 2025 handshake declares
    once and this server is stateless — so the legacy capture ignores it and
    falls back to the vendor token, exactly as production does.
    """
    with assembled_app(MCP_OAUTH_ENABLED=False, MCP_CARD_ENABLED=card) as harness:
        wire = harness.wire(user_agent=user_agent, capabilities=capabilities)
        if era == MODERN:
            # No handshake in the 2026 era: the instructions come from discovery.
            instructions = wire.call('server/discover', era=MODERN).get('instructions')
            tools = wire.call('tools/list', era=MODERN, name='tools/list')
            prompts = wire.call('prompts/list', era=MODERN, name='prompts/list')
        else:
            instructions = wire.initialize().get('instructions')
            tools = wire.call('tools/list')
            prompts = wire.call('prompts/list')
        card_resource = _card_resource(wire, era=era)
    if raw is not None:
        raw['tools/list'] = tools

    # The 2026 envelope (`ERA_ONLY_KEYS`) sits BESIDE `tools` and `prompts` in
    # each result, and only those two lists are read — so it never reaches the
    # view. `test_the_modern_envelope_really_is_there_and_really_is_dropped`
    # checks both halves of that rather than trusting the field selection.
    return {
        'instructions': instructions,
        'tools': sorted((_tool_view(t) for t in tools.get('tools', [])), key=lambda t: t['name']),
        'prompts': sorted((_prompt_view(p) for p in prompts.get('prompts', [])), key=lambda p: p['name']),
        'card_resource': card_resource,
    }


def _capture_client(name: str, card: bool, **kwargs) -> dict[str, Any]:
    """`_capture` for a named row of CLIENTS, declaration included."""
    user_agent, capabilities = CLIENTS[name]
    return _capture(user_agent, card, capabilities=capabilities, **kwargs)


def _record(client: str, card: bool, view: dict[str, Any]) -> dict[str, Any]:
    user_agent, capabilities = CLIENTS[client]
    return {
        'client': client,
        'user_agent': user_agent,
        # What this client DECLARES, which is what the card is keyed on.
        # Committed beside the view so a golden says WHY it looks like this.
        'declares_mcp_apps': capabilities == DECLARES_APPS,
        'card_enabled': card,
        **view,
    }


# ── the goldens ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(('client', 'card'), CASES)
def test_the_model_sees_what_the_golden_says(client, card):
    view = _record(client, card, _capture_client(client, card))
    path = _golden_path(client, card)

    if UPDATE:
        GOLDEN_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps(view, indent=2, sort_keys=True, ensure_ascii=False) + '\n')
        return

    assert path.exists(), f'no golden for {client} (card {"on" if card else "off"}); run with UPDATE_MCP_GOLDENS=1'
    golden = json.loads(path.read_text())
    assert view == golden, (
        f'\nWHAT THE MODEL SEES CHANGED for {client} (card {"on" if card else "off"}).\n'
        'This is not a snapshot to refresh by reflex. What the model reads decides\n'
        'what it calls, so before regenerating:\n'
        '  - re-run the tool-choice eval against the new wording;\n'
        '  - expect the directory listings to need updating if a description changed.\n'
        'If the change is intended: UPDATE_MCP_GOLDENS=1 uv run pytest '
        'tests/test_model_view.py --no-cov, and let the diff be reviewed.\n'
    )


@pytest.mark.parametrize(('client', 'card'), CASES)
def test_both_protocol_eras_give_the_model_the_same_view(client, card):
    legacy_raw: dict = {}
    modern_raw: dict = {}
    legacy = _capture_client(client, card, raw=legacy_raw)
    modern = _capture_client(client, card, era=MODERN, raw=modern_raw)
    # Proved on the very responses being compared, not on a separate request:
    # an equality between two captures that were secretly both legacy would
    # pass for the wrong reason. `resultType` is the 2026 discriminator.
    assert 'resultType' in modern_raw['tools/list'], 'the "modern" capture was not a 2026-era response'
    assert 'resultType' not in legacy_raw['tools/list'], 'the "legacy" capture carried a 2026 envelope'

    if client in ERA_DIVERGENT and card:
        # The one divergence the design accepts, asserted in BOTH directions so
        # it cannot quietly become an equality again. `Claude-User` that
        # declares no MCP Apps support is Claude Code: the modern era reads
        # that declaration and closes the card; the legacy era has none to
        # read, so the vendor token decides and it keeps today's answer. Claude
        # Code speaks the modern era, so the closed one is what it gets and
        # the legacy leg is only a compatibility floor.
        modern_tools = {t['name'] for t in modern['tools']}
        legacy_tools = {t['name'] for t in legacy['tools']}
        assert not (CARD_ONLY_TOOL_NAMES & modern_tools), 'a declared no must close the card on the modern era'
        assert CARD_ONLY_TOOL_NAMES <= legacy_tools, 'the legacy era has no declaration and keeps the fallback'
        assert modern != legacy, 'this row is listed as era-divergent but the two eras agree'
        return

    assert modern == legacy, (
        f'\nThe 2026-07-28 manifest differs from the 2025 one for {client} '
        f'(card {"on" if card else "off"}), AFTER the envelope was stripped.\n'
        'Do NOT normalise this away. The tool payloads are supposed to be identical\n'
        'across protocol eras, and test_dual_era asserts they are, so a\n'
        'difference here is evidence about the MCP SDK, not about this file.\n'
        f'If it is a per-client DECISION that legitimately differs by era, add {client!r}\n'
        'to ERA_DIVERGENT with the reason, rather than relaxing this assertion.\n'
    )


def test_every_measured_identity_has_a_committed_view():
    """A registry entry with no golden is an identity nobody can review.

    The registry is the one place an identity is spelled, and every table that
    keys on one is pinned to it. This is that pin for the goldens: add
    `openai-mcp (Brand New Surface)` to the registry and a wait row beside it,
    and without this the whole suite stays green while no file records what
    that client's model would see.
    """
    from lenz_mcp import client

    committed = {client.parse_identity(user_agent) for user_agent, _ in CLIENTS.values()}
    missing = set(client.KNOWN_IDENTITIES) - committed
    assert not missing, (
        f'measured identities with no row in CLIENTS: {sorted(missing)}. '
        'Add one (with the capabilities that client declares) and regenerate: '
        'UPDATE_MCP_GOLDENS=1 uv run pytest tests/test_model_view.py --no-cov'
    )


def test_every_committed_view_has_a_golden_on_disk():
    """The other half: a row in CLIENTS whose file was never generated would
    otherwise only fail on the one parametrized case that reads it."""
    for name in CLIENTS:
        for card in FLAGS:
            path = _golden_path(name, card)
            assert path.exists(), f'no golden for {name} (card {"on" if card else "off"}): {path}'


def test_the_modern_envelope_really_is_there_and_really_is_dropped():
    """The equality test is only meaningful if both of these hold.

    If the 2026 envelope were absent, the eras would compare equal for want of
    anything to differ; if it leaked into the view, they would differ for a
    reason that has nothing to do with wording. Measured, not assumed.
    """
    with assembled_app(MCP_OAUTH_ENABLED=False, MCP_CARD_ENABLED=True) as harness:
        raw = harness.wire(user_agent='Claude-User').call('tools/list', era=MODERN, name='tools/list')
    assert 'resultType' in raw, f'no 2026 envelope on the modern result: {sorted(raw)}'
    view = _capture('Claude-User', True, era=MODERN)
    assert not ERA_ONLY_KEYS & set(view), f'the envelope leaked into the view: {sorted(view)}'


# ── properties that should hold for every client, forever ────────────────────
#
# Asserted as their own tests rather than left to a golden diff: a golden only
# says something CHANGED, and these say what must never be true.


@pytest.mark.parametrize(('client', 'card'), CASES)
def test_every_client_is_offered_the_core_tools(client, card):
    """A floor, not a count, so a card step adding a tool never fights it.

    An empty capture that matched an empty golden would be the same failure as
    a test suite that runs zero tests and reports success.
    """
    view = _capture_client(client, card)
    names = {t['name'] for t in view['tools']}
    assert {'assess_claim', 'check_usage'} <= names, names
    assert view['instructions'], 'the server sent no instructions'


@pytest.mark.parametrize('card', FLAGS)
def test_a_client_that_says_it_renders_no_card_is_never_offered_a_card_only_tool(card):
    """`ui.visibility: ['app']` is a client-honored HINT, not server enforcement.

    OpenAI's Responses-API connector ignores it and lists every tool to its
    model (measured 2026-09-18), so a card-only tool listed to a client that
    renders no card is one the model can see and call with nowhere to put the
    result — and two of the three start paid checks. Stripping server-side is
    what actually withholds them.

    Keyed on what the client SAYS, which is the reversal this release makes: an
    unmeasured suffix is no longer refused the card, it is asked. The Responses
    API connector declares the extension and is now served one, knowingly
    (tests/test_card.py::test_a_declaring_client_that_renders_nothing_is_accepted).
    What must never happen is a client that answered "no" being served one.
    """
    user_agent, capabilities = CLIENTS['claude-code']
    assert capabilities != DECLARES_APPS, 'the test would pass vacuously against a declaring client'
    # The MODERN era: it is the only one that carries a declaration, and it is
    # what Claude Code speaks. The legacy capture of the same client keeps the
    # vendor-token fallback, which is the divergence
    # `test_both_protocol_eras_give_the_model_the_same_view` pins.
    view = _capture(user_agent, card, era=MODERN, capabilities=capabilities)
    offered = {t['name'] for t in view['tools']} & CARD_ONLY_TOOL_NAMES
    assert not offered, f'a client that declares no MCP Apps support was offered: {sorted(offered)}'


@pytest.mark.parametrize(('client', 'card'), CASES)
def test_no_client_is_shown_an_openai_namespaced_meta_key(client, card):
    """No TOOL declares an `openai/`-namespaced `_meta` key, for anyone."""
    view = _capture_client(client, card)
    stray = [(t['name'], k) for t in view['tools'] for k in t['meta_keys'] if k.startswith('openai/')]
    assert not stray, stray


@pytest.mark.parametrize('card', FLAGS)
def test_every_client_is_told_the_same_thing_about_the_tools_it_can_see(card):
    """Per-client tailoring may hide a tool or change its `_meta`. It must never
    change what a tool SAYS, or a case measured on one client stops describing
    another — and the tool-choice eval measures on one client."""
    by_name: dict[str, dict[str, Any]] = {}
    for client in CLIENTS:
        for tool in _capture_client(client, card)['tools']:
            # Everything the model reads, not three hand-picked fields: the first
            # version skipped `outputSchema` and `annotations`, so a client-specific
            # output description could pass here and survive every regeneration
            # too. Only what tailoring may LEGITIMATELY vary is left
            # out — the tool's name, its `_meta` shape and its visibility.
            wording = {k: v for k, v in tool.items() if k not in {'name', 'meta_keys', 'model_visible'}}
            if tool['name'] in by_name:
                assert by_name[tool['name']] == wording, f'{tool["name"]} is worded differently for {client}'
            else:
                by_name[tool['name']] = wording
