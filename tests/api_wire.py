"""The public API on the wire, for tests: an ``httpx.MockTransport`` behind the
connector's own shared HTTP client.

Every request the client sends goes through the real code path (the SDK, our
headers, the shared ``httpx.AsyncClient`` that ``client._get_client`` builds) and
is recorded; the answer comes from a handler the test sets. Nothing reaches a
network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from lenz_mcp import client, config

Handler = Callable[[httpx.Request], httpx.Response]

#: The real class, captured at import: a second `install` in one test must not
#: wrap the first one's factory.
_REAL_ASYNC_CLIENT = httpx.AsyncClient

#: The version every answer of the served API carries.
VERSION_HEADERS = {config.API_VERSION_HEADER: config.API_VERSION}


def answer(status: int = 200, body: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    """A JSON answer in the API version the connector reads."""
    return httpx.Response(status, json={} if body is None else body, headers={**VERSION_HEADERS, **(headers or {})})


def default_answer(request: httpx.Request) -> httpx.Response:
    """A plausible success for each endpoint the connector calls."""
    path = request.url.path.removeprefix('/api/v1')
    if path == '/assess':
        return answer(200, {'status': 'ok', 'claims': [], 'failure': None, 'more_claims': []})
    if path == '/verify':
        return answer(202, {'task_id': 'a' * 32, 'status': 'queued'})
    if path.startswith('/verify/status/'):
        return answer(200, {'task_id': path.rsplit('/', 1)[-1], 'status': 'processing'})
    if path.endswith('/select'):
        return answer(202, {'status': 'queued', 'items': []})
    if path.startswith('/ask/'):
        return answer(200, {'role': 'expert', 'content': 'An answer.'})
    if path == '/citecheck':
        return answer(202, {'citecheck_id': 'ab12cd34', 'status': 'queued'})
    if path.startswith('/citechecks/'):
        return answer(200, {'citecheck_id': path.rsplit('/', 1)[-1], 'status': 'checking'})
    if path == '/me/usage':
        return answer(200, {'tier': 'free', 'credits': {'remaining': 10}})
    if path == '/verifications':
        return answer(200, {'items': [], 'total': 0, 'page': 1, 'page_size': 10})
    if path.startswith('/verifications/'):
        return answer(200, {'verification_id': path.rsplit('/', 1)[-1], 'claim': 'c', 'score': 5})
    return httpx.Response(404, json={'detail': 'Not found.', 'code': 'not_found'}, headers=VERSION_HEADERS)


class Wire:
    """The recorded requests, and the handler that answers them."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Handler = default_answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def respond(self, response: httpx.Response | Handler) -> None:
        """Answer every request with a copy of ``response``, or let a handler decide."""
        if isinstance(response, httpx.Response):
            template = response
            self.handler = lambda _request: httpx.Response(
                template.status_code, headers=template.headers, content=template.content
            )
        else:
            self.handler = response

    @property
    def last(self) -> httpx.Request:
        assert self.requests, 'no request was sent'
        return self.requests[-1]

    def body(self, request: httpx.Request | None = None) -> Any:
        content = (request or self.last).content
        return json.loads(content) if content else None


def install(monkeypatch: pytest.MonkeyPatch) -> Wire:
    """Route the connector's shared HTTP client through a recording MockTransport.

    The client is still BUILT by ``client._get_client`` (its default headers,
    limits and timeout are the production ones); only its transport is replaced.
    """
    wire = Wire()

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop('transport', None)
        return _REAL_ASYNC_CLIENT(*args, transport=httpx.MockTransport(wire), **kwargs)

    monkeypatch.setattr(client.httpx, 'AsyncClient', _factory)
    monkeypatch.setattr(client, '_http_client', None)
    return wire
