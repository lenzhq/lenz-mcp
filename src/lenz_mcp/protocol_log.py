"""One log line per request to the MCP endpoint: protocol version, what the client
declares, and what we answered.

A client probing a protocol version this server does not serve is answered by the
SDK's transport, with a 400, before any handler of ours runs — and then it falls
back. Nothing logged inside the server ever sees those requests, so without this
the only clients we could measure would be the ones already working. This wrapper
sits OUTSIDE the SDK app and records, for every request to the endpoint (rejected
ones included):

- ``header_version`` / ``header_method``: the ``mcp-protocol-version`` and
  ``mcp-method`` headers;
- ``era_requested``: ``modern`` when the client ASKED for the 2026-07-28
  revision or later (a version in the header, in ``initialize`` or in
  ``_meta``, an ``mcp-method`` header, which only that revision defines, or a
  modern-only method such as ``server/discover``), else
  ``legacy``. It is what was asked for, never what was served: a probe the
  pinned SDK rejects is still ``modern``, and ``status`` says whether it worked;
- ``rpc``: the JSON-RPC method(s), and ``tool`` for a ``tools/call``;
- for ``initialize`` (the handshake every 2025-era client opens with):
  ``init_version``, ``client`` and the declared ``capabilities``;
- for a modern request, which has no handshake: ``meta_version``, and ``client``
  and ``capabilities`` read from the ``io.modelcontextprotocol/*`` keys of
  ``params._meta``;
- ``capabilities`` / ``meta``: the keys under ``params.capabilities`` and
  ``params._meta`` on ANY request, one level deep, so a client that declares
  what it supports per request rather than at the handshake shows up too;
- ``status``: the HTTP status we answered.

It observes only, and never reads ahead of the app: a request the auth layer
rejects is rejected before its body is read, as it would be without this
wrapper. The body is sampled as the app reads it (at most `_MAX_PARSE_BYTES`),
and a body that was not read whole within that cap is not parsed, so its line
carries the header fields only. The response is untouched. In particular it
never answers a version probe itself — advertising a protocol the server does
not implement would make discovery succeed and the calls after it fail.

Nothing a client sends is logged verbatim: tool arguments never, and every
logged value is reduced to a short token that cannot carry a space, an ``=`` or
a line break, so no value can forge a field or a second record. The logger is
configured at INFO (``observability.configure_logging``), since a deployment's
floor for app loggers is usually WARNING.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# What a logged token may contain. Deliberately excludes whitespace and `=`,
# which delimit the line's fields.
_TOKEN_UNSAFE = re.compile(r'[^A-Za-z0-9._/:@+(),;-]')
_MAX_TOKEN_CHARS = 80
_MAX_ITEMS = 20
# Larger bodies still pass through untouched; they are just not parsed.
_MAX_PARSE_BYTES = 256_000

# The first revision without `initialize`: its clients declare version, identity
# and capabilities in each request's `_meta` under these keys.
_FIRST_MODERN_VERSION = '2026-07-28'
_VERSION_DATE = re.compile(r'[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])')
_META_VERSION = 'io.modelcontextprotocol/protocolVersion'
_META_CLIENT = 'io.modelcontextprotocol/clientInfo'
_META_CAPABILITIES = 'io.modelcontextprotocol/clientCapabilities'
# Methods only a modern client sends, so they place a request with no version on it.
_MODERN_ONLY_METHODS = frozenset({'server/discover', 'subscriptions/listen'})


def log_token(value: Any) -> str:
    """One client-controlled value, safe to put in a log line.

    Public because it is no longer only this module's: anything a client sends
    that reaches a log line goes through it (`src/lenz_mcp/middleware.py` logs the
    URI a `resources/read` asked for). A raw value there is a log-forging hole —
    a newline in a `ui://` URI writes a second line, and a reader has no way to
    tell it from one we emitted. Strips everything outside the token alphabet,
    whitespace and `=` included, and bounds the length.
    """
    text = _TOKEN_UNSAFE.sub('', value if isinstance(value, str) else str(value))[:_MAX_TOKEN_CHARS]
    return text or '-'


def _keys(mapping: Any) -> set[str]:
    """Top-level keys of a declared-capabilities dict, plus their own keys one level down."""
    if not isinstance(mapping, dict):
        return set()
    keys = set()
    for key, value in list(mapping.items())[:_MAX_ITEMS]:
        name = log_token(key)
        keys.add(name)
        if isinstance(value, dict):
            keys.update(f'{name}.{log_token(sub)}' for sub in list(value)[:_MAX_ITEMS])
    return keys


def _joined(items: list[str] | set[str]) -> str:
    # `,` separates the items, so an item may not carry one.
    ordered = sorted(items) if isinstance(items, set) else items
    return ','.join(item.replace(',', '') for item in ordered[:_MAX_ITEMS]) or '-'


def _version_date(value: Any) -> str | None:
    """The value when it is a protocol version (an ASCII calendar date), else None.

    Versions are dates, so they compare correctly as text. Anything else a client
    sends (a number, `latest`, non-ASCII digits) is not a version we can place."""
    return value if isinstance(value, str) and _VERSION_DATE.fullmatch(value) else None


def _era_requested(header_version: str, meta_version: Any, header_method: str, methods: list[Any]) -> str:
    """The protocol era the client ASKED for, strongest signal first.

    A stated version decides: the header, else the first ``_meta`` version. Only
    with neither does a weaker signal count: an ``mcp-method`` header (only the
    2026-07-28 transport defines it) or a method only that revision has. A
    dual-era client that fell back to 2025-11-25 may keep sending ``Mcp-Method``,
    and its calls are legacy. ``initialize`` never decides: the modern revision
    has none, so offering a newer version in one is a legacy handshake the server
    negotiates down (its offer is in ``init_version``)."""
    for version in (header_version, meta_version):
        stated = _version_date(version)
        if stated:
            return 'modern' if stated >= _FIRST_MODERN_VERSION else 'legacy'
    if header_method.strip() or any(method in _MODERN_ONLY_METHODS for method in methods):
        return 'modern'
    return 'legacy'


def _messages(body: bytes) -> list[dict[str, Any]]:
    if not body or len(body) > _MAX_PARSE_BYTES:
        return []
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):  # deep nesting raises RecursionError
        return []
    batch = parsed if isinstance(parsed, list) else [parsed]
    return [m for m in batch[:_MAX_ITEMS] if isinstance(m, dict)]


def _describe(scope: dict[str, Any], body: bytes, status: int | None, gave_up: str = '') -> str:
    headers: dict[str, str] = {}
    for key, value in scope.get('headers') or []:
        # First value wins for a repeated header, as Starlette's `Headers.get` (and so the SDK) reads it.
        headers.setdefault(key.decode('latin-1').lower(), value.decode('latin-1'))
    methods: list[str] = []
    raw_methods: list[Any] = []
    tools: list[str] = []
    capabilities: set[str] = set()
    meta: set[str] = set()
    init_version = client = meta_version = meta_client = '-'
    raw_meta_version: Any = None
    header_version = headers.get('mcp-protocol-version', '')
    header_method = headers.get('mcp-method', '')
    for message in _messages(body):
        method = message.get('method')
        raw_params = message.get('params')
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        raw_declared = params.get('_meta')
        declared: dict[str, Any] = raw_declared if isinstance(raw_declared, dict) else {}
        if isinstance(method, str):
            methods.append(log_token(method))
            raw_methods.append(method)
        if method == 'initialize':
            init_version = log_token(params.get('protocolVersion', ''))
            client = _client(params.get('clientInfo'), client)
        if method == 'tools/call' and isinstance(params.get('name'), str):
            tools.append(log_token(params['name']))
        if _META_VERSION in declared and raw_meta_version is None:  # the first one seen, like `client`
            raw_meta_version = declared[_META_VERSION]
            meta_version = log_token(raw_meta_version)
        meta_client = _client(declared.get(_META_CLIENT), meta_client)
        capabilities |= _keys(params.get('capabilities'))
        capabilities |= _keys(declared.get(_META_CAPABILITIES))
        meta |= _keys(declared)
    fields = {
        'http_method': log_token(scope.get('method', '')),
        'status': log_token(status) if status is not None else '-',
        'header_version': log_token(header_version),
        'header_method': log_token(header_method),
        'era_requested': _era_requested(header_version, raw_meta_version, header_method, raw_methods),
        'rpc': _joined(methods),
        'tool': _joined(tools),
        'init_version': init_version,
        'meta_version': meta_version,
        'capabilities': _joined(capabilities),
        'meta': _joined(meta),
        # The handshake's identity when there is one, else the one a modern request carries.
        'client': client if client != '-' else meta_client,
        # Whether the caller was still there when we answered. Its own field,
        # never a value of `status`: a `status` that is sometimes an HTTP code
        # and sometimes a word breaks every filter already written against it,
        # and the two facts are independent — a dropped call still has whatever
        # status we had reached.
        'gave_up': gave_up or '-',
        'ua': log_token(headers.get('user-agent', '')),
    }
    return 'mcp_protocol ' + ' '.join(f'{key}={value}' for key, value in fields.items())


def _client(info: Any, current: str) -> str:
    """`name/version` from a clientInfo object, keeping the first one seen."""
    if current != '-' or not isinstance(info, dict):
        return current
    return f'{log_token(info.get("name", ""))}/{log_token(info.get("version", ""))}'


class ProtocolLog:
    """ASGI wrapper that logs every request to the MCP endpoint. See the module docstring."""

    def __init__(self, app, *, path: str):
        self._app = app
        bare = path.rstrip('/') or '/'
        self._paths = frozenset({bare, bare + '/'})

    def __getattr__(self, name):
        # A transparent wrapper, like ServerStreamGuard: attribute reads reach the app inside.
        return getattr(self._app, name)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get('type') != 'http' or scope.get('path') not in self._paths:
            await self._app(scope, receive, send)
            return

        # Never read ahead of the app: a request the auth
        # layer rejects must be rejected before its body is read, exactly as
        # without this wrapper. So the body is only OBSERVED as the app reads
        # it, and at most _MAX_PARSE_BYTES of it is kept.
        sample = bytearray()
        complete = False
        truncated = False
        # Two distinct ways a caller leaves, each flagged on the line. Without
        # the flags one would read as `status=-`, which is also what a failure
        # before the response start looks like, and the other as `status=200`,
        # which is indistinguishable from a successful call.
        disconnected = False
        write_failed = False

        async def _receive():
            nonlocal complete, truncated, disconnected
            message = await receive()
            if message.get('type') == 'http.disconnect':
                disconnected = True
            if message.get('type') == 'http.request':
                chunk = message.get('body', b'')
                room = _MAX_PARSE_BYTES - len(sample)
                if len(chunk) > room:
                    truncated = True
                sample.extend(chunk[: max(room, 0)])
                if not message.get('more_body'):
                    complete = True
            return message

        status: int | None = None

        async def _send(message):
            nonlocal status, write_failed
            if message.get('type') == 'http.response.start':
                status = message.get('status')
            try:
                await send(message)
            except BaseException:
                # Note and re-raise, untouched: the server needs this exception
                # to tear the connection down, and an observer that swallowed it
                # would change behaviour rather than record it.
                write_failed = True
                raise

        try:
            await self._app(scope, _receive, _send)
        finally:
            # A write that failed says the caller went during the answer. A
            # disconnect with no answer begun says it went before one. A
            # disconnect AFTER a complete response is ordinary and says
            # nothing, which is why this is not simply `disconnected`.
            if write_failed:
                gave_up = 'mid_response'
            elif disconnected and status is None:
                gave_up = 'before_answer'
            else:
                gave_up = ''
            _log_request(scope, bytes(sample) if complete and not truncated else b'', status, gave_up)


# Logging failures swallowed since the process started. A measurement must never
# fail or slow a request; the count keeps a silently broken log line findable.
log_failures = 0


def _log_request(scope: dict[str, Any], body: bytes, status: int | None, gave_up: str = '') -> None:
    """Write the line. The body is empty unless the app read all of it within the cap:
    a cut JSON document is never parsed, and the header fields are logged regardless."""
    global log_failures
    try:
        logger.info('%s', _describe(scope, body, status, gave_up))
    except Exception:  # noqa: BLE001 — a measurement must never fail a request
        log_failures += 1
        try:
            logger.warning('mcp_protocol_log_failed count=%d', log_failures, exc_info=True)
        except Exception:  # noqa: BLE001
            pass
