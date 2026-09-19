"""Run the tool-choice eval.

    uv run python -m evals.tool_choice --vendor both --repeat 3
    uv run python -m evals.tool_choice --print-submission
    uv run python -m evals.tool_choice --check-fresh
    uv run python -m evals.tool_choice --prices prices.json ...

Makes PAID model calls (a few cents a run). Not in the pytest path; a structural
test checks the cases without a model.

`--prices PATH` prices the spend summary: a JSON object mapping a model-name
prefix to `[eur_per_1M_input_tokens, eur_per_1M_output_tokens]` (the longest matching
prefix wins). Without it the summary prints token totals only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RESULTS_DIR = Path(__file__).resolve().parent


def _load_env() -> None:
    """Vendor API keys from a `.env` in the working directory, when there is
    one and python-dotenv is installed. The environment always wins."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path.cwd() / '.env', override=False)


# A case passes at 3/3, is FLAKY at 1-2/3, fails at 0/3. Flaky is reported
# separately and on purpose: a trigger that fires two times in three is a
# finding about the wording, not noise to be averaged away.
PASSED = 'pass'
FLAKY = 'flaky'
FAILED = 'fail'


def _judge(case, turn) -> tuple[bool, str]:
    """Did this attempt satisfy the case? Deterministic; no model judges."""
    from evals.tool_choice.cases import NONE, RESTRAINT

    names = turn.names
    for forbidden in case.forbid:
        if forbidden in names:
            return False, f'called the forbidden {forbidden}'

    if case.expect == NONE:
        return (not names), ('called ' + ', '.join(names) if names else '')

    if case.expect == RESTRAINT:
        # Zero calls passes. Otherwise: only the allowed tools, each under its
        # ceiling. This is the shape for "spend no more than you were asked to".
        limits = dict(case.at_most)
        for name in names:
            if name not in limits:
                return False, f'called {name}, which this case does not allow'
        for name, limit in limits.items():
            count = names.count(name)
            if count > limit:
                return False, f'called {name} {count} times; at most {limit} allowed'
        return True, ''

    expected = case.expected_tools
    if not names:
        return False, 'called nothing'
    if case.single_call and len(names) != 1:
        return False, f'made {len(names)} calls ({names}); this case must be one call'
    # An ordered list must appear in order; a single name must be the FIRST call
    # (a model that reaches for check_usage first has still got it wrong).
    if len(expected) > 1:
        if names[: len(expected)] != expected:
            return False, f'called {names}, wanted {expected} in order'
    elif names[0] != expected[0]:
        return False, f'called {names[0]} first, wanted {expected[0]}'

    for key, predicate in case.expect_args.items():
        value = turn.calls[0].arguments.get(key)
        if not predicate(value):
            return False, f'argument {key}={value!r} failed its check'
    return True, ''


def _run_case(case, manifest, ask, *, model: str, with_instructions: bool, repeat: int) -> dict[str, Any]:
    outcomes: list[bool] = []
    notes: list[str] = []
    observed: list[list[dict[str, Any]]] = []
    usages = []
    for _ in range(repeat):
        turn = ask(case.history, case.prompt, model=model, with_instructions=with_instructions)
        usages.append(turn.usage)
        ok, note = _judge(case, turn)
        outcomes.append(ok)
        # The ARGUMENTS, not just the names. The first run proved why: a model
        # called the right tool and put the draft somewhere other than `claim`,
        # and the record could not say where — so the finding needed another
        # paid call to read. Arguments are what makes a result file re-readable.
        observed.append([{'name': call.name, 'arguments': call.arguments} for call in turn.calls])
        if note:
            notes.append(note)
    passes = sum(outcomes)
    verdict = PASSED if passes == repeat else (FAILED if passes == 0 else FLAKY)
    return {
        'case': case.id,
        'group': case.group,
        'verdict': verdict,
        'passes': passes,
        'attempts': repeat,
        'observed': observed,
        'notes': sorted(set(notes)),
        'usage': {
            'input': sum(u.input_tokens for u in usages),
            'output': sum(u.output_tokens for u in usages),
            'cached': sum(u.cached_tokens for u in usages),
            'cache_write': sum(u.cache_write_tokens for u in usages),
        },
    }


def _display_path(path: Path) -> Path:
    """`path` relative to the working directory when it lies inside it, else absolute.

    Right wherever the package sits in the tree, and wherever the run started.
    """
    try:
        return path.relative_to(Path.cwd().resolve())
    except ValueError:
        return path


def _gate(results: list[dict[str, Any]]) -> list[str]:
    """The groups that must be perfect. Others are reported, not gated.

    The two judgement groups have no threshold, so they report and never fail.
    """
    must_be_perfect = ('openai_submission', 'named', 'must_not_fire')
    broken = []
    for group in must_be_perfect:
        bad = [r for r in results if r['group'] == group and r['verdict'] != PASSED]
        if bad:
            broken.append(f'{group}: ' + ', '.join(f'{r["case"]} ({r["verdict"]})' for r in bad))
    return broken


def load_prices(path: str | Path) -> dict[str, tuple[float, float]]:
    """Read a `--prices` file: `{model_prefix: [eur_per_1M_input_tokens, eur_per_1M_output_tokens]}`.

    Raises ValueError on anything else, so a malformed file stops the run
    before it spends rather than printing a wrong ceiling after.
    """
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError('expected a JSON object of model prefix -> [input, output]')
    table: dict[str, tuple[float, float]] = {}
    for prefix, pair in data.items():
        if (
            not isinstance(pair, list | tuple)
            or len(pair) != 2
            or not all(isinstance(v, int | float) and not isinstance(v, bool) and v >= 0 for v in pair)
        ):
            raise ValueError(f'{prefix!r}: expected [eur_per_1M_input_tokens, eur_per_1M_output_tokens], got {pair!r}')
        table[str(prefix)] = (float(pair[0]), float(pair[1]))
    return table


def price_for(table: dict[str, tuple[float, float]], model: str) -> tuple[float, float]:
    """The longest prefix of `model` in `table` wins; no match prices at zero."""
    best, prices = '', (0.0, 0.0)
    for prefix, candidate in table.items():
        if model.startswith(prefix) and len(prefix) > len(best):
            best, prices = prefix, candidate
    return prices


def main(argv: list[str] | None = None) -> int:
    # `python -m <this package>`, whatever the package is called in this layout.
    parser = argparse.ArgumentParser(prog=f'python -m {__package__}' if __package__ else None)
    parser.add_argument('--vendor', choices=['anthropic', 'openai', 'both'], default='both')
    parser.add_argument('--model', default='', help='override the vendor flagship')
    parser.add_argument('--client', default='claude', help='which client manifest to show the model')
    parser.add_argument('--group', default=None, help='run these groups only (comma-separated)')
    parser.add_argument('--case', default=None, help='run these case ids only (comma-separated)')
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument(
        '--no-instructions',
        action='store_true',
        help='show the tools WITHOUT the server instructions (the arm that measures how much they carry)',
    )
    parser.add_argument('--both-arms', action='store_true', help='run every case with AND without instructions')
    parser.add_argument('--print-submission', action='store_true', help='render the OpenAI dashboard text and exit')
    parser.add_argument('--check-fresh', action='store_true', help='exit non-zero if no result matches this wording')
    # A mistyped flag must not be able to spend. This refuses to START a run
    # bigger than the number given, and the default is small enough that the
    # full set needs the number raised on purpose, so a run typed when a
    # wiring check was meant cannot spend.
    parser.add_argument('--max-calls', type=int, default=20, help='refuse to start a run larger than this')
    parser.add_argument(
        '--prices',
        default=None,
        help='JSON file: model prefix -> [eur_per_1M_input_tokens, eur_per_1M_output_tokens]; without it, tokens only',
    )
    args = parser.parse_args(argv)

    # Checked before anything that could spend. `--repeat 0` would run no
    # calls, score `passes == repeat` as 0 == 0, and write an all-pass result
    # file that satisfied --check-fresh.
    if args.repeat < 1:
        print(f'--repeat must be at least 1, got {args.repeat}', file=sys.stderr)
        return 2
    # A bad prices file is refused before anything spends, not after the run.
    price_table: dict[str, tuple[float, float]] | None = None
    if args.prices:
        try:
            price_table = load_prices(args.prices)
        except (OSError, ValueError) as exc:
            print(f'--prices {args.prices}: {exc}', file=sys.stderr)
            return 2
    # An EXPLICITLY empty selector (`--group ","`, `--case "$UNSET"`) would
    # parse to no names and read as "no filter" -- i.e. every case. Omitted is
    # None; given-but-empty is a mistake, and a mistake must not widen a run.
    for flag in ('group', 'case'):
        raw = getattr(args, flag)
        if raw is not None and not [part for part in raw.split(',') if part.strip()]:
            print(f'--{flag} was given but names nothing ({raw!r}); omit it to select everything', file=sys.stderr)
            return 2

    _load_env()
    from evals.tool_choice import cases as cases
    from evals.tool_choice import manifest as manifest_mod
    from evals.tool_choice.vendors import ASK, DEFAULT_MODELS

    if args.print_submission:
        print(cases.render_submission())
        return 0

    manifest = manifest_mod.build(args.client)
    wording = manifest.hash()

    if args.check_fresh:
        # FRESH means the cases the OpenAI submission stands on were measured at
        # THIS wording: every `openai_submission` case, on both vendors, WITH the
        # instructions, at least one attempt each. Accepting any file whose
        # name carried the hash would let a one-case n=20 file make a wording
        # "fresh" that had never been run on the submission set.
        # Scoped to the submission set, because that is what this
        # flag gates: a full 35-case run is the eval, not the freshness check.
        needed = {
            (case.id, vendor) for case in cases.by_group('openai_submission') for vendor in ('anthropic', 'openai')
        }
        for path in sorted(RESULTS_DIR.glob(f'results_*_{wording}.json'), reverse=True):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            # The wording the file RECORDS, not the one its name claims: a
            # renamed or hand-copied file must not vouch for a wording it never
            # measured. And a malformed file is skipped, never fatal -- one bad
            # newer file must not hide a good older one.
            if not isinstance(data, dict) or data.get('wording') != wording:
                continue
            rows = data.get('results')
            if not isinstance(rows, list):
                continue
            covered = {
                (row['case'], row['vendor'])
                for row in rows
                if isinstance(row, dict)
                and isinstance(row.get('case'), str)
                and isinstance(row.get('vendor'), str)
                and row.get('with_instructions') is True
                and isinstance(row.get('attempts'), int)
                and row['attempts'] >= 1
            }
            if needed <= covered:
                print(f'fresh: {path.name} covers the submission set at the current wording ({wording})')
                return 0
        print(
            f'STALE: no result file measures the submission set at wording {wording}.\n'
            'Every openai_submission case, both vendors, with instructions. Run it before the submission:\n'
            '  uv run python -m evals.tool_choice --group openai_submission --vendor both --max-calls 48',
            file=sys.stderr,
        )
        return 1

    # Comma-separated, because a wording change is measured on the groups it can
    # move and a before/after pair has to be ONE run per side: four invocations
    # would be four result files to reconcile, and the arms would not share a
    # prompt cache.
    wanted = [name.strip() for name in (args.group or '').split(',') if name.strip()]
    unknown = [name for name in wanted if name not in cases.GROUPS]
    if unknown:
        print(f'unknown group(s) {unknown}; known: {sorted(cases.GROUPS)}', file=sys.stderr)
        return 2
    selected = [case for case in cases.CASES if case.group in wanted] if wanted else list(cases.CASES)
    # One cell, measured at a higher repeat, is how an n=5 disagreement gets
    # settled; without this the smallest unit is a group, and settling one case
    # would spend on four others nobody asked about.
    ids = [name.strip() for name in (args.case or '').split(',') if name.strip()]
    known_ids = {case.id for case in cases.CASES}
    if [i for i in ids if i not in known_ids]:
        print(f'unknown case id(s) {[i for i in ids if i not in known_ids]}', file=sys.stderr)
        return 2
    if ids:
        selected = [case for case in selected if case.id in ids]
    if not selected:
        print(f'no cases in group {args.group!r}', file=sys.stderr)
        return 2

    # A manifest with no tools or no instructions still hashes, and the run
    # would then measure a server nobody serves -- with no instructions the
    # two arms silently become the same experiment. Refuse before spending.
    if not manifest.tools or not manifest.instructions.strip():
        print(
            f'REFUSED: the manifest has {len(manifest.tools)} model-visible tools and '
            f'{len(manifest.instructions)} chars of instructions; that is not a server to measure.',
            file=sys.stderr,
        )
        return 2

    vendors = ['anthropic', 'openai'] if args.vendor == 'both' else [args.vendor]
    arms = [True, False] if args.both_arms else [not args.no_instructions]

    planned = len(selected) * len(vendors) * len(arms) * args.repeat
    if planned > args.max_calls:
        print(
            f'REFUSED: this run would make {planned} paid model calls, over the --max-calls '
            f'ceiling of {args.max_calls}.\n'
            f'  {len(selected)} cases x {len(vendors)} vendor(s) x {len(arms)} arm(s) x {args.repeat} repeat(s)\n'
            f'Raise it deliberately if that is what you meant: --max-calls {planned}',
            file=sys.stderr,
        )
        return 2

    print(f'client={args.client} wording={wording} tools={len(manifest.tools)} cases={len(selected)}')
    print(f'planned calls: {planned} (ceiling {args.max_calls})')
    all_results: list[dict[str, Any]] = []
    spend: dict[str, dict[str, int]] = {}
    for vendor in vendors:
        model = args.model or DEFAULT_MODELS[vendor]
        for with_instructions in arms:
            arm = 'with instructions' if with_instructions else 'WITHOUT instructions'
            print(f'\n── {vendor} / {model} / {arm} ──')

            def ask(history, prompt, *, model=model, with_instructions=with_instructions, vendor=vendor):
                return ASK[vendor](manifest, history, prompt, model=model, with_instructions=with_instructions)

            for case in selected:
                result = _run_case(
                    case, manifest, ask, model=model, with_instructions=with_instructions, repeat=args.repeat
                )
                result |= {'vendor': vendor, 'model': model, 'with_instructions': with_instructions}
                all_results.append(result)
                totals = spend.setdefault(
                    model, {'vendor': vendor, 'input': 0, 'output': 0, 'cached': 0, 'cache_write': 0, 'calls': 0}
                )
                for key in ('input', 'output', 'cached', 'cache_write'):
                    totals[key] += result['usage'][key]
                totals['calls'] += result['attempts']
                mark = {PASSED: 'ok  ', FLAKY: 'FLAKY', FAILED: 'FAIL'}[result['verdict']]
                detail = f' {result["notes"][0]}' if result['notes'] else ''
                print(f'  {mark} {case.id} ({result["passes"]}/{result["attempts"]}){detail}')

    print('\n── summary ──')
    for vendor in vendors:
        for with_instructions in arms:
            rows = [r for r in all_results if r['vendor'] == vendor and r['with_instructions'] == with_instructions]
            counts = Counter(r['verdict'] for r in rows)
            arm = 'with' if with_instructions else 'without'
            print(
                f'{vendor:10} instructions {arm:8} '
                f'pass {counts[PASSED]}  flaky {counts[FLAKY]}  fail {counts[FAILED]}  of {len(rows)}'
            )
    if args.both_arms:
        print('\n── what the instructions carry (pass rate, with − without) ──')
        for vendor in vendors:
            for group in cases.GROUPS:
                rows = [r for r in all_results if r['vendor'] == vendor and r['group'] == group]
                if not rows:
                    continue
                with_arm = [r for r in rows if r['with_instructions']]
                without = [r for r in rows if not r['with_instructions']]
                if not with_arm or not without:
                    continue

                def rate(rows: list[dict[str, Any]]) -> float:
                    return sum(r['passes'] for r in rows) / max(1, sum(r['attempts'] for r in rows))

                delta = rate(with_arm) - rate(without)
                print(f'{vendor:10} {group:20} {rate(with_arm):.2f} vs {rate(without):.2f}  delta {delta:+.2f}')

    print('\n── spend ──')
    if price_table is None:
        print('  prices were not given (--prices PATH): token totals only.')

    grand = 0.0
    for model, totals in spend.items():
        per_in, per_out = price_for(price_table or {}, model)
        # A CEILING: every input token priced at the full rate, cached or not.
        # The vendors report input differently. Anthropic's `input_tokens`
        # EXCLUDES cache reads and cache writes, so its total is all three.
        # OpenAI's `input_tokens` INCLUDES the cached part, so adding `cached`
        # again would double-count it. A cache write bills at 1.25x, so it is
        # priced at that.
        if totals.get('vendor') == 'openai':
            total_in = totals['input']
        else:
            total_in = totals['input'] + totals['cached'] + totals.get('cache_write', 0)
        write_premium = 0.25 * totals.get('cache_write', 0)
        cost = ((total_in + write_premium) * per_in + totals['output'] * per_out) / 1_000_000
        grand += cost
        cached_share = totals['cached'] / total_in if total_in else 0
        priced = f'  <= EUR {cost:.2f}' if price_table is not None else ''
        print(
            f'{model:20} calls {totals["calls"]:4}  input {total_in:8} '
            f'({totals["cached"]:8} cached, {cached_share:.0%})  out {totals["output"]:6}{priced}'
        )
    if not any(t['cached'] for t in spend.values()):
        print('  NOTE: no cached input reported — the manifest was paid for in full on every call.')
    if price_table is not None:
        print(
            f'{"total":20} <= EUR {grand:.2f} — a CEILING: every input token is priced at full rate, '
            'and a cached read costs a fraction of that.'
        )

    argument_only = [
        r for r in all_results if r['verdict'] != PASSED and any('argument' in note for note in r['notes'])
    ]
    if argument_only:
        print('\n── right tool, wrong argument (the raw calls) ──')
        for r in argument_only:
            print(f'{r["vendor"]:10} {r["case"]}: {r["notes"]}')
            for attempt in r['observed']:
                print(f'    {json.dumps(attempt)[:400]}')

    # Minute-resolution, not date: two runs of the same wording on one day are
    # normal (a first pass and a second one), and a date-only name would let the
    # second silently replace the first's raw arguments — which is the half that cannot
    # be reconstructed without paying again.
    stamp = datetime.now(UTC).strftime('%Y-%m-%dT%H%M')
    out = RESULTS_DIR / f'results_{stamp}_{wording}.json'
    out.write_text(
        json.dumps(
            {
                'wording': wording,
                'client': args.client,
                'user_agent': manifest.user_agent,
                'instructions_chars': len(manifest.instructions),
                'tools': manifest.tool_names,
                'repeat': args.repeat,
                'models': sorted(spend),
                'spend': spend,
                'results': all_results,
            },
            indent=2,
        )
        + '\n'
    )
    print(f'\nwritten: {_display_path(out)}')

    broken = _gate(all_results)
    if broken:
        print('\nGATES BROKEN:', file=sys.stderr)
        for line in broken:
            print(f'  {line}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
