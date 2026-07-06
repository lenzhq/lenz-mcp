#!/usr/bin/env bash
# Publish server.json to the official MCP Registry (registry.modelcontextprotocol.io).
#
# The registry is in preview and may reset its data — re-run this to re-publish.
# Namespace `io.lenz/*` is proven via a DNS TXT record on lenz.io:
#   v=MCPv1; k=ed25519; p=<base64 public key>
#
# Requires:
#   - mcp-publisher            (brew install mcp-publisher)
#   - the Ed25519 private key  (default: ~/lenz-mcp-registry.pem — keep OUT of this repo)
set -euo pipefail

KEY="${LENZ_MCP_REGISTRY_KEY:-$HOME/lenz-mcp-registry.pem}"
DOMAIN="lenz.io"

if [[ ! -f "$KEY" ]]; then
  echo "error: signing key not found at $KEY" >&2
  echo "  generate: openssl genpkey -algorithm Ed25519 -out $KEY" >&2
  exit 1
fi

cd "$(dirname "$0")"

echo "==> validating server.json"
mcp-publisher validate --file ./server.json

echo "==> logging in (DNS namespace: $DOMAIN)"
PRIVATE_KEY="$(openssl pkey -in "$KEY" -noout -text | grep -A3 'priv:' | tail -n +2 | tr -d ' :\n')"
mcp-publisher login dns --domain "$DOMAIN" --private-key "$PRIVATE_KEY"

echo "==> publishing"
mcp-publisher publish --file ./server.json

echo "==> live listing:"
curl -s "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.lenz" \
  | python3 -m json.tool
