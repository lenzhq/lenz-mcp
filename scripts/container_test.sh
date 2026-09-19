#!/usr/bin/env bash
# The server's container test: build the image from a commit exactly as a
# release does (a `git archive` of the repository and the LENZ_MCP_VERSION build
# argument), run it against a stub API, and run the smoke (scripts/smoke.py)
# against it on both protocol eras:
#   - OAuth off, with a key: health, GET refused, tools/list and check_usage on
#     both eras (the key reaches the stub API through the server);
#   - OAuth on, keyless: the 401 challenge that starts OAuth discovery, both eras.
# Runs in CI and on a Mac with Docker Desktop.
#
#   bash scripts/container_test.sh [<git-ref>]    (default HEAD)
#
# LENZ_MCP_VERSION sets the version the image is built with (default
# 0.0.0-test); the release sets it to the tag.
set -euo pipefail

REF="${1:-HEAD}"
VERSION="${LENZ_MCP_VERSION:-0.0.0-test}"
VERSION="${VERSION#v}"
ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"
WORK="$(mktemp -d)"
TAG="lenz-mcp-container-test:$$"
KEY="container-test-key-$$"
STUB_PORT=18731
PORT_OFF=18732
PORT_ON=18733
STUB_PID=""
cleanup() {
    docker rm -f lenz-mcp-ct-off lenz-mcp-ct-on >/dev/null 2>&1 || true
    if [ -n "${STUB_PID}" ]; then kill "${STUB_PID}" 2>/dev/null || true; fi
    rm -rf "${WORK}"
}
trap cleanup EXIT

echo "==> Build context: git archive ${REF}"
git archive "${REF}" | tar -x -C "${WORK}"
docker build -q --build-arg "LENZ_MCP_VERSION=${VERSION}" -t "${TAG}" "${WORK}" >/dev/null

python3 scripts/stub_api.py "${STUB_PORT}" "${KEY}" &
STUB_PID=$!
disown "${STUB_PID}"

# The container reaches the stub on the host: host networking on Linux (CI),
# host.docker.internal with a published port on Docker Desktop.
run() {  # name host-port oauth
    local name="$1" port="$2" oauth="$3"
    local net=() api_host listen
    if [ "$(uname -s)" = "Linux" ]; then
        net=(--network host)
        api_host="127.0.0.1"
        listen="${port}"
    else
        net=(-p "${port}:8080")
        api_host="host.docker.internal"
        listen=8080
    fi
    docker run -d --name "${name}" "${net[@]}" \
        -e PORT="${listen}" \
        -e MCP_API_BASE_URL="http://${api_host}:${STUB_PORT}/api/v1" \
        -e MCP_ALLOWED_HOSTS= \
        -e MCP_OAUTH_ENABLED="${oauth}" \
        -e WORKOS_AUTHKIT_DOMAIN=container-test.authkit.app \
        -e MCP_PUBLIC_URL="http://127.0.0.1:${port}/mcp" \
        "${TAG}" >/dev/null
}
run lenz-mcp-ct-off "${PORT_OFF}" False
run lenz-mcp-ct-on "${PORT_ON}" True

for port in "${PORT_OFF}" "${PORT_ON}"; do
    curl -fsS --retry 30 --retry-all-errors --retry-delay 1 "http://127.0.0.1:${port}/mcp/healthz" >/dev/null
done

if [ "$(docker exec lenz-mcp-ct-off id -u)" != "10001" ]; then
    echo "FAIL: the container does not run as the unprivileged user"
    exit 1
fi
reported="$(docker exec lenz-mcp-ct-off python -c 'from lenz_mcp import config; print(config.APP_VERSION)')"
if [ "${reported}" = "dev" ] || [ "${reported}" != "${VERSION}" ]; then
    echo "FAIL: the image reports version '${reported}', not the '${VERSION}' it was built with"
    exit 1
fi

# Every module imports inside the image, including the ones only a live request
# reaches (the OAuth bridge's signer, the card resources), and an OAuth bridge
# assertion can be minted there: the keyless OAuth smoke below never reaches
# either, so a missing dependency would pass it.
docker exec -e MCP_SERVICE_SIGNING_KEY=container-test-signing-key -e MCP_SERVICE_SIGNING_KEYS=container-test-signing-key-0123456789abcdef \
    lenz-mcp-ct-on python -c "
import importlib, pkgutil, lenz_mcp
for m in pkgutil.walk_packages(lenz_mcp.__path__, 'lenz_mcp.'):
    importlib.import_module(m.name)
from lenz_mcp.bridge import mint_service_assertion
assert mint_service_assertion(1)
print('server modules import, and the bridge mints')
"

status=0
LENZ_API_KEY="${KEY}" PYTHONPATH=src python3 scripts/smoke.py --base-url "http://127.0.0.1:${PORT_OFF}" --oauth off --attempts 1 || status=1
PYTHONPATH=src python3 scripts/smoke.py --base-url "http://127.0.0.1:${PORT_ON}" --oauth on --keyless --attempts 1 || status=1
if [ "${status}" != 0 ]; then
    echo "==> container logs (OAuth off)"
    docker logs lenz-mcp-ct-off 2>&1 | tail -40
    echo "==> container logs (OAuth on)"
    docker logs lenz-mcp-ct-on 2>&1 | tail -40
fi
exit "${status}"
