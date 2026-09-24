"""Drive the assembled MCP app over HTTP, in either protocol era.

Every behaviour has to be checked twice: once as a 2025-era client speaks it
(`initialize`, then requests carrying `MCP-Protocol-Version`) and once as a
2026-07-28 client does (no handshake; the version, the client identity and the
capabilities ride each request's `_meta`, and the method is repeated in an
`Mcp-Method` header).

`scripts/smoke.py` owns the wire contract for a DEPLOYED server (it is what the
smoke test speaks, stdlib-only so it runs on a plain system python3). This module
mirrors it for tests, and a wire test pins the two against each other, so a
change to one era's shape cannot quietly apply to only one of them.

Two things it exists to stop:

- **Tests that build their own transport.** Each one then proves something about a
  hand-built stack instead of `lenz_mcp.asgi.application` — the thing that is
  deployed, with the protocol log and the GET guard around it (and CORS, which
  `asgi.py` adds only when `MCP_OAUTH_ENABLED`).
- **Sharing one session manager.** The SDK allows `run()` once per manager
  instance, so a second suite entering the shared one raises. `assembled_app`
  re-imports the MCP modules, which builds a fresh server and a fresh manager per
  test, and restores the modules afterwards.

Usage:

    with assembled_app(MCP_OAUTH_ENABLED=False) as harness:
        wire = harness.wire()
        wire.initialize()                      # legacy handshake
        result = wire.rpc('tools/list')        # legacy request
        result = wire.rpc('tools/list', era=MODERN)   # 2026-07-28 request
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
from pathlib import Path
from typing import Any

import httpx


def _repo_root() -> Path:
    """The repository this package is checked out in: the nearest parent with a
    pyproject.toml. Tests and dev tools locate files from it, so they read the
    same whichever layout the package sits in."""
    for parent in Path(__file__).resolve().parents:
        if (parent / 'pyproject.toml').is_file():
            return parent
    raise RuntimeError('no pyproject.toml above the package: not a checkout')


REPO_ROOT = _repo_root()

LEGACY = 'legacy'
MODERN = 'modern'
LEGACY_VERSION = '2025-11-25'
MODERN_VERSION = '2026-07-28'

# The modules that hold MCP state, in import order: config reads the environment, server
# builds the MCPServer/FastMCP from config, asgi assembles the app around it.
_MCP_MODULES = (
    'lenz_mcp.config',
    'lenz_mcp.mcp_card',
    'lenz_mcp.widget_resource',
    'lenz_mcp.server',
    'lenz_mcp.asgi',
)


#: The MCP Apps extension, and the mime type a client must list under it for
#: the SDK to count it as declared (`mcp.server.apps.client_supports_apps`).
APPS_EXTENSION_ID = 'io.modelcontextprotocol/ui'
APP_MIME_TYPE = 'text/html;profile=mcp-app'

#: What a card host declares: the capabilities block that makes `card_active`
#: true on the modern era. A test that wants a card client asks for THIS rather
#: than spelling the extension, so the one place the shape is written is here.
DECLARES_APPS: dict[str, Any] = {'extensions': {APPS_EXTENSION_ID: {'mimeTypes': [APP_MIME_TYPE]}}}
#: A client that declares capabilities and NOT the card — Claude Code's shape.
DECLARES_NO_APPS: dict[str, Any] = {'elicitation': {}, 'roots': {'listChanged': True}}


def modern_meta(client_name: str = 'wire', capabilities: dict[str, Any] | None = None) -> dict[str, Any]:
    """The `_meta` block a 2026-07-28 client puts on every request."""
    return {
        'io.modelcontextprotocol/protocolVersion': MODERN_VERSION,
        'io.modelcontextprotocol/clientInfo': {'name': client_name, 'version': '1'},
        'io.modelcontextprotocol/clientCapabilities': capabilities or {},
    }


def messages(response: httpx.Response) -> list[dict[str, Any]]:
    """Every JSON-RPC message in a response, whether it came back as JSON or SSE."""
    body = response.text
    if not body.strip():
        return []
    if response.headers.get('content-type', '').startswith('text/event-stream'):
        found = []
        for event in body.replace('\r\n', '\n').split('\n\n'):
            data = '\n'.join(line[5:].lstrip() for line in event.split('\n') if line.startswith('data:'))
            if data:
                with contextlib.suppress(ValueError):
                    found.append(json.loads(data))
        return [m for m in found if isinstance(m, dict)]
    try:
        parsed = json.loads(body)
    except ValueError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    return [m for m in items if isinstance(m, dict)]


class Wire:
    """One client against one assembled app. Async under the hood, sync to use."""

    def __init__(
        self,
        harness,
        *,
        user_agent: str = 'wire/1',
        authorization: str | None = None,
        timeout: float = 10.0,
        capabilities: dict[str, Any] | None = None,
    ):
        self._harness = harness
        self.user_agent = user_agent
        self.authorization = authorization
        self.timeout = timeout
        # What this client DECLARES on every modern request. The card is keyed
        # on it, so a card client is one that passes `DECLARES_APPS` here — a
        # User-Agent alone no longer makes one, in the harness any more than in
        # production. Only the 2026-07-28 era carries it: the 2025 handshake
        # declares capabilities once, and this server is stateless, so nothing
        # a legacy client declares survives to its next request.
        self.capabilities = capabilities
        self.negotiated_version: str | None = None
        self._id = 0

    # ── requests ──

    def request(self, method: str, path: str = '/mcp', **kwargs) -> httpx.Response:
        async def _go():
            transport = httpx.ASGITransport(app=self._harness.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
                return await client.request(method, path, timeout=self.timeout, **kwargs)

        # On the harness's loop, the one the session manager's task group lives on:
        # a request from another loop fails inside the SDK, not in the test.
        return self._harness.run(_go(), self.timeout)

    def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        era: str = LEGACY,
        name: str | None = None,
        notification: bool = False,
        client_name: str = 'wire',
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One JSON-RPC request, shaped for `era`. Returns the raw response.

        `name` fills the modern `Mcp-Name` header (the tool name, the prompt name or
        the resource URI the request is about); the transport rejects a mismatch.
        """
        self._id += 1
        payload: dict[str, Any] = {'jsonrpc': '2.0', 'method': method}
        if not notification:
            payload['id'] = f'wire-{self._id}'
        params = dict(params or {})
        sent = {
            'content-type': 'application/json',
            'accept': 'application/json, text/event-stream',
            'user-agent': self.user_agent,
        }
        if era == MODERN:
            # MERGED, not overwritten: the 2026 era carries client capabilities only in
            # `_meta`, so overwriting a caller's block would drop exactly what a
            # capability-gated test declares, and the test would pass against a server
            # that saw no capabilities at all.
            params['_meta'] = {**modern_meta(client_name, self.capabilities), **(params.get('_meta') or {})}
            sent['mcp-protocol-version'] = MODERN_VERSION
            sent['mcp-method'] = method
            if name:
                sent['mcp-name'] = name
        elif method != 'initialize':
            # The version this session actually negotiated, not a constant: a 2.x
            # transport that checks the header against it would reject the mismatch,
            # and the failure would read as a server bug.
            sent['mcp-protocol-version'] = self.negotiated_version or LEGACY_VERSION
        if params:
            payload['params'] = params
        if self.authorization:
            sent['authorization'] = self.authorization
        sent.update(headers or {})
        return self.request('POST', content=json.dumps(payload), headers=sent)

    # ── replies ──

    @staticmethod
    def reply(response: httpx.Response) -> dict[str, Any]:
        """The JSON-RPC message answering THIS response's own request, matched by id.

        The id is read back off `response.request`, not from a counter: a helper that
        assumed "the newest request" would read an earlier response against a later
        id, and every assertion after the first would be about the wrong message.
        """
        try:
            wanted = json.loads(response.request.content)['id']
        except (KeyError, ValueError, TypeError) as exc:  # pragma: no cover - a test bug, not a server one
            raise AssertionError(f'the request carried no JSON-RPC id: {exc}') from exc
        for message in messages(response):
            if message.get('id') == wanted:
                return message
        raise AssertionError(f'no JSON-RPC reply with id {wanted}: HTTP {response.status_code} {response.text[:300]}')

    def result(self, response: httpx.Response) -> dict[str, Any]:
        """The `result` object, or an assertion naming what came back instead."""
        assert response.status_code == 200, f'HTTP {response.status_code}: {response.text[:300]}'
        message = self.reply(response)
        assert 'error' not in message, f'JSON-RPC error: {message["error"]}'
        result = message.get('result')
        assert isinstance(result, dict), f'no result object: {message}'
        return result

    def error(self, response: httpx.Response) -> dict[str, Any]:
        """The `error` object, for the paths that are supposed to fail."""
        message = self.reply(response)
        assert 'error' in message, f'expected a JSON-RPC error, got {message}'
        return message['error']

    def call(self, method: str, params: dict[str, Any] | None = None, **kwargs) -> dict[str, Any]:
        """`rpc` plus `result`, for the happy path."""
        return self.result(self.rpc(method, params, **kwargs))

    # ── the legacy handshake ──

    def initialize(self, version: str = LEGACY_VERSION, client_name: str = 'wire') -> dict[str, Any]:
        result = self.call(
            'initialize',
            {
                'protocolVersion': version,
                'capabilities': {},
                'clientInfo': {'name': client_name, 'version': '1'},
            },
        )
        self.negotiated_version = result.get('protocolVersion')
        acknowledged = self.rpc('notifications/initialized', notification=True)
        assert acknowledged.status_code in (200, 202), acknowledged.status_code
        return result


class Harness:
    """The assembled app, its session manager and the loop both run on."""

    def __init__(self, app, loop):
        self.app = app
        self.loop = loop

    def run(self, coro, timeout: float = 10.0):
        return self.loop.run_until_complete(asyncio.wait_for(coro, timeout))

    def wire(self, **kwargs) -> Wire:
        return Wire(self, **kwargs)


def _cancel_pending(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel whatever is still running, and let the cancellations land, before the close.

    `sse_starlette` starts ONE `_shutdown_watcher` per thread the first time the 2025
    transport builds an `EventSourceResponse`. It is a `while True` that ends only when
    uvicorn sets `AppStatus.should_exit`, which nothing does here — so closing the loop
    under it prints `Task was destroyed but it is pending!`. In a server the loop lives as
    long as the process and uvicorn's shutdown ends the watcher properly, so the warning
    is an artifact of creating and destroying a loop per test, not a leak.

    Sweeping is still worth it: the flag is thread-local and sticky, so exactly one such
    line appears per process no matter how many tests run, and that one line is
    indistinguishable from a task WE genuinely leaked. Cancelling here keeps
    `Task was destroyed` meaning "something is wrong" rather than "this is the usual one".
    """
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    if not pending:
        return
    for task in pending:
        task.cancel()
    # Bounded: a task that swallows CancelledError would otherwise hang the whole
    # pytest run here, with no timeout and no clue which task did it.
    loop.run_until_complete(asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5))


def _env_value(value: Any) -> str:
    if isinstance(value, bool):
        return 'True' if value else 'False'
    if isinstance(value, list | tuple):
        return ','.join(value)
    return str(value)


@contextlib.contextmanager
def connector_env(**overrides):
    """Set the connector's environment variables, restoring them afterwards.

    `lenz_mcp.config` reads the environment at import, so a caller reloads it
    (or the modules built from it) inside this block. `None` unsets a variable.
    """
    import os

    saved = {name: os.environ.get(name) for name in overrides}
    try:
        for name, value in overrides.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = _env_value(value)
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextlib.contextmanager
def assembled_app(**env_overrides):
    """`lenz_mcp.asgi.application`, rebuilt under `env_overrides`, running.

    The overrides are the connector's environment variables (`MCP_OAUTH_ENABLED=False`),
    written as their Python values: a bool becomes `True`/`False`, a list is
    comma-joined. Re-imports the MCP modules so the server, its tools and its
    session manager are fresh for this test (the SDK allows one `run()` per
    manager), then restores them on the way out so the next test gets the
    default server.
    """

    # Snapshot each module's namespace and put it back afterwards, rather than
    # reloading again on the way out. Other suites hold references taken at import
    # (`from lenz_mcp.server import mcp`) and enter that object's session manager
    # while driving `asgi.application`; a second reload leaves those two pointing at
    # different servers, and the SDK raises "Task group is not initialized" — which
    # reads as a broken test, not as this fixture's doing.
    originals = {name: dict(importlib.import_module(name).__dict__) for name in _MCP_MODULES}
    try:
        with connector_env(**env_overrides):
            modules = [importlib.reload(importlib.import_module(name)) for name in _MCP_MODULES]
            asgi = modules[-1]
            manager = importlib.import_module('lenz_mcp.server').mcp.session_manager
            loop = asyncio.new_event_loop()
            stop = asyncio.Event()
            ready: asyncio.Future = loop.create_future()

            async def _lifespan():
                # Entered and left inside ONE task: the SDK's session manager holds an
                # anyio task group, and anyio refuses to leave a cancel scope in a task
                # other than the one that entered it. Driving __aenter__ and __aexit__
                # from two `run_until_complete` calls raises on every teardown, and
                # swallowing that would hide a real shutdown failure with it.
                async with manager.run():
                    ready.set_result(None)
                    await stop.wait()

            lifespan = loop.create_task(_lifespan())
            try:
                loop.run_until_complete(asyncio.wait_for(asyncio.shield(ready), 10))
                yield Harness(asgi.application, loop)
            finally:
                stop.set()
                try:
                    loop.run_until_complete(asyncio.wait_for(lifespan, 10))
                finally:
                    _cancel_pending(loop)
                    loop.close()
    finally:
        # Always: a test that raises inside the context would otherwise leave every
        # other suite with a reloaded server whose manager has stopped.
        for name, namespace in originals.items():
            module = importlib.import_module(name)
            module.__dict__.clear()
            module.__dict__.update(namespace)
