"""Fail when a tracked file carries a reference that belongs in a private notebook.

This repository is developed in the open, and some things never belong in it:
paths into someone's local notes or scratch space, internal hosting addresses,
and pull-request numbers in code comments (the history is in git; a comment
should say why, not where). CI runs this over every tracked file.

    python3 scripts/scrub_check.py
"""

from __future__ import annotations

import re
import subprocess
import sys

PATTERNS = {
    'a local notes path': re.compile(r'(~/)?\.claude/'),
    'a scratch path': re.compile(r'(?<![\w./-])tmp/'),
    'an internal hosting address': re.compile(r'\b[a-z0-9-]+\.run\.app\b'),
    'a pull-request number in a comment': re.compile(r'(#|//|/\*|<!--).*\(#\d{2,5}\)'),
}
# Files whose content is data, not prose.
SKIP = re.compile(r'(^|/)(uv\.lock|package-lock\.json)$|\.(png|svg|ico)$')


def main() -> int:
    files = (
        subprocess.run(  # noqa: S603
            ['git', 'ls-files', '-z'],  # noqa: S607
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .split('\0')
    )
    findings = []
    for path in filter(None, files):
        if SKIP.search(path) or path == 'scripts/scrub_check.py':
            continue
        try:
            text = open(path, encoding='utf-8').read()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for what, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f'{path}:{lineno}: {what}: {line.strip()[:120]}')
    for finding in findings:
        print(finding)
    if findings:
        print(f'{len(findings)} line(s) to rewrite.', file=sys.stderr)
        return 1
    print('No private references found.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
