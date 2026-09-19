"""The MCP wire: one transport and one JSON-RPC envelope, for every client we run.

Every client that speaks to a deployed server from outside it uses this module,
so they fail together or not at all. ``scripts/smoke.py`` is one: a smoke test
to run against a deployed server. A second, hand-written client could pass on a
wire the smoke test fails on, which is why the wire is shared rather than copied.

**Standard library only, and Python 3.9.** ``scripts/smoke.py`` runs on a plain
system ``python3``, outside the project's virtualenv and with no dependencies, so
this module inherits both constraints. Nothing from the rest of the package,
nothing outside the import set below, and no 3.10+ syntax evaluated at runtime
(``from __future__ import annotations`` covers annotations; a type alias assigned
as a value does not, which is why ``Transport`` is a string). A test proves it by
running the smoke test that way, rather than by inspecting anything.

What is deliberately NOT here: the argparse surface, the reporting, the exit codes
and the OAuth-mode decision, which all stay in ``scripts/smoke.py``. They are
the smoke test's own behaviour, and other clients need none of them.
"""

# Runs on a plain system python3, 3.9 or newer: no 3.10+ syntax at runtime.
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable  # noqa: F401 — resolves the string `Transport` alias below
from dataclasses import dataclass
from typing import Any

LEGACY_VERSION = '2025-11-25'
MODERN_VERSION = '2026-07-28'

#: Every post-handshake revision this wire can speak, NEWEST FIRST.
#:
#: A server is modern when it lists one of these, never when it lists
#: ``MODERN_VERSION``. The difference only shows up on the day a newer protocol
#: lands: after a rollback, TODAY's smoke test runs against an OLDER revision,
#: so a single pinned string would fail the rollback of a perfectly good 2.x
#: revision — the one moment the smoke test most needs to work. Append the new revision
#: here (newest first) and both the check and what the smoke speaks follow.
MODERN_VERSIONS = (MODERN_VERSION,)


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # lowercased names
    body: bytes


# A string, not a runtime expression: `bytes | None` is a TypeError before Python 3.10.
Transport = 'Callable[[str, str, dict[str, str], bytes | None, float], Response]'


class TransportError(Exception):
    """The request did not produce an HTTP response (refused, reset, timed out)."""


def _read_by(resp, deadline: float) -> bytes:
    """The whole body, or TransportError once the wall-clock deadline passes.

    A socket timeout alone is not a deadline: an SSE stream that sends a heartbeat
    every 15 s (the SDK's pings) never goes quiet for 20 s, so `resp.read()` would
    wait for the hosting platform's request timeout to cut it, and the smoke test would hang exactly on the
    held-stream regression it exists to catch."""
    chunks: list[bytes] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransportError('the response was still streaming at the deadline (a stream held open?)')
        try:  # bound the next read by what is left, not by the full per-request timeout
            resp.fp.raw._sock.settimeout(remaining)
        except AttributeError:
            pass
        chunk = resp.read1(65536)
        if not chunk:
            return b''.join(chunks)
        chunks.append(chunk)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A 3xx is answered as itself. Following it would send the Authorization header
    to wherever the Location points (urllib copies it, across hosts), and a
    redirected check would pass against a server that is not lenz-mcp."""

    def redirect_request(self, *_args, **_kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> Response:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    deadline = time.monotonic() + timeout
    try:
        with _OPENER.open(request, timeout=timeout) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, _read_by(resp, deadline))
    except urllib.error.HTTPError as exc:
        reader = exc.fp if hasattr(exc.fp, 'read1') else None
        error_body = _read_by(reader, deadline) if reader is not None else exc.read()
        return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()}, error_body)


def guard_transport(transport):
    """Map everything a transport can raise short of an HTTP answer to TransportError.

    `http.client.HTTPException` (IncompleteRead on a cut chunked body, BadStatusLine)
    is not an OSError: unmapped, one network blip right after the traffic shift would
    crash the smoke with a traceback instead of failing one check and retrying."""

    def _guarded(method, url, headers, body, timeout):
        try:
            return transport(method, url, headers, body, timeout)
        except TransportError:
            raise
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError) as exc:
            raise TransportError(f'{type(exc).__name__}: {exc}') from exc

    return _guarded


def modern_meta(client_name: str = 'lenz-deploy-smoke', version: str = MODERN_VERSIONS[0]) -> dict[str, Any]:
    """The `_meta` block a post-handshake client puts on every request.

    A function, not an inline literal, so the test harness can pin its shape against
    this one: this file is what the smoke test speaks,
    and the two must not drift apart.

    `version` is the revision being spoken. It is a parameter because a caller may
    have to speak an OLDER one than it prefers — see `MODERN_VERSIONS`.
    """
    return {
        'io.modelcontextprotocol/protocolVersion': version,
        'io.modelcontextprotocol/clientInfo': {'name': client_name, 'version': '1'},
        'io.modelcontextprotocol/clientCapabilities': {},
    }


def request(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    era: str,
    request_id: str | None,
    user_agent: str,
    client_name: str,
    key: str | None = None,
    name: str | None = None,
    version: str = MODERN_VERSIONS[0],
) -> tuple[dict[str, str], bytes]:
    """The headers and body of one JSON-RPC request to `/mcp`, in either era.

    The one place the envelope is built, for every client alike:
    `_meta` and the `MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` headers on a
    2026-07-28 request, the version header on every 2025-era request except
    `initialize`, and the Bearer key. `request_id=None` is a notification.

    `version` is the 2026-07-28-era revision to speak. It feeds BOTH the `_meta`
    block and the `MCP-Protocol-Version` header, from one argument: the two drifting
    apart is answered with a 400 that reads as a protocol problem, not a caller bug.
    A default here is a decision for every caller;
    a caller that needs another value passes it, and this function never branches
    on who is calling.
    """
    payload: dict[str, Any] = {'jsonrpc': '2.0', 'method': method}
    if request_id is not None:
        payload['id'] = request_id
    params = dict(params or {})
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
        'User-Agent': user_agent,
    }
    if era == 'modern':
        params['_meta'] = modern_meta(client_name, version)
        headers['MCP-Protocol-Version'] = version
        headers['Mcp-Method'] = method
        if name:
            headers['Mcp-Name'] = name
    elif method != 'initialize':
        headers['MCP-Protocol-Version'] = LEGACY_VERSION
    if params:
        payload['params'] = params
    if key:
        headers['Authorization'] = f'Bearer {key}'
    return headers, json.dumps(payload).encode()


def reply(resp: Response, request_id: str | None) -> dict[str, Any] | None:
    """The JSON-RPC message answering `request_id`, from a JSON or SSE body."""
    if request_id is None:
        return None
    return next((m for m in messages(resp) if m.get('id') == request_id), None)


def messages(resp: Response) -> list[dict[str, Any]]:
    """Every JSON-RPC message in a response body, JSON or SSE."""
    text = resp.body.decode('utf-8', errors='replace')
    if not text.strip():
        return []
    if resp.headers.get('content-type', '').startswith('text/event-stream'):
        messages = []
        for event in text.replace('\r\n', '\n').split('\n\n'):
            data = '\n'.join(line[5:].lstrip() for line in event.split('\n') if line.startswith('data:'))
            if data:
                try:
                    messages.append(json.loads(data))
                except ValueError:
                    continue
        return [m for m in messages if isinstance(m, dict)]
    try:
        parsed = json.loads(text)
    except ValueError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    return [m for m in items if isinstance(m, dict)]
