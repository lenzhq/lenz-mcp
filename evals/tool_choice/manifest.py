"""What a given MCP client is actually served: instructions + model-visible tools.

ONE builder, used by the tool-choice eval and by the per-client manifest
goldens. Two builders would be two answers to "what
does the model see", and the whole point of both jobs is that there is one.

It drives a REAL REQUEST through the assembled app rather than calling
``mcp.list_tools()``, because the per-client tailoring does not live in the
lister: which client is asking decides whether the card tools carry ``_meta.ui``
and whether the card-only tools appear at all, and since the SDK 2.x migration
that tailoring runs in ``lenz_mcp.middleware``, over the SERIALIZED wire
dict, inside a request. Calling the low-level lister would quietly measure a
manifest no client receives — and so would reaching for
``FastMCP._mcp_server.request_handlers``, which 2.x removed.

So the client's identity goes where the server reads it, the request's own
User-Agent header, and what comes back is WIRE DICTS (``inputSchema``,
``_meta``) rather than SDK models. ``src/lenz_mcp/testing.py`` is the one
transport for that, shared with the card suite: a second one here would be a
manifest measured on a stack nobody serves.

That makes the eval a CHECKOUT tool: it is not meant to run inside a deployed
image -- it makes paid calls, and is run by a person before a submission.

App-only tools are excluded from ``tools``: a tool marked
``ui.visibility: ['app']`` is for a card, and the model is not supposed to see
it. They are returned separately as ``app_only`` so a caller can assert they are
absent rather than having to know the names.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

# The clients worth asking about, by the identity the server keys on
# (client.client_identity). Two OpenAI entries because one token covers two
# clients with different manifests, and one unmeasured suffix because that is
# the case a golden set would otherwise never cover.
CLIENTS: dict[str, str] = {
    'claude': 'Claude-User/1.0',
    'chatgpt': 'openai-mcp/1.0.0',
    'chatgpt-codex': 'openai-mcp/1.0.0 (Codex)',
    'openai-api': 'openai-mcp/1.0.0 (Responses API)',
    'sdk': 'python-httpx/0.28',
    'unknown-suffix': 'openai-mcp/1.0.0 (Something New)',
    # No User-Agent at all: what a client that sends none is served, and what
    # the wrapped identity read falls back to when it fails (mcp 2.x removes the
    # SDK internal it goes through). It must look like any other unknown client.
    'no-user-agent': '',
}


# The fields that steer a model's CHOICE of tool. The hash covers these and
# nothing else, deliberately — see Manifest.hash.
_CHOICE_FIELDS = ('name', 'title', 'description', 'input_schema')


@dataclass(frozen=True)
class Manifest:
    """What one client is served, and nothing about how it was obtained."""

    client: str
    user_agent: str
    instructions: str
    tools: list[dict[str, Any]]
    app_only: list[str]

    @property
    def tool_names(self) -> list[str]:
        return [tool['name'] for tool in self.tools]

    def hash(self) -> str:
        """A digest of the wording that steers tool CHOICE.

        Recorded beside every eval result so a result can be told apart from the
        wording it was measured against. It covers the instructions and each
        tool's name, title, description and schema — and deliberately NOT
        `annotations` or `meta_keys`, which a golden set wants but which do not
        steer choice: including them would invalidate a run's results on a
        change that cannot have altered them.
        """
        payload = json.dumps(
            {
                'instructions': self.instructions,
                'tools': [{k: tool[k] for k in _CHOICE_FIELDS} for tool in self.tools],
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _annotations(tool: dict[str, Any]) -> dict[str, Any]:
    annotations = tool.get('annotations') or {}
    if not isinstance(annotations, dict):
        return {}
    return {key: value for key, value in sorted(annotations.items()) if value is not None}


def _meta_keys(tool: dict[str, Any]) -> list[str]:
    """Every key path in the tool's `_meta`, flattened, values discarded.

    `ui.visibility` and `ui.resourceUri` read differently at a glance, and the
    difference decides whether a tool is the model's or only the card's — so
    the PATH is what a golden needs, and the value is what it must never hold.
    """

    def walk(value: Any, prefix: str = '') -> list[str]:
        if not isinstance(value, dict):
            return [prefix] if prefix else []
        paths: list[str] = []
        for key in sorted(value):
            path = f'{prefix}.{key}' if prefix else str(key)
            paths.extend(walk(value[key], path))
        return paths

    return walk(tool.get('_meta') or {})


def _is_app_only(tool: dict[str, Any]) -> bool:
    meta = tool.get('_meta') or {}
    ui = meta.get('ui') if isinstance(meta, dict) else None
    visibility = ui.get('visibility') if isinstance(ui, dict) else None
    return isinstance(visibility, list) and 'app' in visibility


def build(client: str = 'claude', *, user_agent: str | None = None) -> Manifest:
    """The manifest this client receives, from the code in this checkout."""
    from lenz_mcp.testing import assembled_app

    ua = user_agent if user_agent is not None else CLIENTS[client]
    # The card flag OFF, its default: the manifest measured is the one every
    # client is served whether or not a card renders. OAuth off so the
    # request needs no token exchange; the header is what identifies the client.
    with assembled_app(MCP_OAUTH_ENABLED=False, MCP_CARD_ENABLED=False) as harness:
        wire = harness.wire(user_agent=ua, authorization='Bearer lenz_eval')
        handshake = wire.initialize(client_name='lenz-tool-choice-eval')
        listed = wire.call('tools/list')['tools']
        # What a client is SHOWN, from the same handshake rather than from
        # `server.mcp.instructions`: the module attribute is what the code
        # holds, and this is what reached the client.
        instructions = handshake.get('instructions') or ''

    visible = [tool for tool in listed if not _is_app_only(tool)]
    return Manifest(
        client=client,
        user_agent=ua,
        instructions=instructions,
        tools=[
            {
                'name': tool['name'],
                'title': tool.get('title') or '',
                'description': tool.get('description') or '',
                'input_schema': tool.get('inputSchema') or {},
                # Model-adjacent: they steer a host's approval prompts rather
                # than the model's choice, and a wording diff should show them.
                'annotations': _annotations(tool),
                # KEY NAMES only, never values. ChatGPT's `_meta` carries the
                # user's city, region, timezone and coordinates; none of that
                # belongs anywhere near a committed golden file. Making a tool
                # app-only is itself a change to these keys, which is why they
                # are here.
                'meta_keys': _meta_keys(tool),
            }
            for tool in visible
        ],
        app_only=sorted(tool['name'] for tool in listed if _is_app_only(tool)),
    )
