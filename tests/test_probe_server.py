"""The dev probe connector imports and registers its tools (scripts/probe).

A local-only FastMCP server an operator adds to Claude as a custom connector for one
click-through: the remote tool-call timeout (`sleep_probe`) and whether Claude
renders a remote connector's MCP Apps card (`card_probe`, `card_ping`). It ships
nothing to the live server: it is not part of lenz-mcp and nothing imports it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json

import pytest

from lenz_mcp.mcp_card import CARD_DIR
from lenz_mcp.testing import REPO_ROOT

SCRIPT = REPO_ROOT / 'scripts/probe/server.py'
PROBE_TOOLS = {'sleep_probe', 'card_probe', 'card_ping', 'spend_probe', 'card_log', 'card_fixture'}
# Fakes of lenz-mcp's card tools: same names, so they are checked by what they do, not by name.
FAKE_CARD_TOOLS = {'start_verification_widget', 'get_verification_widget'}


@pytest.fixture(scope='module')
def probe():
    spec = importlib.util.spec_from_file_location('mcp_probe_server', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tools_are_registered(probe):
    tools = {t.name: t for t in asyncio.run(probe.mcp.list_tools())}
    assert set(tools) == PROBE_TOOLS | FAKE_CARD_TOOLS
    assert tools['card_probe'].meta == {'ui': {'resourceUri': probe.CARD_URI}}
    # The approval question compares a read-only call with one that is not.
    assert tools['card_ping'].annotations.read_only_hint is True
    assert tools['spend_probe'].annotations.read_only_hint is False
    assert tools['spend_probe'].annotations.destructive_hint is False
    assert tools['card_log'].meta == {'ui': {'visibility': ['app']}}
    assert set(tools['sleep_probe'].input_schema['properties']) == {'seconds', 'progress'}


def test_the_card_is_an_mcp_app_resource(probe):
    resources = asyncio.run(probe.mcp.list_resources())
    card = next(r for r in resources if str(r.uri) == probe.CARD_URI)
    assert probe.CARD_URI.startswith('ui://')
    assert card.mime_type == 'text/html;profile=mcp-app'
    contents = list(asyncio.run(probe.mcp.read_resource(probe.CARD_URI)))
    html = contents[0].content
    for method in (
        'ui/initialize',
        'ui/notifications/initialized',
        'ui/notifications/size-changed',
        'tools/call',
        'ui/message',
        'ui/update-model-context',
        'ui/open-link',
        'ui/request-display-mode',
    ):
        assert method in html, method
    assert 'card_ping' in html
    # The MCP Apps handshake names the app, not an MCP client (ext-apps spec.types.ts).
    assert 'appInfo' in html and 'clientInfo' not in html
    # Claude's style variables are light-dark() values; they follow color-scheme, not the variables alone.
    assert 'style.colorScheme = ctx.theme' in html


def test_sleep_is_capped_and_reports_its_window(probe, monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(probe.asyncio, 'sleep', fake_sleep)
    out = asyncio.run(probe.sleep_probe(999))
    assert out['slept'] == probe.MAX_SLEEP_SECONDS == 300
    assert sum(slept) == 300
    assert out['started_at'] <= out['finished_at']


def test_sleep_probe_survives_a_real_modern_call(probe, monkeypatch):
    """Over the transport, not by calling the function.

    `sleep_probe` is the whole point of this probe — it is what measures a host's
    tool-call timeout — and the direct call above passes `ctx=None`, so it never
    reads the request metadata. On 2.x `_meta` is a TypedDict (a plain dict), and
    EVERY modern call carries a non-empty one, so `meta.progressToken` raised
    AttributeError before the sleep: `sleep_probe(seconds=0)` came back
    `isError: true` and the click-through would have measured nothing.
    """
    import httpx

    from lenz_mcp.wire import modern_meta

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(probe.asyncio, 'sleep', fake_sleep)

    async def _call():
        app = probe.build_app()
        async with probe.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as http:
                return await http.post(
                    '/mcp',
                    json={
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'tools/call',
                        'params': {
                            'name': 'sleep_probe',
                            'arguments': {'seconds': 1},
                            '_meta': modern_meta('probe-test'),
                        },
                    },
                    headers={
                        'accept': 'application/json, text/event-stream',
                        'mcp-protocol-version': '2026-07-28',
                        'mcp-method': 'tools/call',
                        'mcp-name': 'sleep_probe',
                    },
                )

    response = asyncio.run(_call())
    assert response.status_code == 200, response.text
    result = response.json()['result']
    assert result.get('isError') is not True, result
    assert json.loads(result['content'][0]['text'])['slept'] == 1


def test_it_is_not_part_of_the_prod_server():
    from lenz_mcp import server

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert not names & PROBE_TOOLS
    assert 'mcp_probe' not in json.dumps(sorted(names))


def test_the_fixture_card_is_the_dev_bundle_and_its_run_is_canned(probe, monkeypatch):
    tools = {t.name: t for t in asyncio.run(probe.mcp.list_tools())}
    assert tools['card_fixture'].meta == {'ui': {'resourceUri': probe.FIXTURE_CARD_URI}}
    for name in FAKE_CARD_TOOLS:
        assert tools[name].meta == {'ui': {'visibility': ['app']}}
    html = list(asyncio.run(probe.mcp.read_resource(probe.FIXTURE_CARD_URI)))[0].content
    # The version comes from package.json, not a literal: the card's URI is
    # bumped by hand and a hardcoded marker here breaks on the next bump.
    import json

    card_dir = CARD_DIR
    version = json.loads((card_dir / 'package.json').read_text())['cardVersion']
    assert f'lenz-card" content="v{version}-dev"' in html and 'Dev fixture' in html

    clock = [1000.0]
    monkeypatch.setattr(probe.time, 'monotonic', lambda: clock[0])
    task_id = probe.fake_start_verification_widget('A claim.')['task_id']
    steps = []
    for _ in range(12):
        out = probe.fake_get_verification_widget(task_id)
        steps.append(out.get('step', out['status']))
        clock[0] += 1.0
    assert steps[0] == 'starting' and 'research' in steps and steps[-1] == 'completed'
    done = probe.fake_get_verification_widget(task_id)
    assert probe.fake_get_verification_widget(done['verification_id']) == done
    assert probe.fake_get_verification_widget('0' * 32)['status'] == 'not_found'


def test_prod_has_no_fixture_card_and_its_card_tools_are_the_real_ones():
    from lenz_mcp import mcp_card, server

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert 'card_fixture' not in tools
    assert server.start_verification_widget.__module__ == 'lenz_mcp.server'
    resources = [str(r.uri) for r in asyncio.run(server.mcp.list_resources())]
    assert not [u for u in resources if 'probe' in u or 'dev' in u]
    for uri in mcp_card.card_versions():
        html = mcp_card.card_html(uri)
        # Neither the switcher nor any fixture reaches the bundle lenz-mcp serves.
        for marker in ('Dev fixture', 'provenance', 'hand-built', 'quick-low', 'deep-28-sources', 'lz-dev'):
            assert marker not in html, marker


@pytest.mark.parametrize(
    ('user_agent', 'deliver'),
    [
        ('openai-mcp/1.0.0', 'message'),
        ('openai-mcp/1.0.0 (Codex)', 'message'),
        ('openai-mcp/1.0.0 (ChatGPT)', 'message'),
        # Same vendor, so the same hint: it is keyed on the token, and an
        # OpenAI card must never be told to push silently.
        ('openai-mcp/1.0.0 (Responses API)', 'message'),
        ('openai-mcp/1.0.0 (Responses API) proxy/1.0', 'message'),
        ('Claude-User', 'context'),
        ('', 'context'),
    ],
)
def test_the_fake_card_tools_stamp_delivery_as_lenz_mcp_does(probe, user_agent, deliver):
    """The probe's card tools answer the way lenz-mcp's do, `_card.deliver`
    included: without it the real card in ChatGPT took the silent route, which
    ChatGPT drops (seen 2026-09-18). Same rule as mcp_card.card_delivery."""
    from lenz_mcp import mcp_card

    reset = probe.bind_user_agent(user_agent)
    try:
        started = probe.fake_start_verification_widget('The claim.')
        polled = probe.fake_get_verification_widget(started['task_id'])
        missing = probe.fake_get_verification_widget('f' * 32)
    finally:
        reset()
    for result in (started, polled, missing):
        assert result[mcp_card.CARD_RESULT_NAMESPACE] == {'deliver': deliver}, result
    assert probe.MESSAGE_DELIVERY_VENDOR_TOKENS == mcp_card.MESSAGE_DELIVERY_VENDOR_TOKENS


def test_the_delivery_stamp_reads_the_caller_over_the_transport(probe):
    """The direct calls above bind the User-Agent by hand. Over the transport
    the tool must read it from its own request, and the schema the host sees
    must not grow a `ctx` argument."""
    import httpx

    from lenz_mcp import mcp_card
    from lenz_mcp.wire import modern_meta

    tools = {t.name: t for t in asyncio.run(probe.mcp.list_tools())}
    assert set(tools['start_verification_widget'].input_schema['properties']) == {'claim', 'retry_of'}
    assert set(tools['get_verification_widget'].input_schema['properties']) == {'task_id'}

    async def _call(user_agent):
        app = probe.build_app()
        async with probe.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as http:
                return await http.post(
                    '/mcp',
                    json={
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'tools/call',
                        'params': {
                            'name': 'start_verification_widget',
                            'arguments': {'claim': 'The claim.'},
                            '_meta': modern_meta('probe-test'),
                        },
                    },
                    headers={
                        'accept': 'application/json, text/event-stream',
                        'mcp-protocol-version': '2026-07-28',
                        'mcp-method': 'tools/call',
                        'mcp-name': 'start_verification_widget',
                        'user-agent': user_agent,
                    },
                )

    for user_agent, deliver in (('openai-mcp/1.0.0', 'message'), ('Claude-User', 'context')):
        response = asyncio.run(_call(user_agent))
        assert response.status_code == 200, response.text
        result = response.json()['result']
        payload = result.get('structuredContent') or json.loads(result['content'][0]['text'])
        assert payload[mcp_card.CARD_RESULT_NAMESPACE] == {'deliver': deliver}, (user_agent, payload)
