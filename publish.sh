#!/usr/bin/env bash
# Publish the Lenz MCP server to the official MCP Registry
# (registry.modelcontextprotocol.io) under the io.lenz/* namespace (DNS-authed).
#
# (Note: Smithery shut down its proxy, API, accounts, and CLI on 2026-10-31;
# its public directory at smithery.ai is now static/managed by Arcade.dev and
# can no longer be updated via CLI or API.)
#
# Run this after deploying an MCP server change so the registry re-reads server.json.
#
# Usage:
#   ./publish.sh
#
# Requires:
#   mcp-publisher (brew install mcp-publisher) + the Ed25519 signing key at
#   LENZ_MCP_REGISTRY_KEY (default $HOME/.config/lenz-mcp/registry.pem;
#   keep it OUT of this repo).
set -euo pipefail

# ── config ───────────────────────────────────────────────────────────
SERVER_URL="https://lenz.io/mcp"

# registry
REGISTRY_KEY="${LENZ_MCP_REGISTRY_KEY:-${XDG_CONFIG_HOME:-$HOME/.config}/lenz-mcp/registry.pem}"
REGISTRY_DOMAIN="lenz.io"

# ── args ─────────────────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg (see --help)" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")"

# ── registry ─────────────────────────────────────────────────────────
if [[ ! -f "$REGISTRY_KEY" ]]; then
  echo "error: signing key not found at $REGISTRY_KEY" >&2
  echo "  generate: openssl genpkey -algorithm Ed25519 -out $REGISTRY_KEY" >&2
  exit 1
fi
echo "==> [registry] validating server.json"
mcp-publisher validate --file ./server.json

echo "==> [registry] logging in (DNS namespace: $REGISTRY_DOMAIN)"
priv="$(openssl pkey -in "$REGISTRY_KEY" -noout -text | grep -A3 'priv:' | tail -n +2 | tr -d ' :\n')"
mcp-publisher login dns --domain "$REGISTRY_DOMAIN" --private-key "$priv"

echo "==> [registry] publishing"
mcp-publisher publish --file ./server.json

echo "==> [registry] live listing:"
curl -s "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.lenz&version=latest" \
  | python3 -m json.tool

echo "==> done"
