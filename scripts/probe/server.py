"""Dev probe connector: what a remote MCP connector gets in Claude (2026-09-17).

NOT lenz-mcp and never deployed. A standalone MCPServer with no auth, bound
to localhost, that you expose through a tunnel and add to Claude as a custom
connector for one click-through. It settles two questions:

1. ``sleep_probe``: the tool-call timeout of a remote connector (the largest N
   that returns, and what Claude shows when one does not). It decides
   ``VERIFY_WAIT_SECONDS`` in the connector's config.py.
2. ``card_probe`` / ``card_ping``: whether Claude renders a remote connector's
   MCP Apps card (``ui://`` resource, ``text/html;profile=mcp-app``,
   ``_meta.ui.resourceUri`` on the tool), whether the card can call a tool back
   through the host bridge, whether it reads the host theme and reports its height.
3. The card's buttons (run 2): `ui/message`, `ui/update-model-context`,
   `ui/open-link`, `ui/request-display-mode`, and a direct call to `spend_probe`,
   which is marked NOT read-only, to see whether Claude asks before a card spends.
   Method names and params follow ext-apps src/spec.types.ts, not the published page.

Every HTTP request is logged to stdout: the JSON-RPC method, the protocol-version
header, and on ``initialize`` the client's declared capabilities and info.

Run: ``uv run python scripts/probe/server.py`` (README.md beside this file).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

HOST = '127.0.0.1'
PORT = int(os.environ.get('MCP_PROBE_PORT', '8765'))
MAX_SLEEP_SECONDS = 300
# Bumped by hand whenever the card changes: Claude caches the card by URI, and the
# tool list (which carries this URI) per connector, until the connector is re-added.
CARD_URI = 'ui://lenz-probe/card-v2'
CARD_MIME = 'text/html;profile=mcp-app'


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec='milliseconds')


def log(event: str, **fields: Any) -> None:
    print(json.dumps({'at': _now(), 'event': event, **fields}, default=str), flush=True)


# Advertise the MCP Apps extension, or not. The open question is whether a host
# renders a card from a server that does NOT advertise it: Claude rendered one from
# this probe back when the SDK could not advertise at all (run 1, 2026-09-17),
# so "advertising is required" has never actually been tested. Run the probe both
# ways against the same host — `MCP_PROBE_ADVERTISE_APPS=1` is the advertising one.
ADVERTISE_APPS = os.environ.get('MCP_PROBE_ADVERTISE_APPS') == '1'
_extensions = []
if ADVERTISE_APPS:
    from mcp.server.apps import Apps

    # Empty: this declares support in `server/discover` and binds no tool or resource,
    # which is exactly the variable under test.
    _extensions.append(Apps())

mcp = MCPServer(
    'lenz-probe',
    instructions='A development probe. Call its tools only when the user asks for them by name.',
    version='probe',
    extensions=_extensions or None,
)

# As lenz-mcp does: a stateless server cannot deliver a subscription stream, and
# leaving the handler registered would make `server/discover` advertise four things
# (three listChanged flags and resources.subscribe) that this probe cannot serve —
# the same honesty rule the real server follows.
del mcp._lowlevel_server._request_handlers['subscriptions/listen']


@mcp.tool()
async def sleep_probe(seconds: int, progress: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Sleep for `seconds` (capped at 300), then return when it started and finished.

    With `progress` true, a progress notification is sent every 10 seconds while
    sleeping (to learn whether progress extends the client's timeout)."""
    seconds = max(0, min(int(seconds), MAX_SLEEP_SECONDS))
    started = time.monotonic()
    started_at = _now()
    meta = ctx.request_context.meta if ctx is not None else None
    # `_meta` is a TypedDict on 2.x — a plain dict at runtime, so `meta.progressToken`
    # is an AttributeError, and EVERY modern call carries a non-empty `_meta`. Both
    # spellings are read: the wire says `progressToken`, the TypedDict field is
    # `progress_token`, and which one survives validation is the SDK's business.
    # Chosen by key PRESENCE, not truthiness: `progressToken: 0` is a valid token,
    # and `or` would drop it and measure the no-progress behaviour instead.
    token = None
    if meta:
        for key in ('progress_token', 'progressToken'):
            if key in meta:
                token = meta[key]
                break
    log('sleep_probe.start', seconds=seconds, progress=progress, client_progress_token=token)
    remaining = seconds
    while remaining > 0:
        step = min(10, remaining) if progress else remaining
        await asyncio.sleep(step)
        remaining -= step
        if progress and token is not None and remaining > 0:
            try:
                # Not ctx.report_progress: the notification needs the request id on
                # it, because a stateless server has no other stream to carry it, and
                # the helper has sent it without one. Passed explicitly so this does
                # not depend on which way the helper behaves today.
                await ctx.request_context.session.send_progress_notification(
                    progress_token=token,
                    progress=seconds - remaining,
                    total=seconds,
                    related_request_id=ctx.request_id,
                )
                log('sleep_probe.progress_sent', progress=seconds - remaining)
            except Exception as exc:  # a closed stream is itself a finding
                log('sleep_probe.progress_failed', error=repr(exc))
    finished_at = _now()
    log('sleep_probe.finish', seconds=seconds, elapsed=round(time.monotonic() - started, 2))
    return {'slept': seconds, 'started_at': started_at, 'finished_at': finished_at}


@mcp.tool(
    meta={'ui': {'resourceUri': CARD_URI}},
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
)
def card_probe() -> dict[str, Any]:
    """Show the probe card. Returns a small result; a host that renders MCP Apps shows it as a card."""
    log('card_probe.called')
    return {'probe': 'card', 'rendered_at': _now(), 'note': 'If you see a card, use its buttons.'}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def card_ping(note: str = '') -> dict[str, Any]:
    """Answer the probe card's ping button. Read-only. `note` carries what the card saw of the host."""
    log('card_ping.called', note=note)
    return {'pong': True, 'at': _now(), 'echo': note}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def spend_probe(note: str = '') -> dict[str, Any]:
    """A probe tool marked NOT read-only (it changes nothing: it echoes and logs).

    Tells whether a host asks for approval before a card calls a non-read-only
    tool, and whether the model sees a card-initiated call's result: the
    `result_token` is new on every call, so the model can only quote it if it saw it."""
    token = f'spend-{secrets.token_hex(3)}'
    log('spend_probe.called', note=note, result_token=token)
    return {'spent': False, 'at': _now(), 'echo': note, 'result_token': token}


@mcp.tool(
    meta={'ui': {'visibility': ['app']}},
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
)
def card_log(event: str, detail: str = '') -> dict[str, Any]:
    """Probe card internals: copies what the host answered the card into the server log. Never call it yourself."""
    log('card.' + event[:60], detail=detail[:4000])
    return {'logged': True}


CARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Lenz probe card</title>
<style>
  body { margin: 0; padding: 16px; font-family: var(--font-sans, system-ui, sans-serif);
         background: var(--color-background-primary, transparent); color: var(--color-text-primary, inherit); }
  h1 { font-size: 16px; margin: 0 0 8px; }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin: 0 0 12px; font-size: 13px; }
  dt { opacity: .7; } dd { margin: 0; word-break: break-word; }
  .buttons { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 12px; }
  button { font: inherit; font-size: 13px; padding: 6px 10px; cursor: pointer; }
  pre { font-size: 11px; white-space: pre-wrap; word-break: break-word; margin: 0; max-height: 320px; overflow: auto; }
</style></head>
<body>
<h1>Lenz probe card (v2)</h1>
<dl>
  <dt>Rendered</dt><dd id="rendered"></dd>
  <dt>Host</dt><dd id="host">(no ui/initialize reply yet)</dd>
  <dt>Capabilities</dt><dd id="caps">?</dd>
  <dt>Theme</dt><dd id="theme">?</dd>
  <dt>Style variables</dt><dd id="styles">?</dd>
  <dt>Container</dt><dd id="dims">?</dd>
  <dt>Secret word</dt><dd id="secret"></dd>
</dl>
<div class="buttons">
  <button id="b-ping">1. Ping (card_ping, read-only)</button>
  <button id="b-spend">2. Call spend_probe directly</button>
  <button id="b-context">3. Tell Claude the secret word (ui/update-model-context)</button>
  <button id="b-message">4. Ask Claude to run a check (ui/message)</button>
  <button id="b-link">5. Open lenz.io (ui/open-link)</button>
  <button id="b-full">6. Fullscreen (ui/request-display-mode)</button>
  <button id="b-inline">7. Back inline</button>
</div>
<pre id="log"></pre>
<script>
(function () {
  var nextId = 1, pending = {}, seen = {};
  var secret = 'probe-' + Math.random().toString(16).slice(2, 8);
  function $(id) { return document.getElementById(id); }
  $('rendered').textContent = new Date().toISOString();
  $('secret').textContent = secret + ' (only Claude hears it, and only through button 3)';
  function send(msg) { window.parent.postMessage(msg, '*'); }
  function show(line) { $('log').textContent = new Date().toISOString().slice(11, 19) + ' ' + line + '\\n' + $('log').textContent; reportSize(); }
  // Everything the host answers goes to the card AND, best effort, to the server log.
  function record(event, detail) {
    var text = typeof detail === 'string' ? detail : JSON.stringify(detail);
    show(event + ': ' + text);
    if (event.indexOf('card_log') === 0) { return; }
    request('tools/call', {name: 'card_log', arguments: {event: event, detail: text}}, true).catch(function () {});
  }
  function request(method, params, quiet) {
    var id = nextId++;
    send({jsonrpc: '2.0', id: id, method: method, params: params || {}});
    return new Promise(function (resolve, reject) {
      pending[id] = {resolve: resolve, reject: reject};
      // Long enough for a host to ask the user first.
      setTimeout(function () { if (pending[id]) { delete pending[id]; reject(new Error('no reply to ' + method + ' in 120s')); } }, 120000);
    });
  }
  function notify(method, params) { send({jsonrpc: '2.0', method: method, params: params || {}}); }
  // Content height, not the iframe's: measured as the ext-apps SDK does
  // (html at max-content), so a frame taller than the card can still shrink.
  var lastWidth = 0, lastHeight = 0;
  function reportSize() {
    var html = document.documentElement, original = html.style.height;
    html.style.height = 'max-content';
    var height = Math.ceil(html.getBoundingClientRect().height);
    html.style.height = original;
    var width = Math.ceil(window.innerWidth);
    if (width === lastWidth && height === lastHeight) { return; }
    lastWidth = width; lastHeight = height;
    notify('ui/notifications/size-changed', {width: width, height: height});
  }
  function applyStyles(ctx) {
    // Claude's style variables are light-dark() values: they follow color-scheme, so set it on every theme.
    if (ctx.theme) { document.documentElement.style.colorScheme = ctx.theme; }
    if (ctx.styles && ctx.styles.variables) {
      Object.keys(ctx.styles.variables).forEach(function (k) { document.documentElement.style.setProperty(k, ctx.styles.variables[k]); });
    }
  }
  function press(label, method, params) {
    record(label + ' -> ' + method, params);
    request(method, params).then(function (result) {
      record(label + ' <- result', result);
    }).catch(function (err) { record(label + ' <- error', err.message); });
  }
  window.addEventListener('message', function (event) {
    var msg = event.data;
    if (!msg || msg.jsonrpc !== '2.0') { return; }
    if (msg.id !== undefined && pending[msg.id]) {
      var p = pending[msg.id]; delete pending[msg.id];
      if (msg.error) { p.reject(new Error(JSON.stringify(msg.error))); } else { p.resolve(msg.result); }
      return;
    }
    if (msg.method === 'ui/notifications/host-context-changed' && msg.params) {
      if (msg.params.theme) { $('theme').textContent = msg.params.theme + ' (changed)'; }
      applyStyles(msg.params);
      record('host-context-changed', msg.params.displayMode ? {displayMode: msg.params.displayMode, theme: msg.params.theme} : {theme: msg.params.theme});
    } else if (msg.method === 'ui/resource-teardown' && msg.id !== undefined) {
      send({jsonrpc: '2.0', id: msg.id, result: {}});
    } else if (msg.method && msg.method !== 'ui/notifications/tool-input' && msg.method !== 'ui/notifications/tool-result') {
      show('host sent ' + msg.method);
    }
    reportSize();
  });
  // McpUiInitializeRequest params (ext-apps src/spec.types.ts): appInfo, appCapabilities, protocolVersion.
  request('ui/initialize', {
    appInfo: {name: 'lenz-probe-card', version: '2'},
    appCapabilities: {availableDisplayModes: ['inline', 'fullscreen']},
    protocolVersion: '2026-01-26'
  }).then(function (result) {
    var ctx = (result && result.hostContext) || {};
    var info = (result && result.hostInfo) || {};
    seen.host = (info.name || '?') + ' ' + (info.version || '') + ' / protocol ' + (result && result.protocolVersion);
    var vars = ctx.styles && ctx.styles.variables ? Object.keys(ctx.styles.variables) : [];
    $('host').textContent = seen.host;
    $('caps').textContent = JSON.stringify((result && result.hostCapabilities) || null);
    $('theme').textContent = ctx.theme || '(none)';
    $('styles').textContent = vars.length + ' variables; bg=' + (vars.length ? String(ctx.styles.variables['--color-background-primary']).slice(0, 80) : '');
    $('dims').textContent = JSON.stringify(ctx.containerDimensions || null) + ' mode=' + ctx.displayMode + ' available=' + JSON.stringify(ctx.availableDisplayModes || null);
    applyStyles(ctx);
    notify('ui/notifications/initialized', {});
    record('initialize <- hostCapabilities', (result && result.hostCapabilities) || null);
  }).catch(function (err) { $('host').textContent = 'ui/initialize failed: ' + err.message; reportSize(); });

  $('b-ping').addEventListener('click', function () { press('1 ping', 'tools/call', {name: 'card_ping', arguments: {note: 'from the card'}}); });
  $('b-spend').addEventListener('click', function () { press('2 spend', 'tools/call', {name: 'spend_probe', arguments: {note: 'direct from the card'}}); });
  $('b-context').addEventListener('click', function () {
    press('3 context', 'ui/update-model-context', {
      content: [{type: 'text', text: 'The Lenz probe card says: the secret word is ' + secret + '.'}],
      structuredContent: {secret_word: secret}
    });
  });
  $('b-message').addEventListener('click', function () {
    press('4 message', 'ui/message', {role: 'user', content: [{type: 'text', text: "Call the lenz-probe spend_probe tool with note 'from the card button'."}]});
  });
  $('b-link').addEventListener('click', function () { press('5 link', 'ui/open-link', {url: 'https://lenz.io'}); });
  $('b-full').addEventListener('click', function () { press('6 fullscreen', 'ui/request-display-mode', {mode: 'fullscreen'}); });
  $('b-inline').addEventListener('click', function () { press('7 inline', 'ui/request-display-mode', {mode: 'inline'}); });
  if (window.ResizeObserver) { var ro = new ResizeObserver(reportSize); ro.observe(document.documentElement); ro.observe(document.body); }
  reportSize();
})();
</script>
</body></html>
"""


# ── The real Lenz card with its dev fixture switcher ─────────────────────
# `card_fixture` shows the card lenz-mcp serves, built with the switcher
# (in the card package: `npm run build:dev`). Its button calls the two fake tools below,
# which replay a canned deep check in ~10 seconds from the card fixtures, so the
# button -> running -> result path runs in real Claude with no credits and no wait.
# lenz-mcp has none of these, and the connector's probe-server test checks it.
# The card lives in the connector's package; found from the repository root, the
# nearest parent with a pyproject.toml.
CARD_DIR = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file()) / 'src/lenz_mcp/card'
FIXTURE_CARD_URI = 'ui://lenz-probe/card-dev-v1'
CANNED_STAGES = [
    (1.5, 'starting', 0),
    (3.0, 'framing', 1),
    (5.0, 'research', 2),
    (7.0, 'debate', 3),
    (8.5, 'adjudication', 4),
    (10.0, 'conclusion', 5),
]
_CANNED_RUNS: dict[str, float] = {}


# The card's delivery hint, stamped as lenz-mcp stamps it (mcp_card.py
# card_delivery, keyed on the VENDOR token). Without it the real card in
# ChatGPT takes the silent `ui/update-model-context` route, which ChatGPT
# accepts and drops (measured 2026-09-18). A copy, not an import: this probe
# runs standalone. The connector's probe-server test pins it to lenz-mcp's rule.
#
# The token, not the full identity: a new parenthesised suffix on a vendor's
# own client must not change how its card reaches the model. One did
# (`openai-mcp/1.0.0 (ChatGPT)`, 2026-09-23), and under an identity table the
# card would have gone silent in the app it was measured in.
MESSAGE_DELIVERY_VENDOR_TOKENS = frozenset({'openai-mcp'})
_REQUEST_USER_AGENT: contextvars.ContextVar[str] = contextvars.ContextVar('probe_user_agent', default='')


def bind_user_agent(user_agent: str):
    token = _REQUEST_USER_AGENT.set(user_agent or '')
    return lambda: _REQUEST_USER_AGENT.reset(token)


# Stops at whitespace as well as `/`: a version is optional and a suffix is
# not tied to one, so `openai-mcp (Codex)` is a shape a host can send.
_UA_TOKEN_END = re.compile(r'[/\s()]')


def _vendor_token() -> str:
    return _UA_TOKEN_END.split(_REQUEST_USER_AGENT.get().strip(), 1)[0]


def _bind_from(ctx: Context | None) -> None:
    # Read from the tool's own request, as lenz-mcp reads it from the request
    # its middleware sees: a value set in the ASGI layer does not reach the task
    # that runs the handler (the transport test measured exactly that).
    try:
        headers = ctx.headers if ctx is not None else None
    except ValueError:  # outside a request
        headers = None
    if headers is not None:
        _REQUEST_USER_AGENT.set(headers.get('user-agent', '') or '')


def _with_delivery(result: dict[str, Any]) -> dict[str, Any]:
    deliver = 'message' if _vendor_token() in MESSAGE_DELIVERY_VENDOR_TOKENS else 'context'
    return {**result, '_card': {'deliver': deliver}}


def _fixtures() -> dict[str, Any]:
    return json.loads((CARD_DIR / 'fixtures' / 'fixtures.json').read_text(encoding='utf-8'))


@mcp.tool(
    meta={'ui': {'resourceUri': FIXTURE_CARD_URI}},
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
)
def card_fixture() -> dict[str, Any]:
    """Show the Lenz card with its dev fixture switcher. Returns a quick check from the synthetic fixtures."""
    log('card_fixture.called')
    return _fixtures()['quick']['quick-low']['toolResult']


@mcp.tool(
    name='start_verification_widget',
    meta={'ui': {'visibility': ['app']}},
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def fake_start_verification_widget(claim: str, retry_of: str = '', ctx: Context = None) -> dict[str, Any]:
    """Probe stand-in for the Lenz card's start tool: starts a canned ~10 s run. Never call it yourself."""
    _bind_from(ctx)
    task_id = secrets.token_hex(16)
    _CANNED_RUNS[task_id] = time.monotonic()
    log('fake_start.called', claim=claim[:200], retry_of=retry_of, task_id=task_id)
    return _with_delivery({'status': 'submitted', 'task_id': task_id})


@mcp.tool(
    name='get_verification_widget',
    meta={'ui': {'visibility': ['app']}},
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
)
def fake_get_verification_widget(task_id: str, ctx: Context = None) -> dict[str, Any]:
    """Probe stand-in for the Lenz card's poll tool: replays the canned run. Never call it yourself."""
    _bind_from(ctx)
    result = _fixtures()['deep']['deep-28-sources-3-warnings']['result']
    if len(task_id) == 8 and task_id == result['verification_id']:
        log('fake_get.recovered', task_id=task_id)
        return _with_delivery(result)
    started = _CANNED_RUNS.get(task_id)
    if started is None:
        log('fake_get.not_found', task_id=task_id)
        return _with_delivery({'status': 'not_found', 'message': 'That check could not be found.'})
    elapsed = time.monotonic() - started
    for until, step, index in CANNED_STAGES:
        if elapsed < until:
            log('fake_get.processing', task_id=task_id, step=step)
            return _with_delivery(
                {'status': 'processing', 'step': step, 'index': index, 'total': 5, 'elapsed_seconds': int(elapsed)}
            )
    log('fake_get.completed', task_id=task_id)
    return _with_delivery(result)


@mcp.resource(
    FIXTURE_CARD_URI,
    name='lenz_card_dev',
    title='Lenz card (dev fixtures)',
    mime_type=CARD_MIME,
    meta={'ui': {'csp': {'connectDomains': [], 'resourceDomains': []}, 'prefersBorder': False}},
)
def lenz_card_dev() -> str:
    log('lenz_card_dev.read')
    return (CARD_DIR / 'dist-dev' / 'card-dev.html').read_text(encoding='utf-8')


@mcp.resource(
    CARD_URI, name='probe_card', title='Lenz probe card', mime_type=CARD_MIME, meta={'ui': {'prefersBorder': True}}
)
def probe_card() -> str:
    log('card_resource.read')
    return CARD_HTML


class RequestLog:
    """Log each request's JSON-RPC method, protocol version and, on initialize,
    what the client declares. The body is buffered and replayed unchanged."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode(errors='replace') for k, v in scope.get('headers', [])}
        chunks, more = [], True
        while more:
            message = await receive()
            if message['type'] != 'http.request':
                break
            chunks.append(message.get('body', b''))
            more = message.get('more_body', False)
        body = b''.join(chunks)
        entry: dict[str, Any] = {
            'http': f'{scope["method"]} {scope["path"]}',
            'protocol_version': headers.get('mcp-protocol-version'),
            'session_id': headers.get('mcp-session-id'),
            'user_agent': headers.get('user-agent'),
            'accept': headers.get('accept'),
        }
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            payload = None
        for item in payload if isinstance(payload, list) else [payload] if payload else []:
            if not isinstance(item, dict):
                continue
            entry.setdefault('rpc', []).append({'method': item.get('method'), 'id': item.get('id')})
            params = item.get('params') or {}
            if item.get('method') == 'initialize':
                entry['initialize'] = {
                    'protocolVersion': params.get('protocolVersion'),
                    'clientInfo': params.get('clientInfo'),
                    'capabilities': params.get('capabilities'),
                }
            elif item.get('method') == 'tools/call':
                entry['tool'] = {
                    'name': params.get('name'),
                    'arguments': params.get('arguments'),
                    'meta': params.get('_meta'),
                }
        log('request', **entry)

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            message = await receive()
            if message['type'] == 'http.disconnect' and not status.get('complete'):
                # The client gave up before the answer: on a tool call, this is its timeout.
                log(
                    'client.gave_up',
                    http=entry['http'],
                    rpc=entry.get('rpc'),
                    after=round(time.monotonic() - started, 2),
                )
            return message

        started = time.monotonic()
        status: dict[str, Any] = {}

        async def send_logged(message):
            if message['type'] == 'http.response.start':
                status['code'] = message['status']
            elif message['type'] == 'http.response.body' and not message.get('more_body', False):
                status['complete'] = True
            await send(message)

        try:
            await self.app(scope, replay, send_logged)
        finally:
            log(
                'response',
                http=entry['http'],
                rpc=entry.get('rpc'),
                status=status.get('code'),
                elapsed=round(time.monotonic() - started, 2),
            )


def build_app():
    # `stateless_http` and `transport_security` moved to the builder in mcp 2.x.
    # A tunnel forwards its own Host header; the rebinding guard would 421 it.
    return RequestLog(
        mcp.streamable_http_app(
            stateless_http=True,  # as lenz-mcp runs
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
    )


if __name__ == '__main__':  # pragma: no cover - manual run
    import uvicorn

    log('probe.start', url=f'http://{HOST}:{PORT}/mcp', python=sys.version.split()[0])
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level='warning')
