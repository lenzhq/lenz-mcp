"""Read a result file and answer the questions a wording decision needs.

    uv run python -m evals.tool_choice.report [result.json]

The runner prints a table; this prints the per-finding view: how often each
failure happened, out of how many attempts, per vendor and per arm, with one raw
sample. That is the shape a decision needs — "twice in three on one vendor" and
"three in three on both" are different problems, and a table cannot say which.

It also checks the FAITHFULNESS of any draft a model split itself, because that
is the question a rewrite raises: a resolved pronoun is helpful, a dropped hedge
or a rounded figure is not. Mechanical, not a judgement, and only a words check:
every content word of a split claim is looked for in the source text, the
negations and figures of a claim are compared IN ORDER with the draft sentence it
matches best (so a dropped "not" or a moved figure is named), and what differs is
printed for a human to read. Silence is not a finding that the split is faithful.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

RESULTS_DIR = Path(__file__).resolve().parent

# Words whose absence says nothing: a split that adds "the" is not a rewrite.
_STOP = {
    'a',
    'about',
    'an',
    'and',
    'are',
    'as',
    'at',
    'be',
    'been',
    'by',
    'for',
    'from',
    'had',
    'has',
    'in',
    'is',
    'it',
    'its',
    'of',
    'on',
    'or',
    'over',
    'same',
    'that',
    'the',
    'their',
    'this',
    'to',
    'until',
    'was',
    'were',
    'with',
}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[\w%’']+", text.lower()) if w not in _STOP]


def _latest() -> Path:
    files = sorted(RESULTS_DIR.glob('results_*.json'))
    if not files:
        raise SystemExit('no result files')
    return files[-1]


def _attempts(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """(failed attempts, total attempts) over a set of rows."""
    total = sum(r['attempts'] for r in rows)
    failed = sum(r['attempts'] - r['passes'] for r in rows)
    return failed, total


_NEGATION = re.compile(r"\b(?:not|no|never|none|neither|nor|without|cannot|\w+n['’]t)\b", re.IGNORECASE)
_FIGURE = re.compile(r'\d+(?:[.,]\d+)?%?')


def _signals(text: str) -> list[str]:
    """The negations and figures of a text, in the order they appear."""
    found = [(m.start(), m.group(0).lower()) for m in _NEGATION.finditer(text)]
    found += [(m.start(), m.group(0)) for m in _FIGURE.finditer(text)]
    return [token for _, token in sorted(found)]


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]


def faithfulness(source: str, claims: list[str]) -> list[str]:
    """What a split lost or invented, relative to the source text. A words check, not a judgement."""
    notes: list[str] = []
    source_words = set(_words(source))
    sentences = _sentences(source) or [source]
    for claim in claims:
        missing = [w for w in _words(claim) if w not in source_words]
        if missing:
            notes.append(f'"{claim}" adds words not in the draft: {missing}')
        # The draft sentence this claim is closest to, by shared words: a negation
        # or a figure must match THAT sentence's, in order. Comparing against the
        # whole draft would let "X did not increase 10%" lose its "not" unseen.
        claim_words = set(_words(claim))
        closest = max(sentences, key=lambda sentence: len(claim_words & set(_words(sentence))))
        if _signals(claim) != _signals(closest):
            notes.append(
                f'"{claim}": negations and figures {_signals(claim)} differ from the draft sentence it matches '
                f'({_signals(closest)})'
            )
    # A figure or a hedge going missing is the damage worth naming explicitly.
    for token in _FIGURE.findall(source):
        if not any(token in claim for claim in claims):
            notes.append(f'the figure {token!r} is in the draft and in none of the split claims')
    for hedge in ('analysts say', 'roughly', 'about', 'approximately', 'reportedly', 'estimated'):
        if hedge in source.lower() and not any(hedge in c.lower() for c in claims):
            notes.append(f'the hedge {hedge!r} is in the draft and in none of the split claims')
    return notes


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = Path(argv[0]) if argv else _latest()
    data = json.loads(path.read_text())
    rows = data['results']

    print(f'file     {path.name}')
    print(f'wording  {data["wording"]}   repeat {data["repeat"]}   models {", ".join(data.get("models", []))}')

    print('\n── every failure, by vendor and arm (failed attempts / total) ──')
    buckets: dict[tuple[str, str, bool], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row['verdict'] != 'pass':
            buckets[(row['case'], row['vendor'], row['with_instructions'])].append(row)
    if not buckets:
        print('  none')
    for (case, vendor, with_instructions), group in sorted(buckets.items()):
        failed, total = _attempts(group)
        arm = 'with' if with_instructions else 'without'
        notes = sorted({note for row in group for note in row['notes']})
        print(f'  {case:32} {vendor:10} {arm:8} {failed}/{total}  {notes}')

    print('\n── a raw sample for each failing case ──')
    for (case, vendor, with_instructions), group in sorted(buckets.items()):
        sample = next((a for row in group for a in row['observed']), None)
        arm = 'with' if with_instructions else 'without'
        print(f'  {case} / {vendor} / {arm}:')
        print(f'    {json.dumps(sample)[:500]}')

    print('\n── did any model split a draft itself, and was the split faithful? ──')
    drafts = {row['case'] for row in rows if 'draft' in row['case']}
    for case in sorted(drafts):
        for row in [r for r in rows if r['case'] == case]:
            for attempt in row['observed']:
                for call in attempt:
                    claims = call.get('arguments', {}).get('claims')
                    if not claims:
                        continue
                    arm = 'with' if row['with_instructions'] else 'without'
                    print(f'  {case} / {row["vendor"]} / {arm}: split into {len(claims)}')
                    for claim in claims:
                        print(f'      {claim}')
                    source = _source_for(case)
                    for note in faithfulness(source, claims) or [
                        '(words check only: nothing flagged; a person still reads the split)'
                    ]:
                        print(f'    ! {note}')
    return 0


def _source_for(case_id: str) -> str:
    from evals.tool_choice import cases as cases

    return cases.by_id(case_id).prompt


if __name__ == '__main__':
    raise SystemExit(main())
