"""Run the tool-choice eval.

    uv run python -m evals.tool_choice --vendor both --repeat 3
    uv run python -m evals.tool_choice --gated --both-arms --repeat 5 --max-calls 600   # the release run
    uv run python -m evals.tool_choice --print-submission
    uv run python -m evals.tool_choice --check-fresh
    uv run python -m evals.tool_choice --prices prices.json ...

Makes PAID model calls. Not in the pytest path; a structural test checks the
cases without a model.

Exit codes: 0 the planned rows ran and every gated one passed; 1 a gated row
failed; 2 bad arguments (nothing spent); 3 the run is incomplete (a vendor call
errored, or the run was stopped) and cannot vouch either way.

`--prices PATH` prices the spend summary: a JSON object mapping a model-name
prefix to `[eur_per_1M_input_tokens, eur_per_1M_output_tokens]` (the longest matching
prefix wins). Without it the summary prints token totals only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.tool_choice.scoring import ERRORED, FAILED, FLAKY, PASSED, RELEASE_REPEAT
from evals.tool_choice.scoring import gate as _gate
from evals.tool_choice.scoring import judge as _judge

RESULTS_DIR = Path(__file__).resolve().parent


def _load_env() -> None:
    """Vendor API keys from a `.env` in the working directory, when there is
    one and python-dotenv is installed. The environment always wins."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path.cwd() / '.env', override=False)


# Stop a run after this many vendor errors in a row: a revoked key or an outage
# would otherwise spend the rest of the run's time (and, for a half-failing vendor,
# money) on calls that cannot say anything.
MAX_CONSECUTIVE_ERRORS = 5


def _run_case(
    case, manifest, ask, *, model: str, with_instructions: bool, repeat: int, state: dict[str, int] | None = None
) -> dict[str, Any]:
    """Run one case `repeat` times. A failed vendor call is an ERRORED attempt, never a pass or a fail.

    One rate limit or outage on attempt 17 of 100 must not lose the paid output
    of the other 99, and it must not read as a regression of the wording either.
    """
    outcomes: list[bool] = []
    notes: list[str] = []
    observed: list[list[dict[str, Any]]] = []
    usages = []
    errors = 0
    state = state if state is not None else {'consecutive_errors': 0}
    for _ in range(repeat):
        try:
            turn = ask(case.history, case.prompt, model=model, with_instructions=with_instructions)
        except Exception as exc:  # noqa: BLE001 -- any vendor failure is an errored attempt
            errors += 1
            state['consecutive_errors'] = state.get('consecutive_errors', 0) + 1
            notes.append(f'errored: {type(exc).__name__}')
            observed.append([])
            if state['consecutive_errors'] >= MAX_CONSECUTIVE_ERRORS:
                break
            continue
        state['consecutive_errors'] = 0
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
    attempts = len(observed)
    if errors:
        verdict = ERRORED
    else:
        verdict = PASSED if passes == repeat else (FAILED if passes == 0 else FLAKY)
    return {
        'case': case.id,
        'group': case.group,
        'verdict': verdict,
        'passes': passes,
        'attempts': attempts,
        'errors': errors,
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


def _write_results(path: Path, payload: dict[str, Any]) -> None:
    """Write the result file whole or not at all (a crash mid-write must not leave half a file)."""
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, indent=2) + '\n')
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    # `python -m <this package>`, whatever the package is called in this layout.
    parser = argparse.ArgumentParser(prog=f'python -m {__package__}' if __package__ else None)
    parser.add_argument('--vendor', choices=['anthropic', 'openai', 'both'], default='both')
    parser.add_argument(
        '--model', default='', help='override the vendor default (the result is then not release-fresh)'
    )
    parser.add_argument('--client', default='claude', help='which client manifest to show the model')
    parser.add_argument('--group', default=None, help='run these groups only (comma-separated)')
    parser.add_argument('--case', default=None, help='run these case ids only (comma-separated)')
    parser.add_argument(
        '--gated',
        action='store_true',
        help='run exactly the gated cases (the release set); combine with --both-arms --repeat 5',
    )
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument(
        '--no-instructions',
        action='store_true',
        help='show the tools WITHOUT the server instructions (ChatGPT never shows its model them)',
    )
    parser.add_argument('--both-arms', action='store_true', help='run every case with AND without instructions')
    parser.add_argument(
        '--distractor',
        action='store_true',
        help='also offer a web_search tool, as the real apps do. Report-only: never gated, never release-fresh',
    )
    parser.add_argument('--print-submission', action='store_true', help='render the OpenAI dashboard text and exit')
    parser.add_argument(
        '--check-fresh',
        action='store_true',
        help='exit non-zero unless a complete, passing result file covers the whole gated matrix at the current eval',
    )
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
    from evals.tool_choice import freshness, scoring
    from evals.tool_choice import manifest as manifest_mod
    from evals.tool_choice.vendors import ASK, DEFAULT_MODELS

    if args.print_submission:
        print(cases.render_submission())
        return 0

    manifest = manifest_mod.build(args.client)
    wording = manifest.hash()
    harness = freshness.harness_hash()
    key = freshness.eval_key(manifest)

    if args.check_fresh:
        # FRESH means a result file may vouch for the eval AS IT IS NOW: the same
        # wording AND harness (cases, scorer, vendor wiring, built histories), the
        # current default models, a run that finished, and every row of the whole
        # gated matrix (every gated case, both vendors, both instruction arms)
        # present at the release repeat count and passing. A file that merely
        # carries the right hash, or covers the submission set only, is not that.
        ok, why = freshness.check_fresh(RESULTS_DIR, key, DEFAULT_MODELS)
        if ok:
            print(f'fresh: {why} (key {key})')
            return 0
        planned = len(scoring.required_matrix()) * RELEASE_REPEAT
        print(
            f'STALE (key {key}): {why}.\n'
            'The release check needs one complete run of every gated case, both vendors, both instruction\n'
            f'arms, at {RELEASE_REPEAT} attempts each, all passing:\n'
            f'  uv run python -m evals.tool_choice --gated --both-arms --repeat {RELEASE_REPEAT} --max-calls {planned}',
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
    if args.gated:
        selected = [case for case in selected if scoring.is_gated_case(case)]
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
        print(f'no cases selected (group {args.group!r}, case {args.case!r}, gated {args.gated})', file=sys.stderr)
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

    # Keys are read per call (`os.environ[...]`), so a missing one would otherwise
    # be an errored attempt on every call. Say so before the first one.
    key_names = {'anthropic': 'ANTHROPIC_API_KEY', 'openai': 'OPENAI_API_KEY'}
    missing = [key_names[v] for v in vendors if not os.environ.get(key_names[v])]
    if missing:
        print(
            f'REFUSED: {", ".join(missing)} is not set (environment or a .env in the working directory).',
            file=sys.stderr,
        )
        return 2

    # The cells this run was asked to fill: what the gate is held to. A run narrowed
    # on purpose (one group, one vendor) is judged on its own plan, and says so; a
    # crash or an errored row still leaves the plan unmet.
    plan = {
        (case.id, vendor, instr)
        for case in selected
        if scoring.is_gated_case(case)
        for vendor in vendors
        for instr in arms
        if not args.distractor
    }
    full_matrix = plan >= scoring.required_matrix() and args.repeat >= RELEASE_REPEAT and not args.model

    print(
        f'client={args.client} key={key} wording={wording} harness={harness} tools={len(manifest.tools)} cases={len(selected)}'
    )
    print(f'planned calls: {planned} (ceiling {args.max_calls})')
    all_results: list[dict[str, Any]] = []
    spend: dict[str, dict[str, int]] = {}
    stamp = datetime.now(UTC).strftime('%Y-%m-%dT%H%M')
    out = RESULTS_DIR / f'results_{stamp}_{key}.json'
    # A second run in the same minute must not replace the first's raw arguments,
    # which cannot be rebuilt without paying again.
    suffix = 1
    while out.exists():
        suffix += 1
        out = RESULTS_DIR / f'results_{stamp}-{suffix}_{key}.json'

    def snapshot(*, complete: bool) -> dict[str, Any]:
        return {
            'key': key,
            'wording': wording,
            'harness': harness,
            'complete': complete,
            'client': args.client,
            'user_agent': manifest.user_agent,
            'instructions_chars': len(manifest.instructions),
            'tools': manifest.tool_names,
            'repeat': args.repeat,
            'distractor': args.distractor,
            'models': sorted(spend),
            'spend': spend,
            'results': all_results,
        }

    state = {'consecutive_errors': 0}
    stopped = False
    try:
        for vendor in vendors:
            model = args.model or DEFAULT_MODELS[vendor]
            for with_instructions in arms:
                arm = 'with instructions' if with_instructions else 'WITHOUT instructions'
                extra = ' + web_search' if args.distractor else ''
                print(f'\n── {vendor} / {model} / {arm}{extra} ──')

                def ask(
                    history,
                    prompt,
                    *,
                    model=model,
                    with_instructions=with_instructions,
                    vendor=vendor,
                ):
                    return ASK[vendor](
                        manifest,
                        history,
                        prompt,
                        model=model,
                        with_instructions=with_instructions,
                        distractor=args.distractor,
                    )

                for case in selected:
                    result = _run_case(
                        case,
                        manifest,
                        ask,
                        model=model,
                        with_instructions=with_instructions,
                        repeat=args.repeat,
                        state=state,
                    )
                    result |= {
                        'vendor': vendor,
                        'model': model,
                        'with_instructions': with_instructions,
                        'distractor': args.distractor,
                    }
                    all_results.append(result)
                    totals = spend.setdefault(
                        model, {'vendor': vendor, 'input': 0, 'output': 0, 'cached': 0, 'cache_write': 0, 'calls': 0}
                    )
                    for usage_key in ('input', 'output', 'cached', 'cache_write'):
                        totals[usage_key] += result['usage'][usage_key]
                    totals['calls'] += result['attempts'] - result['errors']
                    # Written after EVERY case, so a crash, a ctrl-C or a vendor outage on
                    # case 34 of 35 keeps the paid output of the other 34.
                    _write_results(out, snapshot(complete=False))
                    mark = {PASSED: 'ok  ', FLAKY: 'FLAKY', FAILED: 'FAIL', ERRORED: 'ERROR'}[result['verdict']]
                    detail = f' {result["notes"][0]}' if result['notes'] else ''
                    print(f'  {mark} {case.id} ({result["passes"]}/{result["attempts"]}){detail}')
                    if state['consecutive_errors'] >= MAX_CONSECUTIVE_ERRORS:
                        print(
                            f'\nSTOPPED: {MAX_CONSECUTIVE_ERRORS} vendor errors in a row ({vendor}); not spending more.',
                            file=sys.stderr,
                        )
                        stopped = True
                        break
                if stopped:
                    break
            if stopped:
                break
    except KeyboardInterrupt:
        print('\nINTERRUPTED: keeping the rows written so far.', file=sys.stderr)
        stopped = True

    print('\n── summary ──')
    for vendor in vendors:
        for with_instructions in arms:
            rows = [r for r in all_results if r['vendor'] == vendor and r['with_instructions'] == with_instructions]
            counts = Counter(r['verdict'] for r in rows)
            arm = 'with' if with_instructions else 'without'
            print(
                f'{vendor:10} instructions {arm:8} '
                f'pass {counts[PASSED]}  flaky {counts[FLAKY]}  fail {counts[FAILED]}  error {counts[ERRORED]}  of {len(rows)}'
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

    verdict = _gate(all_results, expected=plan)
    complete = not stopped and not verdict.incomplete
    _write_results(out, snapshot(complete=complete))
    print(f'\nwritten: {_display_path(out)}')

    if args.distractor:
        print('\nGATE: not applied (the web_search arm is report-only).')
        return 3 if stopped or any(r['errors'] for r in all_results) else 0
    if full_matrix and complete and verdict.ok:
        print(f'\nGATE: met over the whole matrix at {args.repeat} attempts. Run --check-fresh to confirm.')
    elif not full_matrix:
        print(
            '\nGATE: judged on the rows this run planned. It is NOT the release check: that needs --gated '
            f'--both-arms --repeat {RELEASE_REPEAT}, both vendors, default models.'
        )
    if verdict.failed:
        print('\nGATES BROKEN:', file=sys.stderr)
        for line in verdict.failed:
            print(f'  {line}', file=sys.stderr)
        return 1
    if verdict.incomplete or stopped:
        print('\nINCOMPLETE (a vendor error or a stopped run; this says nothing about the wording):', file=sys.stderr)
        for line in verdict.incomplete:
            print(f'  {line}', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
