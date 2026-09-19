#!/usr/bin/env bash
# Refresh the Lenz MCP listing across catalogs. Two independent targets:
#
#   registry  → the official MCP Registry (registry.modelcontextprotocol.io).
#               Publishes server.json under the io.lenz/* namespace (DNS-authed).
#               The registry is in preview and may reset — re-run to re-publish.
#
#   smithery  → smithery.ai. Re-scans the live server (refreshes the tool list /
#               security scan) and syncs the listing metadata (title, description,
#               icon, homepage, repo, license) so the dashboard fields are
#               reproducible from code rather than hand-edited.
#
# Run this after deploying an MCP server change so the catalogs re-read the live
# server. By default it does both targets; skip either with the flags below.
#
# Usage:
#   ./publish.sh                 # both targets
#   ./publish.sh --skip-smithery # registry only
#   ./publish.sh --skip-registry # smithery only
#
# Requires:
#   registry:  mcp-publisher (brew install mcp-publisher) + the Ed25519 key, at
#              LENZ_MCP_REGISTRY_KEY (default $HOME/.config/lenz-mcp/registry.pem;
#              keep it OUT of this repo)
#   smithery:  a prior `npx @smithery/cli mcp publish` login for the re-scan,
#              and an API key for the metadata sync: SMITHERY_API_KEY, or else
#              the key the Smithery CLI stored at login. The re-scan warns and
#              carries on if it fails; the metadata sync fails the run when it
#              has no key or the API refuses it, so a publish that left the
#              listing's title, description or icon stale is never reported as
#              a success.
set -euo pipefail

# ── config ───────────────────────────────────────────────────────────
SERVER_URL="https://lenz.io/mcp"

# registry
REGISTRY_KEY="${LENZ_MCP_REGISTRY_KEY:-${XDG_CONFIG_HOME:-$HOME/.config}/lenz-mcp/registry.pem}"
REGISTRY_DOMAIN="lenz.io"

# smithery
SMITHERY_NAME="lenz/fact-check"                 # <namespace>/<server>
SMITHERY_DISPLAY_NAME="Lenz Fact-Check"
SMITHERY_ICON_URL="https://lenz.io/lenz-wordmark-512.png"
SMITHERY_HOMEPAGE="https://lenz.io/mcp-server"
SMITHERY_REPO_URL="https://github.com/lenzhq/lenz-mcp"
SMITHERY_LICENSE="Apache-2.0"
SMITHERY_DESCRIPTION="Check the factual claims in a draft or an answer against independent sources. assess_claim is the quick check (about 15-20 seconds): a multi-model verdict (True → False) for each claim, with a bucketed confidence and usually the reviewers' reasoning. verify_claim is the deep check (about a minute to a minute and a half): research → debate → panel review, returning a score, the key finding, warnings and the sources with the quote behind each. ask_followup answers grounded follow-ups on a completed check, and list_verifications returns recent deep checks. Verdicts are directional, not absolute. Connect via OAuth or a free Lenz API key."

# ── args ─────────────────────────────────────────────────────────────
DO_REGISTRY=true
DO_SMITHERY=true
for arg in "$@"; do
  case "$arg" in
    --skip-registry) DO_REGISTRY=false ;;
    --skip-smithery) DO_SMITHERY=false ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg (see --help)" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")"

# ── registry ─────────────────────────────────────────────────────────
publish_registry() {
  if [[ ! -f "$REGISTRY_KEY" ]]; then
    echo "error: signing key not found at $REGISTRY_KEY" >&2
    echo "  generate: openssl genpkey -algorithm Ed25519 -out $REGISTRY_KEY" >&2
    exit 1
  fi
  echo "==> [registry] validating server.json"
  mcp-publisher validate --file ./server.json

  echo "==> [registry] logging in (DNS namespace: $REGISTRY_DOMAIN)"
  local priv
  priv="$(openssl pkey -in "$REGISTRY_KEY" -noout -text | grep -A3 'priv:' | tail -n +2 | tr -d ' :\n')"
  mcp-publisher login dns --domain "$REGISTRY_DOMAIN" --private-key "$priv"

  echo "==> [registry] publishing"
  mcp-publisher publish --file ./server.json

  echo "==> [registry] live listing:"
  curl -s "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.lenz&version=latest" \
    | python3 -m json.tool
}

# ── smithery ─────────────────────────────────────────────────────────
publish_smithery() {
  # (a) re-scan the live server (tool list + security scan). Needs a prior
  #     `smithery auth login`; non-fatal if not authed.
  echo "==> [smithery] re-scanning $SERVER_URL as $SMITHERY_NAME"
  if ! npx -y @smithery/cli@latest mcp publish "$SERVER_URL" -n "$SMITHERY_NAME"; then
    echo "   warn: smithery re-scan failed — run 'npx @smithery/cli mcp publish' once" >&2
    echo "         interactively to log in, then re-run. Skipping re-scan." >&2
  fi

  # (b) sync listing metadata via the registry PATCH API.
  local api_key
  api_key="$(smithery_api_key)"
  if [[ -z "$api_key" ]]; then
    echo "error: no Smithery API key: set SMITHERY_API_KEY, or log in with the" >&2
    echo "       Smithery CLI. The listing's title, description and icon were NOT synced." >&2
    exit 1
  fi
  echo "==> [smithery] syncing listing metadata"
  local body
  body="$(
    SMITHERY_DISPLAY_NAME="$SMITHERY_DISPLAY_NAME" \
    SMITHERY_DESCRIPTION="$SMITHERY_DESCRIPTION" \
    SMITHERY_ICON_URL="$SMITHERY_ICON_URL" \
    SMITHERY_HOMEPAGE="$SMITHERY_HOMEPAGE" \
    SMITHERY_REPO_URL="$SMITHERY_REPO_URL" \
    SMITHERY_LICENSE="$SMITHERY_LICENSE" \
    python3 -c 'import json,os; print(json.dumps({
      "displayName":  os.environ["SMITHERY_DISPLAY_NAME"],
      "description":  os.environ["SMITHERY_DESCRIPTION"],
      "iconUrl":      os.environ["SMITHERY_ICON_URL"],
      "homepage":     os.environ["SMITHERY_HOMEPAGE"],
      "repositoryUrl":os.environ["SMITHERY_REPO_URL"],
      "license":      os.environ["SMITHERY_LICENSE"],
    }))'
  )"
  local encoded_name="${SMITHERY_NAME/\//%2F}"
  local code
  code="$(curl -s -o /dev/stderr -w '%{http_code}' \
    -X PATCH "https://api.smithery.ai/servers/${encoded_name}" \
    -H "Authorization: Bearer ${api_key}" \
    -H "Content-Type: application/json" \
    -d "$body")"
  echo
  if [[ "$code" == 2* ]]; then
    echo "   metadata synced (HTTP $code)"
  else
    echo "error: the metadata sync returned HTTP $code; the listing was NOT updated." >&2
    exit 1
  fi
}

# The API key for the metadata sync: SMITHERY_API_KEY if set, else the key the
# Smithery CLI stored when you logged in (macOS and Linux locations). Prints
# nothing when there is none.
smithery_api_key() {
  if [[ -n "${SMITHERY_API_KEY:-}" ]]; then
    printf '%s' "$SMITHERY_API_KEY"
    return
  fi
  local f
  for f in "$HOME/Library/Application Support/smithery/settings.json" \
           "${XDG_CONFIG_HOME:-$HOME/.config}/smithery/settings.json"; do
    if [[ -f "$f" ]]; then
      python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("apiKey") or "", end="")' "$f" 2>/dev/null && return 0
    fi
  done
  return 0
}

# ── run ──────────────────────────────────────────────────────────────
$DO_REGISTRY && publish_registry || true
$DO_SMITHERY && publish_smithery || true
echo "==> done"
