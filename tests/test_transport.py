"""Transport-level tests for the MCP ASGI app (src/lenz_mcp/asgi.py).

These drive the assembled Starlette app rather than the tool functions, so they
cover what the SDK does with a request *before* it reaches a tool. No DB, no
network: every assertion is about status codes on the transport itself.
"""

import asyncio

import httpx

from lenz_mcp import asgi

# A GET that is NOT short-circuited opens an SSE stream that never completes, so
# every request here is bounded — a regression must fail the test, not hang it.
_TIMEOUT_S = 5


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, _TIMEOUT_S))


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=asgi.application)
    async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as c:
        return await c.request(method, path, **kwargs)


def test_get_on_the_mcp_endpoint_is_rejected_rather_than_left_open():
    """A stateless server can never push on the server→client stream, so it must
    decline it. Leaving it open costs a concurrency slot on the host and ends in a
    truncated body when the request timeout expires, which clients read as a
    dropped connection."""
    resp = _run(_request('GET', '/mcp', headers={'accept': 'text/event-stream'}))

    assert resp.status_code == 405
    assert 'POST' in resp.headers.get('allow', '')


def test_get_rejection_is_scoped_to_the_protocol_endpoint():
    """`/mcp/healthz` is a plain GET probe under the same /mcp prefix, so the
    405 must not swallow it."""
    resp = _run(_request('GET', '/mcp/healthz'))

    assert resp.status_code == 200
    assert resp.json() == {'status': 'ok'}


def _guarded_stub(enabled=True):
    """The guard wrapped around a sentinel app, so pass-through is observable
    without booting the session manager (which needs the ASGI lifespan)."""
    reached = []

    async def _inner(scope, _receive, _send):
        reached.append((scope['method'], scope['path']))

    guard = asgi.ServerStreamGuard(_inner, path='/mcp', enabled=enabled)
    return guard, reached


async def _drive(guard, method, path):
    sent = []

    async def _send(message):
        sent.append(message)

    await guard({'type': 'http', 'method': method, 'path': path, 'headers': []}, None, _send)
    return sent


def test_post_still_reaches_the_mcp_protocol_handler():
    """The short-circuit is GET-only: POST is how every tool call arrives."""
    guard, reached = _guarded_stub()

    sent = _run(_drive(guard, 'POST', '/mcp'))

    assert reached == [('POST', '/mcp')]
    assert sent == []


def test_healthz_under_the_endpoint_prefix_is_not_swallowed():
    """Guard matches the endpoint exactly, not the `/mcp/*` prefix."""
    guard, reached = _guarded_stub()

    _run(_drive(guard, 'GET', '/mcp/healthz'))

    assert reached == [('GET', '/mcp/healthz')]


def test_trailing_slash_form_is_also_rejected():
    guard, reached = _guarded_stub()

    sent = _run(_drive(guard, 'GET', '/mcp/'))

    assert reached == []
    assert sent[0]['status'] == 405


def test_guard_short_circuits_before_anything_inside_the_app():
    """The guard must sit OUTSIDE the MCP app, not inside it.

    With OAuth on, the SDK's transport auth 401s an unauthenticated GET before
    the stream handler is reached — so a guard mounted inside would look fine
    against an anonymous probe while authenticated clients still opened streams,
    which is precisely how this bug survived unnoticed. Nothing within the
    app may run: the answer is a flat 405 regardless of credentials.
    """
    guard, reached = _guarded_stub()

    sent = _run(_drive(guard, 'GET', '/mcp'))

    assert reached == []  # no routing, no auth middleware, no stream handler
    assert sent[0]['status'] == 405


def test_stateful_mode_lets_the_server_stream_through():
    """If stateless_http is ever turned off the stream becomes functional, so the
    guard must get out of the way rather than break server-initiated messages."""
    guard, reached = _guarded_stub(enabled=False)

    _run(_drive(guard, 'GET', '/mcp'))

    assert reached == [('GET', '/mcp')]


def test_guard_is_tied_to_stateless_mode():
    """The stream is only useless *because* the server is stateless. If that ever
    flips, the guard must stop rejecting GET — otherwise it would silently break
    server-initiated messages.

    2.x dropped `mcp.settings.stateless_http`: the mode is now an argument to
    `streamable_http_app()`, so `asgi.STATELESS` is the one value the app is built
    with AND the guard is told about — which is exactly the tie this test asserts.
    """
    assert asgi.STATELESS is True
    assert asgi.application.reject_server_stream is True
