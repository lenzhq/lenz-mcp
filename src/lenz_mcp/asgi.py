"""ASGI entrypoint for the Lenz MCP service.

Run with: ``uvicorn lenz_mcp.asgi:application``. The connector is a plain API
client: its configuration comes from the environment
(``src/lenz_mcp/config.py``), it never touches a database, it reaches the
fact-check API over HTTP and builds branded links by string.
"""

import json

from lenz_mcp import observability

# Before anything logs: the protocol and auth lines are the service's signals.
observability.configure_logging()
observability.init_sentry()

from lenz_mcp import config  # noqa: E402
from lenz_mcp.protocol_log import ProtocolLog  # noqa: E402
from lenz_mcp.server import build_transport_security, mcp  # noqa: E402


class ServerStreamGuard:
    """Answer ``GET /mcp`` with 405 while the server runs stateless.

    Streamable HTTP lets a client open a standalone SSE stream with ``GET`` so
    the server can push it requests/notifications. The SDK's GET handler opens
    that stream unconditionally — it never checks ``stateless_http``. But a
    stateless server builds a fresh transport per request, so nothing is ever
    routed to that stream, and the SDK emits no keepalive on it either.

    Such a connection would sit silent until the hosting platform's request
    timeout cut it. Clients read that as a dropped connection, and it would not
    show up as an error rate because the 200 response line is already on the
    wire; only the body is cut. Each idle stream would also hold a concurrency
    slot until the timeout.

    The spec's stated alternative for a server that offers no such stream is to
    answer ``GET`` with 405, so clients skip it rather than opening one that is
    guaranteed to die. Scoped to the protocol endpoint itself — ``/mcp/healthz``
    (the liveness route under the same prefix) must stay reachable.
    """

    #: Answered to a browser that navigates to the endpoint. See ``_is_browser_visit``.
    _REDIRECT_BODY = b''

    #: JSON-RPC error body, matching what the SDK returns for an unsupported method.
    _BODY = json.dumps(
        {
            'jsonrpc': '2.0',
            'id': 'server-error',
            'error': {'code': -32600, 'message': 'Method Not Allowed: server-initiated stream not offered'},
        }
    ).encode()

    def __init__(self, app, *, path: str, enabled: bool):
        self._app = app
        #: Public so a test (and a future reader) can assert the guard tracks
        #: stateless mode rather than being unconditionally on.
        self.reject_server_stream = enabled
        bare = path.rstrip('/') or '/'
        self._paths = frozenset({bare, bare + '/'})

    def __getattr__(self, name):
        # Stay a transparent wrapper: uvicorn only needs the ASGI callable, but
        # `.routes` / `.add_middleware` etc. should keep resolving to the real
        # Starlette app for anything that introspects the entrypoint.
        return getattr(self._app, name)

    @staticmethod
    def _is_browser_visit(scope) -> bool:
        """True for a person who pasted the address into a browser's location bar.

        A browser navigation asks for ``text/html``; an MCP client opening the
        server stream asks for ``text/event-stream`` (and ``application/json``
        on its POSTs). So the test is: the Accept header names ``text/html``
        and does not name ``text/event-stream``. A bare ``*/*`` (curl, most HTTP
        libraries) is not a browser. This only ever changes the answer to a GET
        that was already going to be refused with 405 below, so no client that
        works today can see a difference.
        """
        accept = b''
        for name, value in scope.get('headers') or []:
            if name == b'accept':
                accept = value.lower()
                break
        return b'text/html' in accept and b'text/event-stream' not in accept

    async def __call__(self, scope, receive, send) -> None:
        if (
            self.reject_server_stream
            and scope['type'] == 'http'
            and scope['method'] == 'GET'
            and scope['path'] in self._paths
        ):
            if self._is_browser_visit(scope):
                # 302, not 301: a browser caches a permanent redirect for good,
                # and the page it points at may move. `vary` keeps any cache
                # from handing this redirect to a client.
                await send(
                    {
                        'type': 'http.response.start',
                        'status': 302,
                        'headers': [
                            (b'location', config.CONNECT_PAGE_URL.encode()),
                            (b'cache-control', b'no-store'),
                            (b'vary', b'Accept'),
                            (b'content-length', b'0'),
                        ],
                    }
                )
                await send({'type': 'http.response.body', 'body': self._REDIRECT_BODY})
                return
            await send(
                {
                    'type': 'http.response.start',
                    'status': 405,
                    'headers': [
                        (b'content-type', b'application/json'),
                        (b'allow', b'POST'),
                    ],
                }
            )
            await send({'type': 'http.response.body', 'body': self._BODY})
            return
        await self._app(scope, receive, send)


# The path the protocol is served on, and the mode the service runs in. Constants here because 2.x removed `mcp.settings.streamable_http_path` and
# `mcp.settings.stateless_http`: the builder takes them, and the guard and the
# protocol log below must be told the same values the builder was given.
MCP_PATH = '/mcp'
STATELESS = True

# Streamable HTTP Starlette app; serves the MCP endpoint at /mcp plus the /healthz
# probe. `stateless_http` and `transport_security` moved here from the server
# constructor in 2.x.
_app = mcp.streamable_http_app(
    streamable_http_path=MCP_PATH,
    stateless_http=STATELESS,
    transport_security=build_transport_security(config.ALLOWED_HOSTS),
)

if config.OAUTH_ENABLED:
    # Browser-based MCP clients (Inspector; possibly future web clients)
    # fetch the metadata + 401 responses cross-origin during OAuth
    # discovery. Tool access is still token-gated — CORS only lifts the
    # browser's read wall. Spike-proven with MCP Inspector + Claude Desktop.
    from starlette.middleware.cors import CORSMiddleware  # noqa: E402

    from lenz_mcp.oauth import install_unavailable_response  # noqa: E402

    # A JWKS outage is a retryable 503, never a 401 or a 500 (oauth.JWKSCache).
    install_unavailable_response(_app)

    _app.add_middleware(
        CORSMiddleware,
        allow_origins=['*'],
        allow_methods=['*'],
        allow_headers=['*'],
        expose_headers=['*'],
    )

# The guard short-circuits before routing, so a rejected GET costs nothing (and
# CORS, added above, still applies to everything that passes). The protocol log
# is outermost so it also records what the guard and the SDK reject.
application = ProtocolLog(
    ServerStreamGuard(_app, path=MCP_PATH, enabled=STATELESS),
    path=MCP_PATH,
)
