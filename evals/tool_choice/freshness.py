"""When is a result file a measurement of THIS eval, and may it vouch for a release.

A result is keyed on everything that decides what it measured, not only on the
words the model is shown:

- the manifest wording (instructions, tool names, titles, descriptions, schemas);
- the harness: the cases and their predicates, the scorer, the way a history and
  the tools are sent to each vendor, and the default models, hashed from the
  PARSED source so a comment or a reformat does not invalidate a paid run but a
  changed predicate, case or prompt does;
- the conversation histories as built (the connector's own tool results, which
  carry guidance of their own): a changed note in a result changes what the
  model reads without touching any source file here.

A file also has to be COMPLETE (the run finished and wrote it whole), measured on
the current default models, and its rows must satisfy the gate over the whole
required matrix at the release repeat count. A file that merely names the right
hash proves none of that.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

from evals.tool_choice import scoring

HERE = Path(__file__).resolve().parent

# The files whose behaviour decides what a run measures.
_HARNESS_FILES = ('cases.py', 'scoring.py', 'tool_results.py', 'vendors.py')


def _canonical(node: Any) -> Any:
    """A parsed tree as plain data, the same on every supported Python.

    `ast.dump` is not: 3.12 adds fields (`type_params`) and 3.13 changes which
    empty ones it prints, so a result measured on one interpreter would read as
    stale on another. Fields that are empty or absent are left out here.
    """
    if isinstance(node, ast.AST):
        fields = {
            name: _canonical(value)
            for name, value in ast.iter_fields(node)
            if name != 'type_params' and value is not None and value != []
        }
        return [type(node).__name__, fields]
    if isinstance(node, list):
        return [_canonical(item) for item in node]
    return repr(node)


def _source_digest(path: Path) -> str:
    """A digest of a module's parsed code: docstrings, comments and layout excluded."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
    return hashlib.sha256(json.dumps(_canonical(tree), sort_keys=True).encode()).hexdigest()


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def harness_hash() -> str:
    """A digest of the cases, scorer, vendor wiring and the histories as they are built."""
    from evals.tool_choice import cases

    histories = [[case.id, _plain(case.history), case.prompt] for case in cases.CASES]
    parts = [_source_digest(HERE / name) for name in _HARNESS_FILES]
    parts.append(json.dumps(histories, sort_keys=True, ensure_ascii=False))
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest()[:16]


def eval_key(manifest) -> str:
    """The key a result file is named and checked by: wording and harness together."""
    return hashlib.sha256(f'{manifest.hash()}:{harness_hash()}'.encode()).hexdigest()[:16]


def check_fresh(directory: Path, key: str, models: dict[str, str]) -> tuple[bool, str]:
    """Is there a result file that may vouch for the current eval? Returns (ok, why).

    Needs a file that records `key`, says it is complete, was measured on the
    current default `models`, and whose rows meet the gate over the whole required
    matrix at `RELEASE_REPEAT` attempts. The best reason for the NEWEST file that
    failed is returned so a stale answer says what to run, not just that it is stale.
    """
    reason = 'no result file for this wording and harness'
    for path in sorted(directory.glob(f'results_*_{key}.json'), reverse=True):
        # The key the file RECORDS, not the one its name claims: a renamed or
        # hand-copied file must not vouch for an eval it never measured. A
        # malformed file is skipped, never fatal: one bad newer file must not
        # hide a good older one.
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get('key') != key:
            continue
        rows = data.get('results')
        if not isinstance(rows, list) or data.get('complete') is not True:
            reason = f'{path.name} is not a complete run'
            continue
        wrong_model = sorted(
            {
                f'{row.get("vendor")}: {row.get("model")}'
                for row in rows
                if isinstance(row, dict) and row.get('model') != models.get(row.get('vendor'))
            }
        )
        if wrong_model:
            reason = f'{path.name} was measured on other models than the current defaults ({", ".join(wrong_model)})'
            continue
        verdict = scoring.gate([r for r in rows if isinstance(r, dict)], min_attempts=scoring.RELEASE_REPEAT)
        problems = [*verdict.failed, *verdict.incomplete]
        if problems:
            more = f' (and {len(problems) - 3} more)' if len(problems) > 3 else ''
            reason = f'{path.name} does not meet the gate: ' + '; '.join(problems[:3]) + more
            continue
        return True, f'{path.name} covers the required matrix at {scoring.RELEASE_REPEAT} attempts, all passing'
    return False, reason
