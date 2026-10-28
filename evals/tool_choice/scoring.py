"""How an attempt is scored, and what counts as the gate being met.

Deterministic: no model judges. Two questions live here.

1. `judge`: did ONE attempt satisfy ONE case?
2. `gate`: given a run's rows, is the release gate met?

The gate is a statement about a whole MATRIX of case x vendor x instruction arm,
not about whatever rows a run happened to produce. A gate that checks only the
rows it is handed passes on an empty list, on a run restricted to one vendor and
on a run that crashed halfway; so `gate` is told which rows to expect, and a row
that is missing, errored or short of attempts is reported as INCOMPLETE rather
than silently skipped.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from evals.tool_choice import cases as case_data
from evals.tool_choice.vendors import DISTRACTOR_NAME

# A case passes when every attempt does, is FLAKY at some, fails at none. Flaky
# is reported separately and on purpose: a trigger that fires two times in three
# is a finding about the wording, not noise to be averaged away. ERRORED is an
# attempt the vendor call itself failed (rate limit, outage, bad key): the row
# says nothing about the wording either way and is never read as a pass or a fail.
PASSED = 'pass'
FLAKY = 'flaky'
FAILED = 'fail'
ERRORED = 'error'

# The release check (`--check-fresh`) needs this many attempts per row, every one
# a pass. Iteration runs use fewer (the CLI default is 3) and are not enough.
RELEASE_REPEAT = 5

# ── what is gated ──────────────────────────────────────────────────────

# Whole groups that must be perfect: the submission's own cases, every case that
# names Lenz, and every case where Lenz must stay quiet.
GATED_GROUPS = ('openai_submission', 'named', 'must_not_fire')

# Single cases gated beside the groups: the consent rule. Ten credits must not
# leave on a shrug, a thank-you, or an explicit no. (`escalate-on-yes`,
# `escalate-not-twelve-rows`, `escalate-all-rows-on-request` and the injection case are reported until they have
# a clean measured run; promote them here after one.)
GATED_CASE_IDS = ('escalate-needs-asking', 'escalate-not-on-thanks', 'escalate-declined')

# The arms a model is gated on, as (vendor, with_instructions). Claude is handed
# the server's instructions; ChatGPT is NOT (it reads the tool descriptions
# alone), so for OpenAI the arm that stands for the real client is the one
# WITHOUT them. The with-instructions OpenAI arm was gated before and stays so
# (the Responses API and other hosts do pass instructions).
GATED_ARMS = (('anthropic', True), ('openai', True), ('openai', False))

# Every arm the release file must contain, gated or not. Claude without
# instructions is measured so the delta is on record, but is not a client.
REQUIRED_ARMS = (*GATED_ARMS, ('anthropic', False))

Key = tuple[str, str, bool]  # (case id, vendor, with_instructions)


def is_gated_case(case: case_data.Case) -> bool:
    return case.group in GATED_GROUPS or case.id in GATED_CASE_IDS


def is_gated_arm(vendor: str, with_instructions: bool) -> bool:
    return (vendor, with_instructions) in GATED_ARMS


def required_matrix(
    cases: Iterable[case_data.Case] | None = None, arms: Iterable[tuple[str, bool]] = REQUIRED_ARMS
) -> set[Key]:
    """Every (case, vendor, arm) a release must have measured: gated cases x required arms."""
    cases = case_data.CASES if cases is None else cases
    return {(case.id, vendor, instr) for case in cases if is_gated_case(case) for vendor, instr in arms}


# ── one attempt ───────────────────────────────────────────────────────


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _assess_ok(args: dict[str, Any]) -> bool:
    """`assess_claim` takes ONE text in `claim` or a list in `claims`, never both, never neither."""
    claim, claims = args.get('claim'), args.get('claims')
    if _text(claim):
        return not claims
    return isinstance(claims, list) and bool(claims) and all(_text(item) for item in claims) and not claim


def _verify_ok(args: dict[str, Any]) -> bool:
    """`verify_claim` takes ONE text in `claim` or 1..10 in `claims`, never both, never neither."""
    claim, claims = args.get('claim'), args.get('claims')
    if _text(claim):
        return not claims
    return isinstance(claims, list) and 1 <= len(claims) <= 10 and all(_text(item) for item in claims) and not claim


def _get_verification_ok(args: dict[str, Any]) -> bool:
    """`get_verification` takes ONE id in `task_id` or 1..10 in `task_ids`, never both, never neither."""
    task_id, task_ids = args.get('task_id'), args.get('task_ids')
    if _text(task_id):
        return not task_ids
    return (
        isinstance(task_ids, list)
        and 1 <= len(task_ids) <= 10
        and all(_text(item) for item in task_ids)
        and not task_id
    )


def _call_claims(call) -> list[str]:
    """The claim string(s) a tool call submitted (`claim` or `claims`)."""
    raw_list = call.arguments.get('claims')
    if isinstance(raw_list, list) and raw_list:
        return [str(c) for c in raw_list if _text(c)]
    single = call.arguments.get('claim')
    return [str(single)] if _text(single) else []


_ARGS_OK = {
    'assess_claim': _assess_ok,
    'verify_claim': _verify_ok,
    'select_claims': lambda a: (
        _text(a.get('task_id'))
        and isinstance(a.get('claims'), list)
        and bool(a['claims'])
        and all(_text(c) for c in a['claims'])
    ),
    'get_verification': _get_verification_ok,
    'ask_followup': lambda a: _text(a.get('verification_id')) and _text(a.get('question')),
}


def judge(case: case_data.Case, turn) -> tuple[bool, str]:
    """Did this attempt satisfy the case: its calls, then (when the case says
    what the user must be told) its reply. Deterministic; no model judges."""
    ok, note = _judge_calls(case, turn)
    # A case that allows calls (RESTRAINT) judges the reply only on a turn that
    # made none: a call that does the work is an answer too.
    made_calls = any(call.name != DISTRACTOR_NAME for call in turn.calls)
    judge_reply = case.reply is not None and (case.expect != case_data.RESTRAINT or not made_calls)
    if ok and judge_reply and not case.reply(turn.text or ''):
        return False, 'the reply does not say what this case requires'
    return ok, note


def _judge_calls(case: case_data.Case, turn) -> tuple[bool, str]:
    """Did this attempt make the right calls? Deterministic; no model judges.

    A call to the distractor `web_search` is never a Lenz call: it is dropped
    before scoring (a model that searches the web instead of staying quiet has not
    triggered Lenz), and a positive case that ends up with no Lenz call says so.
    """
    calls = [call for call in turn.calls if call.name != DISTRACTOR_NAME]
    names = [call.name for call in calls]
    searched = len(calls) != len(turn.calls)

    for forbidden in case.forbid:
        if forbidden in names:
            return False, f'called the forbidden {forbidden}'

    if case.expect == case_data.NONE:
        return (not names), ('called ' + ', '.join(names) if names else '')

    if case.expect == case_data.RESTRAINT:
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
        return False, 'called nothing' + (' (searched the web instead)' if searched else '')

    if case.count > 1:
        return _judge_fan_out(case, calls, turn.batches or (tuple(call.name for call in turn.calls),))

    # Only the expected tools, and the case's declared extras, may be called; and
    # none of them twice (a second call is a second charge). Without this a
    # correct first call followed by an invented second one would pass.
    allowed = set(expected) | set(case.also_allowed)
    for name in names:
        if name not in allowed:
            return False, f'called {name}, which this case does not allow'
    for name in sorted(set(names)):
        if names.count(name) > 1:
            return False, f'called {name} {names.count(name)} times; this case must call it once'

    if case.independent:
        # Every expected tool, once each, in any order: the pair does not depend on
        # either call's result. (A model that asks for one and then the other over two
        # turns is valid in the apps, but this eval sees one turn and a case that
        # passed on half the request would not be vouching for the whole of it.)
        if sorted(names) != sorted(expected):
            return False, f'called {names}, wanted all of {expected} in this turn (any order)'
    elif names[: len(expected)] != expected:
        return False, f'called {names}, wanted {expected}' + (' in order' if len(expected) > 1 else ' first')

    # Every call to a tool with required arguments must be well formed: an
    # `assess_claim({})` is an error result from the server, not a check.
    for call in calls:
        valid = _ARGS_OK.get(call.name)
        if call.name in allowed and valid is not None and not valid(call.arguments):
            return (
                False,
                f'argument check: {call.name} was called with arguments the server would refuse: {call.arguments!r}',
            )

    if case.expect_args and not case.independent:
        first = next(call for call in calls if call.name == expected[0])
        for key, predicate in case.expect_args.items():
            value = first.arguments.get(key)
            if not predicate(value):
                return False, f'argument {key}={value!r} failed its check'
    return True, ''


def _judge_fan_out(case: case_data.Case, calls, turn_batches: tuple[tuple[str, ...], ...] = ()) -> tuple[bool, str]:
    """The one expected tool covering `case.count` distinct claims (either one per call
    or batched in `claims`), with the case's extras allowed once each before or between them."""
    (tool,) = case.expected_tools
    names = [call.name for call in calls]
    for name in names:
        if name != tool and name not in case.also_allowed:
            return False, f'called {name}, which this case does not allow'
    for name in case.also_allowed:
        # Waiting on a started check is polling, once per check or more; any
        # other extra (a balance check) is once.
        if name != 'get_verification' and names.count(name) > 1:
            return False, f'called {name} {names.count(name)} times; at most once'
    fanned = [call for call in calls if call.name == tool]
    valid = _ARGS_OK.get(tool)
    for call in fanned:
        if valid is not None and not valid(call.arguments):
            return (
                False,
                f'argument check: {tool} was called with arguments the server would refuse: {call.arguments!r}',
            )
        for key, predicate in case.expect_args.items():
            if (
                key == 'claim'
                and 'claim' not in call.arguments
                and isinstance(call.arguments.get('claims'), list)
                and not predicate(None)
            ):
                for item in call.arguments['claims']:
                    if not predicate(item):
                        return False, f'argument claims item {item!r} failed its check'
                continue
            value = call.arguments.get(key)
            if not predicate(value):
                return False, f'argument {key}={value!r} failed its check'

    # Compute claims per call so both 1-claim-per-call fan-out and `claims=[...]`
    # batch calls are scored on how many claims they submit per turn and in total.
    per_call_counts = [max(1, len(_call_claims(call))) for call in fanned]
    total_submitted = sum(len(_call_claims(call)) for call in fanned)
    if case.batch:
        call_idx = 0
        widest = 0
        for batch_names in turn_batches:
            in_turn = batch_names.count(tool)
            turn_claims = sum(per_call_counts[call_idx : call_idx + in_turn])
            call_idx += in_turn
            widest = max(widest, turn_claims, in_turn)
        if call_idx < len(per_call_counts):
            widest = max(widest, sum(per_call_counts[call_idx:]))
        if widest > case.batch:
            return False, f'started {widest} calls in one turn; batches of at most {case.batch}'
    if total_submitted != case.count:
        return False, f'called {tool} {total_submitted} times; wanted {case.count}'
    claims = {' '.join(c.split()).lower() for call in fanned for c in _call_claims(call)}
    if len(claims) != case.count:
        return False, f'{tool} covered {len(claims)} different claims; wanted {case.count}'
    return True, ''


# ── the gate ──────────────────────────────────────────────────────────


@dataclass
class Gate:
    """What a run says about the release gate.

    `failed`: a gated row ran and did not pass. `incomplete`: a row the gate needs
    is missing, errored, or short of attempts, so the run cannot vouch either way.
    """

    failed: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.incomplete


def row_key(row: dict[str, Any]) -> Key | None:
    """The cell a result row belongs to, or None for a row that cannot be placed.

    A distractor-arm row is report-only and never fills a gate cell.
    """
    if row.get('distractor'):
        return None
    case, vendor, instr = row.get('case'), row.get('vendor'), row.get('with_instructions')
    if isinstance(case, str) and isinstance(vendor, str) and isinstance(instr, bool):
        return (case, vendor, instr)
    return None


def gate(results: list[dict[str, Any]], expected: Iterable[Key] | None = None, *, min_attempts: int = 1) -> Gate:
    """Judge a run against the rows it was supposed to contain.

    `expected` defaults to the COMPLETE required matrix, so `gate([])` is not met.
    A run narrowed on purpose (one group, one vendor) passes the rows it planned.
    Rows in a gated arm must PASS; rows in a required but ungated arm only have to
    be present and error-free. `min_attempts` is the repeat count each needed row
    must have been run at.
    """
    wanted = required_matrix() if expected is None else set(expected)
    by_key: dict[Key, dict[str, Any]] = {}
    for row in results:
        key = row_key(row)
        if key is not None:
            by_key[key] = row
    verdict = Gate()
    for case_id, vendor, instr in sorted(wanted):
        label = f'{case_id} ({vendor}, {"with" if instr else "without"} instructions)'
        row = by_key.get((case_id, vendor, instr))
        if row is None:
            verdict.incomplete.append(f'{label}: no result')
            continue
        attempts = row.get('attempts')
        if not isinstance(attempts, int) or attempts < min_attempts:
            verdict.incomplete.append(f'{label}: {attempts} attempt(s), needs {min_attempts}')
            continue
        if row.get('verdict') == ERRORED or row.get('errors'):
            verdict.incomplete.append(f'{label}: {row.get("errors", "?")} attempt(s) errored')
            continue
        # The stored verdict is not taken on trust: a row passes only when its own
        # counts say so and the raw calls it was judged on are on record.
        observed = row.get('observed')
        if not isinstance(observed, list) or len(observed) != attempts:
            verdict.incomplete.append(f'{label}: the raw calls are not on record for every attempt')
            continue
        passed = row.get('verdict') == PASSED and row.get('passes') == attempts
        if is_gated_arm(vendor, instr) and not passed:
            verdict.failed.append(f'{label}: {row.get("verdict")} ({row.get("passes")}/{attempts})')
    return verdict
