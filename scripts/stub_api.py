"""A stub of the public API's /me/usage for the connector's container test.

Standard library only. Answers /api/v1/me/usage for one key and 401s every
other; everything else is 404. The same shape the smoke's own tests stub, so
the deploy smoke's `check_usage` call has something real to reach.

    python3 scripts/stub_api.py PORT KEY
"""

from __future__ import annotations

import http.server
import json
import sys


def main() -> None:
    port, key = int(sys.argv[1]), sys.argv[2]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.split('?')[0].rstrip('/') != '/api/v1/me/usage':
                self._reply(404, {'detail': 'not found'})
            elif self.headers.get('Authorization') == f'Bearer {key}':
                self._reply(200, {'plan': 'pro', 'credits': {'remaining': 100}, 'costs': {'verify': 10}})
            else:
                self._reply(401, {'detail': 'Invalid API key'})

        def _reply(self, status, body):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    http.server.ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()  # noqa: S104


if __name__ == '__main__':
    main()
