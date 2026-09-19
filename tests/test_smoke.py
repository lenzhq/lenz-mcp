"""scripts/smoke.py: the fail-closed smoke test for a running server.

The smoke is proven against the REAL server: `uvicorn lenz_mcp.asgi:application`
in a subprocess, with its outbound API calls pointed at a stub public API on a
local port (`scripts/stub_api.py`). A check that passes here passes for the
reason it claims to, over the same urllib path a real run uses. Transport-shape
edge cases the real server cannot be made to produce (a server older than our
newest protocol, a held stream) run against an injected fake transport.
"""

from __future__ import annotations

import ast
import http.client
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from lenz_mcp import wire
from scripts import smoke, stub_api

ROOT = Path(__file__).resolve().parents[1]
SMOKE_PATH = ROOT / 'scripts' / 'smoke.py'
SYSTEM_PYTHON = Path('/usr/bin/python3')

GOOD_KEY = 'lenz_smoke_good_key_0123456789'  # gitleaks:allow (a made-up test key)
BAD_KEY = 'lenz_smoke_revoked_key_0123456789'  # gitleaks:allow (a made-up test key)


# ── the script from a plain checkout ────────────────────────────────


def _bare_checkout(dest: Path) -> Path:
    """The tracked files of scripts/ and src/, copied as they stand in the working
    tree: no install, no virtualenv, nothing but what a clone carries."""
    listed = subprocess.run(  # noqa: S603
        ['git', '-C', str(ROOT), 'ls-files', '-z', 'scripts', 'src'],  # noqa: S607
        check=True,
        capture_output=True,
    ).stdout.decode()
    for rel in filter(None, listed.split('\0')):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, target)
    return dest


@pytest.mark.skipif(shutil.which('git') is None or not (ROOT / '.git').exists(), reason='not a git checkout')
def test_the_smoke_runs_from_a_plain_checkout(tmp_path):
    """`python3 scripts/smoke.py` from a clone, with nothing on PYTHONPATH.

    The package is not installed there, so the script has to find it under src/
    by itself. `-S` keeps site-packages off the path too, so an installed copy of
    the package cannot stand in for the checkout's.
    """
    checkout = _bare_checkout(tmp_path / 'checkout')
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    result = subprocess.run(  # noqa: S603
        [sys.executable, '-S', 'scripts/smoke.py', '--help'],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert 'ImportError' not in result.stderr, result.stderr
    assert 'ModuleNotFoundError' not in result.stderr, result.stderr
    assert 'It is not standalone' not in result.stderr, result.stderr
    assert 'LENZ_API_KEY' in result.stdout


# ── a stub public API and a real server ─────────────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture(scope='module')
def stub_api_url():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), stub_api.handler_for(GOOD_KEY))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()


def _serve_mcp(api_url: str, log_path: Path, *, oauth: bool):
    port = _free_port()
    env = {
        **os.environ,
        'PYTHONPATH': str(ROOT / 'src'),
        'MCP_API_BASE_URL': api_url,
        'MCP_ALLOWED_HOSTS': '',
        'MCP_OAUTH_ENABLED': 'True' if oauth else 'False',
        'WORKOS_AUTHKIT_DOMAIN': 'smoke-test.authkit.app' if oauth else '',
        'MCP_PUBLIC_URL': f'http://127.0.0.1:{port}/mcp',
        'SENTRY_DSN': '',
    }
    # Output to a file, never an unread pipe: uvicorn logs every request, and a
    # full pipe buffer blocks the server mid-response after a few dozen requests.
    log = log_path.open('wb')
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, '-m', 'uvicorn', 'lenz_mcp.asgi:application', '--host', '127.0.0.1', '--port', str(port)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    log.close()
    base = f'http://127.0.0.1:{port}'
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f'the server exited: {log_path.read_text(errors="replace")[-2000:]}')
        try:
            with urllib.request.urlopen(f'{base}/mcp/healthz', timeout=1) as resp:
                if resp.status == 200:
                    return proc, base
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError('the server did not come up within 60s')


@pytest.fixture(scope='module')
def mcp_server(stub_api_url, tmp_path_factory):
    proc, base = _serve_mcp(stub_api_url, tmp_path_factory.mktemp('mcp') / 'uvicorn.log', oauth=False)
    yield base
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope='module')
def mcp_server_oauth(stub_api_url, tmp_path_factory):
    proc, base = _serve_mcp(stub_api_url, tmp_path_factory.mktemp('mcp-oauth') / 'uvicorn.log', oauth=True)
    yield base
    proc.terminate()
    proc.wait(timeout=10)


def _run_smoke(*args, key: str | None = GOOD_KEY, timeout: int = 120):
    env = {k: v for k, v in os.environ.items() if k not in ('LENZ_API_KEY', 'PYTHONPATH')}
    env['MCP_SMOKE_ATTEMPTS'] = '1'
    if key is not None:
        env['LENZ_API_KEY'] = key
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SMOKE_PATH), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ── the smoke against the real server ───────────────────────────────


def test_a_healthy_server_passes_every_check(mcp_server):
    result = _run_smoke('--base-url', mcp_server, '--oauth', 'off')
    assert result.returncode == 0, result.stdout + result.stderr
    for check in ('healthz', 'get_refused', 'keyless_tools_list', 'initialize', 'tools_list', 'check_usage'):
        assert f'OK   [{check}]' in result.stdout
    for modern in ('modern_discover', 'modern_tools_list', 'modern_check_usage', 'modern_listen_refused'):
        assert f'OK   [{modern}]' in result.stdout
    assert GOOD_KEY not in result.stdout + result.stderr


def test_a_missing_key_is_a_configuration_error_never_a_skip(mcp_server):
    result = _run_smoke('--base-url', mcp_server, '--oauth', 'off', key=None)
    assert result.returncode == smoke.EXIT_CONFIG
    assert 'LENZ_API_KEY' in result.stdout + result.stderr


def test_a_rejected_key_fails_and_says_so(mcp_server):
    result = _run_smoke('--base-url', mcp_server, '--oauth', 'off', key=BAD_KEY)
    assert result.returncode == smoke.EXIT_FAILED
    assert 'FAIL [check_usage]' in result.stdout
    assert 'API key' in result.stdout
    assert BAD_KEY not in result.stdout + result.stderr


def test_preflight_checks_the_key_against_the_api_itself(stub_api_url):
    """Not through the MCP server: a server that loses the Authorization header also
    answers `auth_required`, and blaming the key for that would be the wrong verdict."""
    api = f'{stub_api_url}/api/v1'
    ok = _run_smoke('--base-url', 'http://127.0.0.1:9', '--api-base-url', api, '--oauth', 'off', '--preflight')
    assert ok.returncode == 0, ok.stdout
    bad = _run_smoke(
        '--base-url', 'http://127.0.0.1:9', '--api-base-url', api, '--oauth', 'off', '--preflight', key=BAD_KEY
    )
    assert bad.returncode == smoke.EXIT_KEY_REJECTED
    unreachable = _run_smoke(
        '--api-base-url', f'http://127.0.0.1:{_free_port()}/api/v1', '--oauth', 'off', '--preflight'
    )
    assert unreachable.returncode == smoke.EXIT_FAILED


def test_auth_required_from_the_mcp_is_an_ordinary_failure_not_a_key_verdict():
    def call(payload, _headers):
        structured = {'status': 'auth_required'}
        return _sse(200, _result(payload, {'content': [], 'isError': False, 'structuredContent': structured}))

    code, _ = _run_fake(lambda *a: _modern_ok(*a, overrides={'tools/call': call}))
    assert code == smoke.EXIT_FAILED


def test_the_modern_checks_run_through_against_a_real_server(mcp_server):
    """A successful discover obliges everything after it to work, and the smoke has
    no switch that lets a server off that hook."""
    result = _run_smoke('--base-url', mcp_server, '--oauth', 'off')
    assert result.returncode == 0, result.stdout
    assert 'OK   [modern_discover]' in result.stdout
    assert 'OK   [modern_check_usage]' in result.stdout


def test_the_wrong_oauth_expectation_fails(mcp_server):
    result = _run_smoke('--base-url', mcp_server, '--oauth', 'on')
    assert result.returncode == smoke.EXIT_FAILED
    assert 'FAIL [keyless_tools_list]' in result.stdout


def test_oauth_on_expects_the_401_challenge_and_still_serves_the_key(mcp_server_oauth):
    result = _run_smoke('--base-url', mcp_server_oauth, '--oauth', 'on')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'OK   [keyless_tools_list]' in result.stdout
    assert 'OK   [keyless_modern_discover]' in result.stdout
    assert 'OK   [check_usage]' in result.stdout


@pytest.mark.parametrize('server', ['mcp_server', 'mcp_server_oauth'])
def test_auto_oauth_detects_either_configuration(request, server):
    result = _run_smoke('--base-url', request.getfixturevalue(server), '--oauth', 'auto')
    assert result.returncode == 0, result.stdout
    assert 'OK   [keyless_tools_list]' in result.stdout


def test_keyless_mode_needs_no_key(mcp_server):
    result = _run_smoke('--base-url', mcp_server, '--oauth', 'off', '--keyless', key=None)
    assert result.returncode == 0, result.stdout
    assert 'check_usage' not in result.stdout


def test_an_unreachable_server_fails_within_the_timeout():
    port = _free_port()  # nothing listens
    started = time.monotonic()
    result = _run_smoke('--base-url', f'http://127.0.0.1:{port}', '--oauth', 'off', '--timeout', '2')
    assert result.returncode == smoke.EXIT_FAILED
    assert time.monotonic() - started < 60


class _PingForever(http.server.BaseHTTPRequestHandler):
    """An SSE stream that never ends and never goes quiet long enough for a
    socket timeout: an idle stream held open, with the SDK's heartbeats."""

    protocol_version = 'HTTP/1.1'

    def _stream(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        try:
            while True:
                self.wfile.write(b': ping\r\n\r\n')
                self.wfile.flush()
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self):  # noqa: N802
        if self.path.endswith('/healthz'):
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._stream()

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        self._stream()

    def log_message(self, *_args):
        pass


def test_a_stream_that_pings_forever_fails_within_the_deadline():
    """A socket timeout never fires on a stream that keeps sending heartbeats, so
    every request needs a wall-clock deadline, or the smoke hangs until the host
    cuts the stream."""
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _PingForever)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        started = time.monotonic()
        result = _run_smoke(
            '--base-url', f'http://127.0.0.1:{server.server_address[1]}', '--oauth', 'off', '--timeout', '2'
        )
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
    assert result.returncode == smoke.EXIT_FAILED
    assert 'FAIL [get_refused]' in result.stdout
    assert elapsed < 30, result.stdout


@pytest.mark.skipif(not SYSTEM_PYTHON.exists(), reason='no system python3 on this machine')
def test_the_smoke_runs_on_the_system_python(mcp_server):
    """The script promises to run on a plain system `python3`, which on macOS is
    3.9: a 3.10+ expression evaluated at runtime would crash it there."""
    env = {**os.environ, 'LENZ_API_KEY': GOOD_KEY, 'MCP_SMOKE_ATTEMPTS': '1'}
    env.pop('PYTHONPATH', None)
    result = subprocess.run(  # noqa: S603
        [str(SYSTEM_PYTHON), str(SMOKE_PATH), '--base-url', mcp_server, '--oauth', 'off'],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# Derived from the imported module, not by counting `parents[...]` off the
# script: a wrong count would report a missing file as a grammar failure.
@pytest.mark.parametrize('path', [SMOKE_PATH, Path(wire.__file__)])
def test_the_smoke_and_its_wire_parse_under_the_3_9_grammar(path):
    """The script runs on Python 3.9; the test suite does not.

    So the runs that would catch a 3.10+ construct are exactly the runs a test
    machine without a 3.9 interpreter never makes: `match`, or a walrus in a
    comprehension, would be a SyntaxError on 3.9 and nowhere else. Parsing under
    the 3.9 grammar checks it everywhere. It does not replace running the script
    under 3.9; it covers the machines where that cannot happen.
    """
    ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))


@pytest.mark.skipif(not SYSTEM_PYTHON.exists(), reason='no system python3 on this machine')
def test_the_smoke_imports_its_wire_when_run_by_absolute_path(tmp_path, mcp_server):
    """Run by ABSOLUTE path, `sys.path[0]` is `scripts/`, not the repository root.

    The wire lives in the package, so `import lenz_mcp` from there fails unless
    the script puts `src/` on the path itself. Run from a cwd that is not the
    repository root, under a bare system python3 rather than the virtualenv,
    because a run from the root under `uv` could pass while the script was broken.
    """
    env = {**os.environ, 'LENZ_API_KEY': GOOD_KEY, 'MCP_SMOKE_ATTEMPTS': '1'}
    env.pop('PYTHONPATH', None)
    result = subprocess.run(  # noqa: S603
        [str(SYSTEM_PYTHON), str(SMOKE_PATH), '--base-url', mcp_server, '--oauth', 'off'],
        env=env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert 'ModuleNotFoundError' not in result.stderr, result.stderr
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_copy_of_the_smoke_on_its_own_says_why_it_cannot_run(tmp_path):
    """The script is not a single file you can put anywhere: it imports the wire
    from the package.

    The bare `ModuleNotFoundError: No module named 'lenz_mcp'` that a copy
    produces says nothing about why, so the script answers for itself.
    """
    # Copied to a directory with no `src/` beside it. The cwd this runs from does
    # not matter: for `python3 <script>`, `sys.path[0]` is the SCRIPT's directory
    # and the cwd is never added, so inheriting the repository root as cwd cannot
    # rescue the import.
    lonely = tmp_path / 'smoke.py'
    lonely.write_text(SMOKE_PATH.read_text())
    python = str(SYSTEM_PYTHON) if SYSTEM_PYTHON.exists() else sys.executable
    result = subprocess.run(  # noqa: S603
        [python, str(lonely), '--oauth', 'off'],
        env={**os.environ, 'PYTHONPATH': ''},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert 'It is not standalone' in result.stderr, result.stderr


# ── transport-shape cases, against a fake transport ─────────────────


class _Fake:
    """Answers requests from a routing function; records what was sent."""

    def __init__(self, route):
        self.route = route
        self.sent = []

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body) if body else None
        self.sent.append((method, url, dict(headers), payload))
        return self.route(method, url, headers, payload)


def _json(status, body, headers=None):
    return smoke.Response(status, {'content-type': 'application/json', **(headers or {})}, json.dumps(body).encode())


def _sse(status, *messages):
    body = ''.join(f'event: message\ndata: {json.dumps(m)}\n\n' for m in messages)
    return smoke.Response(status, {'content-type': 'text/event-stream'}, body.encode())


def _result(payload, result):
    return {'jsonrpc': '2.0', 'id': payload['id'], 'result': result}


_TOOLS = {'tools': [{'name': 'assess_claim'}, {'name': 'check_usage'}]}
_USAGE = {'content': [], 'isError': False, 'structuredContent': {'status': 'ok'}}


def _modern_ok(method, url, headers, payload, *, overrides=None):
    """A server that serves both eras correctly; `overrides` replaces one method's answer."""
    overrides = overrides or {}
    if url.endswith('/mcp/healthz'):
        return _json(200, {'status': 'ok'})
    if method == 'GET':
        return _json(405, {})
    rpc = payload['method']
    if rpc in overrides:
        return overrides[rpc](payload, headers)
    if rpc == 'notifications/initialized':
        return smoke.Response(202, {}, b'')
    modern = headers.get('MCP-Protocol-Version', '').startswith('2026')
    if 'Authorization' not in headers:
        return _json(200, _result(payload, _TOOLS)) if rpc == 'tools/list' else _json(401, {})
    if rpc == 'initialize':
        return _sse(200, _result(payload, {'protocolVersion': '2025-11-25', 'capabilities': {}}))
    if rpc == 'server/discover':
        return _json(
            200,
            _result(
                payload,
                {
                    'supportedVersions': ['2026-07-28'],
                    'capabilities': {'tools': {'listChanged': False}},
                    'resultType': 'complete',
                },
            ),
        )
    if rpc == 'subscriptions/listen':
        return _json(404, {'jsonrpc': '2.0', 'id': payload['id'], 'error': {'code': -32601, 'message': 'nf'}})
    extra = {'resultType': 'complete'} if modern else {}
    if rpc == 'tools/list':
        return _sse(200, _result(payload, {**_TOOLS, **extra}))
    if rpc == 'tools/call':
        return _sse(200, _result(payload, {**_USAGE, **extra}))
    raise AssertionError(rpc)


def _run_fake(route, *argv):
    fake = _Fake(route)
    argv = ('--attempts', '1', *argv)  # a later `--attempts` wins
    code = smoke.main(['--base-url', 'https://mcp.test', '--oauth', 'off', *argv], transport=fake, key=GOOD_KEY)
    return code, fake


def test_a_server_older_than_our_newest_protocol_still_passes(monkeypatch):
    """The case that lets the smoke require the modern era unconditionally: this
    checkout smoking a server built from an OLDER one.

    That server answers 400 to a protocol it has never heard of, so the smoke asks
    at each revision it knows, newest first, and then SPEAKS whatever discover
    settled on. Pinning one string here would fail a perfectly good older server
    on the day a newer protocol lands.
    """
    future = '2027-01-01'
    monkeypatch.setattr(smoke, 'MODERN_VERSIONS', (future, '2026-07-28'))

    def route(method, url, headers, payload, **kwargs):
        version = headers.get('MCP-Protocol-Version', '')
        if method == 'POST' and version == future:
            # As an older server answers a version it does not know.
            return _json(400, {'error': 'unsupported protocol version'})
        return _modern_ok(method, url, headers, payload, **kwargs)

    code, fake = _run_fake(route)
    assert code == 0, 'a server that predates our newest protocol must still pass'
    # `Mcp-Method` rides only on modern requests, so it separates them from the legacy
    # half of the run — which speaks 2025-11-25 and is unaffected by any of this.
    modern = [s for s in fake.sent if 'Mcp-Method' in s[2]]
    after_discover = {s[2]['MCP-Protocol-Version'] for s in modern if s[3].get('method') != 'server/discover'}
    assert after_discover == {'2026-07-28'}, f'the modern calls must speak what discover agreed, got {after_discover}'
    assert future not in after_discover, 'a version the server rejected must not be spoken again'


def test_the_run_speaks_the_version_discover_answered_on_not_the_newest_listed(monkeypatch):
    """A server can REFUSE a version at the transport and still LIST it in the
    discover payload. Taking the newest listed one would undo the negotiation: the
    rest of the run would speak the very version we just watched it refuse, and a
    good older server would fail the smoke."""
    future = '2027-01-01'
    monkeypatch.setattr(smoke, 'MODERN_VERSIONS', (future, '2026-07-28'))

    def route(method, url, headers, payload, **kwargs):
        if method == 'POST' and headers.get('MCP-Protocol-Version') == future:
            return _json(400, {'error': 'unsupported protocol version'})
        response = _modern_ok(method, url, headers, payload, **kwargs)
        if payload and payload.get('method') == 'server/discover':
            # Answers on the old version, but advertises both.
            body = json.loads(response.body)
            body['result']['supportedVersions'] = [future, '2026-07-28']
            return _json(200, body)
        return response

    code, fake = _run_fake(route)
    assert code == 0, 'the run must not re-speak a version the transport refused'
    spoken_after_discover = {
        s[2]['MCP-Protocol-Version']
        for s in fake.sent
        if 'Mcp-Method' in s[2] and s[3].get('method') != 'server/discover'
    }
    assert spoken_after_discover == {'2026-07-28'}, spoken_after_discover


def test_a_discover_result_that_is_not_a_list_of_versions_fails(monkeypatch):
    """`'2026-07-28' in 'not 2026-07-28 sorry'` is True, so a string would pass a
    membership test that is meant to be over a list."""

    def route(method, url, headers, payload, **kwargs):
        response = _modern_ok(method, url, headers, payload, **kwargs)
        if payload and payload.get('method') == 'server/discover':
            body = json.loads(response.body)
            body['result']['supportedVersions'] = 'we support 2026-07-28 honest'
            return _json(200, body)
        return response

    code, _ = _run_fake(route)
    assert code == smoke.EXIT_FAILED


def test_a_server_sharing_no_protocol_with_us_fails(monkeypatch):
    """The other side of the same coin: "older than us" is fine, "nothing in common"
    is a broken server and must not pass as one."""
    monkeypatch.setattr(smoke, 'MODERN_VERSIONS', ('2027-01-01',))
    code, _ = _run_fake(_modern_ok)
    assert code == smoke.EXIT_FAILED


def test_a_fully_modern_server_passes():
    code, fake = _run_fake(_modern_ok)
    assert code == 0
    modern_calls = [s for s in fake.sent if s[2].get('MCP-Protocol-Version') == '2026-07-28']
    assert {s[3]['method'] for s in modern_calls} >= {'server/discover', 'tools/list', 'tools/call'}
    call = next(s for s in modern_calls if s[3]['method'] == 'tools/call')
    assert call[2]['Mcp-Method'] == 'tools/call'
    assert call[2]['Mcp-Name'] == 'check_usage'


def test_discover_succeeding_while_a_modern_call_fails_is_a_failure():
    """A client that sees a successful discover never falls back, so discover must
    not succeed unless what follows it works."""

    def broken_call(payload, headers):
        if headers.get('MCP-Protocol-Version', '').startswith('2026'):
            return _json(400, {'jsonrpc': '2.0', 'id': payload['id'], 'error': {'code': -32600, 'message': 'bad'}})
        return _sse(200, _result(payload, _USAGE))

    code, _ = _run_fake(lambda *a: _modern_ok(*a, overrides={'tools/call': broken_call}))
    assert code == smoke.EXIT_FAILED


def test_a_discover_advertising_subscriptions_fails():
    def discover(payload, _headers):
        return _json(
            200,
            _result(
                payload,
                {
                    'supportedVersions': ['2026-07-28'],
                    'capabilities': {'tools': {'listChanged': True}},
                    'resultType': 'complete',
                },
            ),
        )

    code, _ = _run_fake(lambda *a: _modern_ok(*a, overrides={'server/discover': discover}))
    assert code == smoke.EXIT_FAILED


def test_a_held_listen_stream_fails():
    def hang(_payload, _headers):
        raise smoke.TransportError('TimeoutError: read timed out')

    code, _ = _run_fake(lambda *a: _modern_ok(*a, overrides={'subscriptions/listen': hang}))
    assert code == smoke.EXIT_FAILED


def test_an_sse_reply_is_matched_by_id_not_position():
    def call(payload, _headers):
        other = {'jsonrpc': '2.0', 'id': 'someone-else', 'result': {'isError': True}}
        return _sse(200, other, _result(payload, _USAGE))

    code, _ = _run_fake(lambda *a: _modern_ok(*a, overrides={'tools/call': call}))
    assert code == 0


def test_a_tool_error_result_fails_even_with_http_200():
    def call(payload, _headers):
        return _sse(200, _result(payload, {'content': [], 'isError': True, 'structuredContent': {'status': 'error'}}))

    code, _ = _run_fake(lambda *a: _modern_ok(*a, overrides={'tools/call': call}))
    assert code == smoke.EXIT_FAILED


def test_retries_rerun_the_whole_suite_and_report_the_last_attempt():
    attempts = {'n': 0}

    def flaky_health(method, url, headers, payload):
        if url.endswith('/mcp/healthz'):
            attempts['n'] += 1
            if attempts['n'] == 1:
                return _json(503, {})
        return _modern_ok(method, url, headers, payload)

    code, _ = _run_fake(flaky_health, '--attempts', '2', '--retry-delay', '0')
    assert code == 0
    assert attempts['n'] == 2


@pytest.mark.parametrize('status', [500, 502])
def test_a_listen_5xx_is_not_a_refusal(status):
    def listen(_payload, _headers):
        return _json(status, {})

    code, _ = _run_fake(lambda *a: _modern_ok(*a, overrides={'subscriptions/listen': listen}))
    assert code == smoke.EXIT_FAILED


@pytest.mark.parametrize('status', [401, 403, 421, 429, 503])
def test_only_an_unsupported_version_answer_counts_as_a_refused_discover(status):
    """A modern-capable server answering 429 or 421 must not skip every modern
    check as if it served only the 2025 era."""

    def discover(_payload, _headers):
        return _json(status, {})

    code, _ = _run_fake(lambda *a: _modern_ok(*a, overrides={'server/discover': discover}))
    assert code == smoke.EXIT_FAILED


def test_a_definite_failure_is_not_retried_away():
    """A check that fails on a real answer (not a blip) fails the smoke on the first
    attempt, instead of a flaky server passing one run in three."""
    calls = {'n': 0}

    def flaky_call(payload, _headers):
        calls['n'] += 1
        status = 'error' if calls['n'] == 1 else 'ok'
        return _sse(200, _result(payload, {'content': [], 'isError': False, 'structuredContent': {'status': status}}))

    code, _ = _run_fake(
        lambda *a: _modern_ok(*a, overrides={'tools/call': flaky_call}),
        '--attempts',
        '3',
        '--retry-delay',
        '0',
    )
    assert code == smoke.EXIT_FAILED


def test_a_transient_failure_is_retried_and_the_pass_says_so(capsys):
    calls = {'n': 0}

    def blip(method, url, headers, payload):
        if url.endswith('/mcp/healthz'):
            calls['n'] += 1
            if calls['n'] == 1:
                raise smoke.TransportError('ConnectionResetError: reset')
        return _modern_ok(method, url, headers, payload)

    code, _ = _run_fake(blip, '--attempts', '2', '--retry-delay', '0')
    assert code == 0
    assert 'WARNING' in capsys.readouterr().out


def test_a_malformed_http_answer_fails_the_check_instead_of_crashing():
    def broken(method, url, headers, payload):
        if url.endswith('/mcp/healthz'):
            raise http.client.IncompleteRead(b'')
        return _modern_ok(method, url, headers, payload)

    assert issubclass(http.client.IncompleteRead, http.client.HTTPException)
    # main() guards every transport, the injected one included.
    code, _ = _run_fake(broken)
    assert code == smoke.EXIT_FAILED


# ── redirects and the key ───────────────────────────────────────────


def test_a_redirect_fails_and_the_key_never_follows_it():
    seen = []

    class _Recorder(http.server.BaseHTTPRequestHandler):
        def _record(self):
            seen.append(self.headers.get('Authorization'))
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

        do_GET = _record  # noqa: N815
        do_POST = _record  # noqa: N815

        def log_message(self, *_args):
            pass

    target = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Recorder)
    threading.Thread(target=target.serve_forever, daemon=True).start()

    class _Redirect(http.server.BaseHTTPRequestHandler):
        def _go(self):
            self.send_response(307)
            # A fixed path per request, never the request's own path echoed into a header.
            location = f'http://127.0.0.1:{target.server_address[1]}/mcp'
            if self.path.startswith('/healthz'):
                location = f'http://127.0.0.1:{target.server_address[1]}/healthz'
            self.send_header('Location', location)
            self.send_header('Content-Length', '0')
            self.end_headers()

        do_GET = _go  # noqa: N815
        do_POST = _go  # noqa: N815

        def log_message(self, *_args):
            pass

    redirector = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Redirect)
    threading.Thread(target=redirector.serve_forever, daemon=True).start()
    try:
        result = _run_smoke(
            '--base-url', f'http://127.0.0.1:{redirector.server_address[1]}', '--oauth', 'off', '--timeout', '3'
        )
    finally:
        redirector.shutdown()
        target.shutdown()
    assert result.returncode == smoke.EXIT_FAILED
    assert 'FAIL [healthz]' in result.stdout
    assert f'Bearer {GOOD_KEY}' not in seen


def test_the_key_is_read_from_the_environment_only():
    """A key on the command line would sit in the process list and in any log of
    the command."""
    result = _run_smoke('--help', key=None)
    assert '--api-key' not in result.stdout
    assert 'LENZ_API_KEY' in result.stdout
