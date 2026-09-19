"""Lenz remote MCP server.

A thin Model Context Protocol adapter over the existing public API. It does
NOT reimplement the fact-check pipeline — every tool forwards the caller's
``Authorization`` header to ``lenz.io/api/v1`` over HTTP, so auth, quota, and
usage logging all stay in one place (the public API).

The MCP is an awareness/discovery surface: branded ``/c/<slug>`` links on
results that resolve to already-public claims carry ``utm_medium=mcp`` back to
Lenz. See ``src/lenz_mcp/server.py`` for the tool contract.

Served via Streamable HTTP at ``FRONTEND_URL/mcp`` (a separate ASGI service,
``uvicorn lenz_mcp.asgi:application``).
"""
