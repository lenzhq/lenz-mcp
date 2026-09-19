"""Fail-closed smoke test for the lenz-mcp service, in both MCP protocol eras.

Run it against a deployed server after a deploy (or a rollback), before
trusting the new revision. It reads every answer (JSON or SSE, matched by JSON-RPC id), so a 200 that carries an
error, a tool result with `isError`, or an `auth_required` status fails it.

The checks, in order:
- `GET /mcp/healthz` is 200, and `GET /mcp` is refused (405, no idle stream);
- keyless: with OAuth on, the 401 challenge that starts OAuth discovery (legacy
  tools/list and modern server/discover); with OAuth off, the tool list;
- the 2025-era protocol with the key: initialize, tools/list, a check_usage call
  (free: it reads /me/usage, and proves the key reaches the API);
- the post-handshake protocol: `server/discover` MUST succeed, and everything
  after it must work — modern tools/list and check_usage, no
  `listChanged`/`subscribe` advertised, and `subscriptions/listen` refused rather
  than held open. A client that sees a successful discover never falls back, so a
  half-working modern endpoint is worse than none.

Both eras are checked on every run and both are permanent: Claude and ChatGPT
still open 2025-era sessions in places, and a revision that stopped serving
either is broken.

"Modern" means "lists a revision this smoke can speak" (`wire.MODERN_VERSIONS`),
never one pinned string, and the smoke then SPEAKS the revision discover agreed
on. That is what lets today's checkout smoke an older revision after a rollback:
the day a newer protocol lands, a pinned string would fail the rollback of a
perfectly good revision.

The key comes from `LENZ_API_KEY` in the environment only (never an argument, which
would sit in the process list and in any log of the command), and is never printed. A
missing key is a configuration error, not a skip, unless `--keyless` is given.

Standard library only: it runs on a plain system `python3` (3.9 or newer),
outside the project venv.

Exit codes: 0 all checks passed; 1 a check failed; 2 configuration error;
3 (`--preflight` only) the API rejected the key.
"""

# Runs on a plain system python3, 3.9 or newer: no 3.10+ syntax at runtime.
from __future__ import annotations

import argparse
import itertools
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# The wire itself lives in the package, so every client that speaks to a
# deployed server shares one transport and one envelope, and they fail together.
# A client packaged without this script can still import it.
#
# The repo root goes on sys.path by hand: run by ABSOLUTE path, this script's
# `sys.path[0]` is its own directory, not the root, and the import would fail
# however stdlib-only the module is. The package's `__init__` imports nothing,
# so bare python3 reaches the wire with no dependencies.
#
# APPEND, never insert(0). First place gives the repo root precedence over the
# STDLIB, and `wire.py` imports `http.client` and `urllib.request` below this
# line — so everything they reach for lazily (`email`, `ssl`, `mimetypes`, …)
# becomes shadowable by any future top-level file of that name. Reproduced with
# a one-line `email.py` at the repo root: the smoke dies inside
# `http/client.py: import email.parser`, with a traceback nowhere near the
# cause. We are overriding nothing (the package is simply absent from the
# default path), so precedence buys exactly nothing.
sys.path.append(str(Path(__file__).resolve().parents[1]))

try:
    from lenz_mcp.wire import (  # noqa: E402 — must follow the sys.path line above
        LEGACY_VERSION,
        MODERN_VERSIONS,
        Response,
        Transport,
        TransportError,
        guard_transport,
        modern_meta,  # noqa: F401 — re-exported: a wire test pins it against the harness
        urllib_transport,
    )
    from lenz_mcp.wire import messages as _messages  # noqa: E402,F401 — re-exported, pinned by the same test
    from lenz_mcp.wire import reply as wire_reply  # noqa: E402 — same
    from lenz_mcp.wire import request as wire_request  # noqa: E402 — same
except ImportError as exc:  # pragma: no cover - exercised by copying this file alone
    # This file is not a self-contained script, and a copy run on its own fails
    # with a bare ModuleNotFoundError that says nothing about why. Say it here.
    sys.exit(
        f"This smoke script cannot import the connector's wire module ({exc}).\n"
        'It is not standalone: it speaks the wire protocol from the package, so every '
        'caller of the smoke speaks the same one. Run it from a checkout of the '
        'repository, not from a copy of the file on its own.'
    )

USER_AGENT = 'lenz-deploy-smoke/1'

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_KEY_REJECTED = 3


class CheckFailed(Exception):
    """A check failed on a definite answer: retrying the suite would only hide it."""


class Transient(CheckFailed):
    """A check failed on an answer worth one more try (5xx, 429)."""


def _fail(message: str):
    raise CheckFailed(message)


class Smoke:
    def __init__(
        self,
        base_url: str,
        *,
        key: str,
        oauth: str,
        transport: Transport,
        timeout: float,
        listen_timeout: float,
        out=None,
    ):
        self.base = base_url.rstrip('/')
        # The revision the modern calls speak. Starts at the newest this smoke knows
        # and drops to whatever `server/discover` agreed on, so a rollback to an
        # older revision is smoked in a protocol that revision actually serves.
        self.modern_version = MODERN_VERSIONS[0]
        self.key = key
        self.oauth = oauth
        self.transport = transport
        self.timeout = timeout
        self.listen_timeout = listen_timeout
        self.out = out or sys.stdout
        self.failed = False
        # Retry the suite only when every failure was transient: a revision that fails a
        # check on a real answer must not pass the smoke one run in three.
        self.definite_failure = False
        self._ids = itertools.count(1)

    # ── output ──

    def _say(self, line: str) -> None:
        print(line.replace(self.key, '<LENZ_API_KEY>') if self.key else line, file=self.out, flush=True)

    def check(self, label: str, fn: Callable[[], str | None]) -> bool:
        try:
            note = fn()
        except Transient as exc:
            self._say(f'  FAIL [{label}] {exc}')
            self.failed = True
            return False
        except CheckFailed as exc:
            self._say(f'  FAIL [{label}] {exc}')
            self.failed = True
            self.definite_failure = True
            return False
        except TransportError as exc:
            self._say(f'  FAIL [{label}] no response: {exc}')
            self.failed = True
            return False
        self._say(f'  OK   [{label}]' + (f' {note}' if note else ''))
        return True

    # ── requests ──

    def _get(self, path: str, accept: str = 'application/json') -> Response:
        headers = {'Accept': accept, 'User-Agent': USER_AGENT}
        return self.transport('GET', self.base + path, headers, None, self.timeout)

    def _post(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        era: str,
        auth: bool = True,
        name: str | None = None,
        notification: bool = False,
        timeout: float | None = None,
    ):
        request_id = None if notification else f'smoke-{next(self._ids)}'
        headers, body = wire_request(
            method,
            params,
            era=era,
            request_id=request_id,
            user_agent=USER_AGENT,
            client_name='lenz-deploy-smoke',
            key=self.key if auth else None,
            name=name,
            # The revision discover settled on, not a constant: see modern_discover.
            version=self.modern_version,
        )
        resp = self.transport('POST', f'{self.base}/mcp', headers, body, timeout or self.timeout)
        return resp, wire_reply(resp, request_id)

    @staticmethod
    def _status_failure(resp: Response, what: str) -> CheckFailed:
        kind = Transient if resp.status >= 500 or resp.status == 429 else CheckFailed
        return kind(f'{what}: HTTP {resp.status} {resp.body[:160]!r}')

    @classmethod
    def _result(cls, resp: Response, message: dict[str, Any] | None, what: str) -> dict[str, Any]:
        if resp.status != 200:
            raise cls._status_failure(resp, what)
        if message is None:
            raise CheckFailed(f'{what}: HTTP 200 but no JSON-RPC reply with the request id')
        if 'error' in message:
            raise CheckFailed(f'{what}: JSON-RPC error {message["error"]}')
        result = message.get('result')
        if not isinstance(result, dict):
            raise CheckFailed(f'{what}: reply has no result object')
        return result

    def _challenge(self, resp: Response, what: str) -> str:
        challenge = resp.headers.get('www-authenticate', '')
        if resp.status != 401 or 'oauth-protected-resource' not in challenge:
            raise CheckFailed(f'{what}: expected 401 with an OAuth challenge, got HTTP {resp.status}')
        return 'OAuth challenge'

    # ── checks ──

    def healthz(self):
        resp = self._get('/mcp/healthz')
        if resp.status != 200:
            raise self._status_failure(resp, 'healthz')

    def get_refused(self):
        resp = self._get('/mcp', accept='text/event-stream')
        if resp.status != 405:
            raise CheckFailed(f'expected 405, got HTTP {resp.status}: an idle server stream may be open again')

    def keyless_tools_list(self):
        resp, message = self._post('tools/list', era='legacy', auth=False)
        if self.oauth == 'auto':
            # Not configured (`--oauth auto`): read it off the answer.
            self.oauth = 'on' if resp.status == 401 else 'off'
        if self.oauth == 'on':
            return self._challenge(resp, 'keyless tools/list')
        self._tool_names(self._result(resp, message, 'keyless tools/list'))
        return 'tool list served without a key (OAuth off)'

    def keyless_modern_discover(self):
        # Runs BEFORE the negotiation ladder, so it asks at MODERN_VERSIONS[0]. That is
        # fine only while the auth layer answers before the transport's version gate —
        # it wraps the route, so a 401 wins today. If a second revision is ever appended
        # and this starts failing against an OLDER revision with OAuth on, that ordering
        # is why, and the fix is to negotiate before this check rather than to relax it.
        resp, _ = self._post('server/discover', era='modern', auth=False)
        return self._challenge(resp, 'keyless server/discover')

    @staticmethod
    def _tool_names(result: dict[str, Any]) -> list[str]:
        names = [t.get('name') for t in result.get('tools') or [] if isinstance(t, dict)]
        for required in ('assess_claim', 'check_usage'):
            if required not in names:
                raise CheckFailed(f'tool list lacks {required} (has {names[:12]})')
        return names

    def initialize(self):
        resp, message = self._post(
            'initialize',
            {
                'protocolVersion': LEGACY_VERSION,
                'capabilities': {},
                'clientInfo': {'name': 'lenz-deploy-smoke', 'version': '1'},
            },
            era='legacy',
        )
        result = self._result(resp, message, 'initialize')
        if result.get('protocolVersion') != LEGACY_VERSION:
            raise CheckFailed(f'negotiated {result.get("protocolVersion")!r}, expected {LEGACY_VERSION}')
        resp, _ = self._post('notifications/initialized', era='legacy', notification=True)
        if resp.status not in (200, 202):
            raise CheckFailed(f'notifications/initialized: HTTP {resp.status}')

    def tools_list(self, era: str):
        resp, message = self._post('tools/list', era=era)
        result = self._result(resp, message, f'{era} tools/list')
        if era == 'modern' and result.get('resultType') != 'complete':
            raise CheckFailed(f'modern tools/list has resultType {result.get("resultType")!r}')
        return f'{len(self._tool_names(result))} tools'

    def check_usage(self, era: str):
        resp, message = self._post('tools/call', {'name': 'check_usage', 'arguments': {}}, era=era, name='check_usage')
        result = self._result(resp, message, f'{era} check_usage')
        structured = result.get('structuredContent')
        status = structured.get('status') if isinstance(structured, dict) else None
        if status == 'auth_required':
            # Either the API rejected the key, or this revision lost the Authorization header on
            # its way to the tool. The preflight checked the key against the API directly.
            raise CheckFailed(
                'the tool answered auth_required: the API key did not reach the API, or the API rejected it '
                '(the preflight checks the key on its own)'
            )
        if result.get('isError') or status != 'ok':
            raise CheckFailed(f'tool result isError={result.get("isError")} status={status!r}')
        return 'the key reaches the API'

    def modern_discover(self) -> bool:
        """Run the discover check; True when it passed, so the modern checks must run.

        Discover MUST succeed: every revision that can still be deployed or rolled
        back to serves the post-handshake protocol, so a refusal is a broken revision,
        not an old one.

        The request is tried at each revision this smoke knows, newest first, because
        an OLDER revision answers 400 to a version it has never heard of — and that is
        exactly the revision a rollback points at. Whatever discover settles
        on becomes the version every later modern call speaks.
        """

        def _validate():
            resp = message = None
            for version in MODERN_VERSIONS:
                self.modern_version = version
                resp, message = self._post('server/discover', era='modern')
                # 400 is the transport refusing the version itself, before any handler.
                # Any other status is this revision's real answer and is not retried: a
                # 401, 421 or 429 says nothing about the protocol and must not look like
                # one. The whole loop sits inside the check so a TransportError reaches
                # `check` as itself and stays RETRYABLE — routed through `_fail` it would
                # be a definite failure, and one network blip mid-suite, right after a
                # traffic shift, would disable the retry the smoke has for exactly that.
                if resp.status != 400 or version == MODERN_VERSIONS[-1]:
                    break

            result = self._result(resp, message, 'server/discover')
            versions = result.get('supportedVersions')
            # A string would make `in` a SUBSTRING test, and '2026-07-28' is `in`
            # 'not 2026-07-28 sorry'. Shape first, then membership.
            if not isinstance(versions, list):
                raise CheckFailed(f'supportedVersions is {type(versions).__name__}, not a list: {versions!r}')
            if not any(v in versions for v in MODERN_VERSIONS):
                raise CheckFailed(f'supportedVersions {versions} carries none of {list(MODERN_VERSIONS)}')
            # `self.modern_version` is ALREADY the version discover answered on, and it
            # stays. Taking the newest one the payload merely CLAIMS would undo the
            # negotiation: a revision whose transport refuses the newest while still
            # listing it would be spoken to, for the rest of the run, in the one version
            # we just watched it refuse — turning a good rollback red on the day the
            # ladder exists for.
            if result.get('resultType') != 'complete':
                raise CheckFailed(f'resultType {result.get("resultType")!r}')
            advertised = [
                f'{area}.{flag}'
                for area, block in (result.get('capabilities') or {}).items()
                if isinstance(block, dict)
                for flag in ('listChanged', 'subscribe')
                if block.get(flag) is True
            ]
            if advertised:
                raise CheckFailed(f'advertises {advertised}, which a stateless server cannot deliver')
            return f'supports {versions}'

        # The checks after this one all speak the protocol discover just settled, so a
        # failed discover makes them noise on top of the real failure, not extra signal.
        return self.check('modern_discover', _validate)

    def listen_refused(self):
        try:
            resp, message = self._post('subscriptions/listen', {}, era='modern', timeout=self.listen_timeout)
        except TransportError as exc:
            raise CheckFailed(f'no answer within {self.listen_timeout:g}s (held open?): {exc}') from exc
        if resp.status == 200:
            if message is None:
                raise CheckFailed('subscriptions/listen opened a stream')
            if 'error' not in message:
                raise CheckFailed('subscriptions/listen was served')
            return 'refused (JSON-RPC error)'
        if 400 <= resp.status < 500:
            return f'refused (HTTP {resp.status})'
        raise self._status_failure(resp, 'subscriptions/listen')

    # ── suites ──

    def preflight(self, api_base_url: str) -> int:
        """The key against `GET /me/usage` on the public API, not through lenz-mcp: an MCP
        that drops the Authorization header also answers auth_required, and blaming the key
        for it would refuse the deploy that fixes the MCP."""
        headers = {'Accept': 'application/json', 'User-Agent': USER_AGENT, 'Authorization': f'Bearer {self.key}'}
        url = api_base_url.rstrip('/') + '/me/usage'
        try:
            resp = self.transport('GET', url, headers, None, self.timeout)
        except TransportError as exc:
            self._say(f'  FAIL [api_key] no response from {url}: {exc}')
            return EXIT_FAILED
        if resp.status in (401, 403):
            self._say(f'  FAIL [api_key] the API rejected LENZ_API_KEY (HTTP {resp.status}): fix LENZ_API_KEY')
            return EXIT_KEY_REJECTED
        if resp.status != 200:
            self._say(f'  FAIL [api_key] HTTP {resp.status} from {url}')
            return EXIT_FAILED
        self._say('  OK   [api_key] the API accepts LENZ_API_KEY')
        return EXIT_OK

    def run(self, *, keyless: bool) -> int:
        self.check('healthz', self.healthz)
        self.check('get_refused', self.get_refused)
        self.check('keyless_tools_list', self.keyless_tools_list)
        if self.oauth == 'on':  # after keyless_tools_list, which resolves `auto`
            self.check('keyless_modern_discover', self.keyless_modern_discover)
        if keyless:
            return EXIT_FAILED if self.failed else EXIT_OK

        self.check('initialize', self.initialize)
        self.check('tools_list', lambda: self.tools_list('legacy'))
        self.check('check_usage', lambda: self.check_usage('legacy'))
        if self.modern_discover():
            self.check('modern_tools_list', lambda: self.tools_list('modern'))
            self.check('modern_check_usage', lambda: self.check_usage('modern'))
            self.check('modern_listen_refused', self.listen_refused)
        return EXIT_FAILED if self.failed else EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Fail-closed smoke test for lenz-mcp in both MCP protocol eras. '
        'The API key is read from the LENZ_API_KEY environment variable only.',
    )
    parser.add_argument('--base-url', default=os.environ.get('FRONTEND_URL', 'https://lenz.io'))
    parser.add_argument('--api-base-url', help='the public API root for --preflight (default: BASE_URL/api/v1)')
    parser.add_argument(
        '--oauth',
        choices=('on', 'off', 'auto'),
        required=True,
        help='whether lenz-mcp runs with OAuth; auto reads it off the keyless answer (weaker: use it only unconfigured)',
    )
    parser.add_argument('--keyless', action='store_true', help='only the checks that need no key')
    parser.add_argument(
        '--preflight', action='store_true', help='only prove the API accepts LENZ_API_KEY (exit 3 if rejected)'
    )
    parser.add_argument('--timeout', type=float, default=20.0, help='seconds per request')
    parser.add_argument('--listen-timeout', type=float, default=5.0)
    parser.add_argument('--attempts', type=int, default=int(os.environ.get('MCP_SMOKE_ATTEMPTS', '3')))
    parser.add_argument('--retry-delay', type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None, *, transport: Transport = urllib_transport, key: str | None = None) -> int:
    args = _parser().parse_args(argv)
    transport = guard_transport(transport)
    key = (key if key is not None else os.environ.get('LENZ_API_KEY', '')).strip()
    if not key and not args.keyless:
        print('ERROR: LENZ_API_KEY is not set. The MCP smoke needs it (use --keyless for the keyless checks only).')
        return EXIT_CONFIG

    if args.preflight:
        smoke = Smoke(
            args.base_url,
            key=key,
            oauth=args.oauth,
            transport=transport,
            timeout=args.timeout,
            listen_timeout=args.listen_timeout,
        )
        code = smoke.preflight(args.api_base_url or args.base_url.rstrip('/') + '/api/v1')
        print('MCP preflight: ' + ('passed' if code == EXIT_OK else f'FAILED (exit {code})'), flush=True)
        return code

    attempts = max(1, args.attempts)
    code = EXIT_FAILED
    earlier_failures = 0
    for attempt in range(1, attempts + 1):
        print(
            f'MCP smoke against {args.base_url} (oauth={args.oauth}, attempt {attempt}/{attempts})',
            flush=True,
        )
        smoke = Smoke(
            args.base_url,
            key=key,
            oauth=args.oauth,
            transport=transport,
            timeout=args.timeout,
            listen_timeout=args.listen_timeout,
        )
        code = smoke.run(keyless=args.keyless)
        if code == EXIT_OK or smoke.definite_failure or attempt == attempts:
            break
        earlier_failures += 1
        print(f'  (transient failures only: retrying in {args.retry_delay:g}s)', flush=True)
        time.sleep(args.retry_delay)
    if code == EXIT_OK and earlier_failures:
        print(f'WARNING: passed on attempt {attempt} after {earlier_failures} transient failure(s): watch lenz-mcp.')
    print('MCP smoke: ' + ('passed' if code == EXIT_OK else f'FAILED (exit {code})'), flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
