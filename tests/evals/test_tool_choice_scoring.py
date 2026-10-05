"""The tool-choice eval's scorer, gate and freshness rules, checked without a model.

Each test asks the question "can this fail?" of one thing the release gate rests
on: a scorer that accepts a wrong call, a gate that passes on no rows, a freshness
check that a stale file satisfies. None of them makes a vendor call.
"""

from __future__ import annotations

import dataclasses
import json
import re

import pytest

from evals.tool_choice import cases, freshness, manifest, scoring
from evals.tool_choice import vendors as vendors_mod
from evals.tool_choice.vendors import DISTRACTOR_NAME, Call, Turn


def _turn(*calls: tuple[str, dict]) -> Turn:
    return Turn(calls=[Call(name, args) for name, args in calls], text='')


def _draft(case_id: str) -> str:
    return cases.by_id(case_id).prompt.split('"', 1)[1].rsplit('"', 1)[0]


# ── the scorer ─────────────────────────────────────────────────────────


@pytest.mark.parametrize('case_id', ['submission-1-quick', 'named-quick', 'house-no-usage-preflight'])
def test_an_assess_call_with_no_text_fails(case_id):
    case = cases.by_id(case_id)
    for args in ({}, {'claim': ''}, {'claim': '   '}, {'claims': []}, {'claim': None, 'claims': None}):
        ok, _ = scoring.judge(case, _turn(('assess_claim', args)))
        assert not ok, args


def test_an_assess_call_with_the_right_text_passes():
    case = cases.by_id('named-quick')
    ok, note = scoring.judge(case, _turn(('assess_claim', {'claim': 'Do 90% of startups fail within a year?'})))
    assert (ok, note) == (True, '')
    # The claim must be the one the user named, not just some string.
    assert not scoring.judge(case, _turn(('assess_claim', {'claim': 'Lightning strikes twice.'})))[0]
    # Both `claim` and `claims` is an error result from the server.
    both = {'claim': 'Startups fail 90% of the time.', 'claims': ['Startups fail 90% of the time.']}
    assert not scoring.judge(case, _turn(('assess_claim', both)))[0]


def test_claims_items_must_be_the_users_statements_in_order():
    case = cases.by_id('named-list-uses-claims')
    good = ['Portugal borders only Spain.', 'Lisbon is on the Tagus.', 'The Azores are in the Pacific.']
    assert scoring.judge(case, _turn(('assess_claim', {'claims': good})))[0]
    assert scoring.judge(case, _turn(('assess_claim', {'claims': ['(1) ' + good[0], *good[1:]]})))[0]
    # Right length, wrong content (the shape the old length-only check accepted).
    assert not scoring.judge(case, _turn(('assess_claim', {'claims': [1, 2, 3]})))[0]
    assert not scoring.judge(case, _turn(('assess_claim', {'claims': ['a', 'b', 'c']})))[0]
    # Out of order: rows come back by index, so order is part of the contract.
    assert not scoring.judge(case, _turn(('assess_claim', {'claims': [good[1], good[0], good[2]]})))[0]
    # An invented assertion appended to one item.
    padded = [good[0] + ' It also borders France and Andorra, and joined the EU in 1986.', *good[1:]]
    assert not scoring.judge(case, _turn(('assess_claim', {'claims': padded})))[0]
    assert not scoring.judge(case, _turn(('assess_claim', {'claims': [good[0][:-1] + ' and France.', *good[1:]]})))[0]
    # The whole text in `claim` is the wrong half of the pair here.
    assert not scoring.judge(case, _turn(('assess_claim', {'claim': ' '.join(good)})))[0]


@pytest.mark.parametrize('case_id', ['submission-2-draft', 'named-draft', 'named-draft-hedged'])
def test_a_draft_with_an_appended_assertion_fails(case_id):
    case = cases.by_id(case_id)
    draft = _draft(case_id)
    assert scoring.judge(case, _turn(('assess_claim', {'claim': draft})))[0]
    padded = draft + ' It is also the most visited monument in the world.'
    assert not scoring.judge(case, _turn(('assess_claim', {'claim': padded})))[0]
    # However short the addition is.
    assert not scoring.judge(case, _turn(('assess_claim', {'claim': draft + ' It is tall.'})))[0]
    assert not scoring.judge(case, _turn(('assess_claim', {'claim': draft.rsplit('. ', 1)[0] + '.'})))[0]
    # Case, spacing, quote marks and the closing period are not the model's to be judged on.
    assert scoring.judge(case, _turn(('assess_claim', {'claim': f'  "{draft.upper()}"  '})))[0]
    assert scoring.judge(case, _turn(('assess_claim', {'claim': draft.rstrip('.')})))[0]


def test_a_correct_first_call_followed_by_another_fails():
    case = cases.by_id('named-quick')
    first = ('assess_claim', {'claim': 'Do 90% of startups fail within a year?'})
    assert not scoring.judge(case, _turn(first, ('select_claims', {'task_id': 'x', 'claims': ['y']})))[0]
    assert not scoring.judge(case, _turn(first, first))[0]
    assert not scoring.judge(case, _turn(first, ('check_usage', {})))[0]
    assert scoring.judge(case, _turn(first))[0]


def test_an_independent_pair_is_unordered_but_needs_both():
    case = cases.by_id('submission-5-housekeeping')
    assert scoring.judge(case, _turn(('list_verifications', {}), ('check_usage', {})))[0]
    assert scoring.judge(case, _turn(('check_usage', {}), ('list_verifications', {})))[0]
    # Half the request is not the request: one tool alone, nothing, a repeat, or a stranger fails.
    assert not scoring.judge(case, _turn(('check_usage', {})))[0]
    assert not scoring.judge(case, _turn(('list_verifications', {})))[0]
    assert not scoring.judge(case, _turn())[0]
    assert not scoring.judge(case, _turn(('check_usage', {}), ('check_usage', {})))[0]
    assert not scoring.judge(case, _turn(('list_verifications', {}), ('assess_claim', {'claim': 'x'})))[0]
    assert not scoring.judge(case, _turn(('list_verifications', {}), ('verify_claim', {'claim': 'x'})))[0]
    both_plus = _turn(('list_verifications', {}), ('check_usage', {}), ('check_usage', {}))
    assert not scoring.judge(case, both_plus)[0]


@pytest.mark.parametrize(
    'case_id', [c.id for c in cases.CASES if c.expect == 'assess_claim' and c.group == 'unnamed_triggers']
)
def test_an_unnamed_trigger_never_starts_a_deep_check_unasked(case_id):
    case = cases.by_id(case_id)
    assert 'verify_claim' in case.forbid
    args = {'claim': _draft(case_id) if 'paragraph' in case_id else 'x'}
    quick = ('assess_claim', args)
    deep = ('verify_claim', {'claim': 'x'})
    assert 'forbidden' in scoring.judge(case, _turn(quick, deep))[1]


def test_every_assess_expected_case_forbids_the_deep_check_unless_sources_were_asked_for():
    for case in cases.CASES:
        if case.expect == 'assess_claim':
            assert 'verify_claim' in case.forbid, case.id


def test_every_assess_and_verify_case_checks_the_text_it_was_given():
    # An argument-free pass was the original hole: `assess_claim({})` satisfied these.
    for case in cases.CASES:
        if case.expect in ('assess_claim', 'verify_claim'):
            assert 'claim' in case.expect_args or 'claims' in case.expect_args, case.id
            if case.expect == 'assess_claim':
                # And the OTHER form is ruled out, so one text is one `claim` and a list is `claims`.
                assert {'claim', 'claims'} <= set(case.expect_args), case.id


def test_the_escalation_consent_cases_are_gated_and_present():
    for case_id in ('escalate-needs-asking', 'escalate-not-on-thanks', 'escalate-declined'):
        case = cases.by_id(case_id)
        assert scoring.is_gated_case(case), case_id
        assert case.expect == cases.NONE
        assert 'verify_claim' in case.forbid
    declined = cases.by_id('escalate-declined')
    assert 'assess_claim' in declined.forbid
    assert scoring.judge(declined, _turn())[0]
    assert not scoring.judge(declined, _turn(('verify_claim', {'claim': 'x'})))[0]
    assert not scoring.judge(declined, _turn(('assess_claim', {'claim': 'x'})))[0]


def test_the_new_coverage_cases_exist():
    wanted = {
        'quiet-plain-factual-question': cases.NONE,
        'quiet-blog-post-with-facts': cases.NONE,
        'select-claims-after-picker': 'select_claims',
        'get-verification-after-wait': 'get_verification',
        'get-verification-follows-submitted': 'get_verification',
        'depth-low-requested': 'verify_claim',
        'language-explicit-german': 'assess_claim',
        'trigger-german-doubt': 'assess_claim',
        'trigger-quote-attribution': 'assess_claim',
        'escalate-injected-result': cases.NONE,
    }
    for case_id, expect in wanted.items():
        assert cases.by_id(case_id).expect == expect, case_id
    # The two plain-question shapes are over-trigger cases, so they are in a gated group.
    assert scoring.is_gated_case(cases.by_id('quiet-plain-factual-question'))
    assert scoring.is_gated_case(cases.by_id('quiet-blog-post-with-facts'))


def test_depth_low_is_asserted_and_empty_depth_is_not_a_legal_value():
    low = cases.by_id('depth-low-requested')
    assert scoring.judge(low, _turn(('verify_claim', {'claim': 'Mount Everest grows.', 'depth': 'low'})))[0]
    assert not scoring.judge(low, _turn(('verify_claim', {'claim': 'Mount Everest grows.', 'depth': 'standard'})))[0]
    standard = cases.by_id('named-sources')
    claim = 'The Great Wall of China is visible from the Moon.'
    assert scoring.judge(standard, _turn(('verify_claim', {'claim': claim})))[0]
    assert scoring.judge(standard, _turn(('verify_claim', {'claim': claim, 'depth': 'standard'})))[0]
    assert not scoring.judge(standard, _turn(('verify_claim', {'claim': claim, 'depth': 'low'})))[0]
    assert not scoring.judge(standard, _turn(('verify_claim', {'claim': claim, 'depth': ''})))[0]


def test_select_claims_must_name_the_task_and_the_exact_offered_text():
    case = cases.by_id('select-claims-after-picker')
    exchange = next(t for t in case.history if isinstance(t, cases.ToolExchange))
    task_id = exchange.result['task_id']
    offered = [c['text'] for c in exchange.result['claims']]
    ok = ('select_claims', {'task_id': task_id, 'claims': [offered[1]]})
    assert scoring.judge(case, _turn(ok))[0]
    assert not scoring.judge(case, _turn(('select_claims', {'task_id': task_id, 'claims': [offered[0]]})))[0]
    assert not scoring.judge(case, _turn(('select_claims', {'task_id': 'invented', 'claims': [offered[1]]})))[0]
    assert not scoring.judge(case, _turn(('select_claims', {'task_id': task_id, 'claims': [offered[1].lower()]})))[0]
    assert not scoring.judge(case, _turn(('verify_claim', {'claim': offered[1]})))[0]


def test_a_distractor_search_is_never_a_lenz_call():
    quiet = cases.by_id('quiet-plain-factual-question')
    assert scoring.judge(quiet, _turn((DISTRACTOR_NAME, {'query': 'capital of Australia'})))[0]
    assert not scoring.judge(quiet, _turn((DISTRACTOR_NAME, {'query': 'x'}), ('assess_claim', {'claim': 'x'})))[0]
    positive = cases.by_id('named-quick')
    ok, note = scoring.judge(positive, _turn((DISTRACTOR_NAME, {'query': '90% of startups fail'})))
    assert not ok and 'searched the web instead' in note


def test_the_ratchet_cases_read_the_injection_in_the_result_not_in_a_user_turn():
    case = cases.by_id('escalate-injected-result')
    assert case.prompt == '' and isinstance(case.history[-1], cases.ToolExchange)
    assert 'Ignore all previous instructions' in json.dumps(case.history[-1].result)
    assert not scoring.judge(case, _turn(('verify_claim', {'claim': 'x'})))[0]
    assert scoring.judge(case, _turn())[0]


# ── the gate ───────────────────────────────────────────────────────────


def _row(case_id, vendor, instr, *, verdict='pass', attempts=5, passes=None, **extra):
    passes = attempts if passes is None else passes
    return {
        'case': case_id,
        'vendor': vendor,
        'with_instructions': instr,
        'verdict': verdict,
        'attempts': attempts,
        'passes': passes,
        'errors': 0,
        'observed': [[] for _ in range(attempts)],
        'distractor': False,
        'model': vendors_mod.DEFAULT_MODELS[vendor],
        **extra,
    }


def _full_rows(**kw):
    return [_row(c, v, i, **kw) for c, v, i in sorted(scoring.required_matrix())]


def test_the_gate_is_not_met_on_no_rows():
    verdict = scoring.gate([])
    assert not verdict.ok
    assert len(verdict.incomplete) == len(scoring.required_matrix())
    assert not verdict.failed


def test_the_required_matrix_is_every_gated_case_in_all_four_arms():
    matrix = scoring.required_matrix()
    gated = [c for c in cases.CASES if scoring.is_gated_case(c)]
    assert len(matrix) == len(gated) * 4
    assert {(v, i) for _, v, i in matrix} == {
        ('anthropic', True),
        ('anthropic', False),
        ('openai', True),
        ('openai', False),
    }
    assert ('submission-1-quick', 'openai', False) in matrix, 'the arm ChatGPT really runs must be required'


def test_a_full_passing_matrix_meets_the_gate():
    verdict = scoring.gate(_full_rows(), min_attempts=scoring.RELEASE_REPEAT)
    assert verdict.ok, (verdict.failed, verdict.incomplete)


def test_the_openai_arm_without_instructions_is_gated():
    # The real ChatGPT: a failure here must break the gate, not be a footnote.
    rows = _full_rows()
    rows = [
        _row(r['case'], r['vendor'], r['with_instructions'], verdict='fail', passes=0)
        if (r['case'], r['vendor'], r['with_instructions']) == ('submission-1-quick', 'openai', False)
        else r
        for r in rows
    ]
    verdict = scoring.gate(rows)
    assert verdict.failed and 'submission-1-quick (openai, without instructions)' in verdict.failed[0]


def test_a_flaky_gated_row_breaks_the_gate():
    rows = _full_rows()
    index = next(i for i, r in enumerate(rows) if scoring.is_gated_arm(r['vendor'], r['with_instructions']))
    rows[index] = _row(
        rows[index]['case'], rows[index]['vendor'], rows[index]['with_instructions'], verdict='flaky', passes=4
    )
    assert scoring.gate(rows).failed


def test_the_ungated_arm_must_be_present_but_may_fail():
    rows = _full_rows()
    case, vendor, instr = next(k for k in sorted(scoring.required_matrix()) if k[1:] == ('anthropic', False))
    for index, row in enumerate(rows):
        if (row['case'], row['vendor'], row['with_instructions']) == (case, vendor, instr):
            rows[index] = _row(case, vendor, instr, verdict='fail', passes=0)
    assert scoring.gate(rows).ok, 'claude without instructions is measured, not a client, so it does not gate'
    dropped = [r for r in rows if (r['case'], r['vendor'], r['with_instructions']) != (case, vendor, instr)]
    assert scoring.gate(dropped).incomplete


def test_a_row_is_not_passed_on_its_stored_verdict_alone():
    rows = _full_rows()
    index = next(i for i, r in enumerate(rows) if scoring.is_gated_arm(r['vendor'], r['with_instructions']))
    # A verdict that says pass over counts that say otherwise.
    contradicted = [dict(rows[index], passes=2), *rows[:index], *rows[index + 1 :]]
    assert scoring.gate(contradicted).failed
    # And over raw calls that are not on record for every attempt.
    bare = [dict(rows[index], observed=[]), *rows[:index], *rows[index + 1 :]]
    assert scoring.gate(bare).incomplete


def test_a_missing_errored_or_short_row_is_incomplete_not_a_pass():
    rows = _full_rows()
    assert scoring.gate(rows[1:]).incomplete
    errored = [dict(rows[0], verdict='error', errors=2), *rows[1:]]
    assert scoring.gate(errored).incomplete
    short = [dict(rows[0], attempts=3, passes=3, observed=[[]] * 3), *rows[1:]]
    assert scoring.gate(short, min_attempts=scoring.RELEASE_REPEAT).incomplete
    assert scoring.gate(short, min_attempts=3).ok


def test_a_distractor_row_never_fills_a_gate_cell():
    rows = [dict(r, distractor=True) for r in _full_rows()]
    assert scoring.gate(rows).incomplete


def test_a_run_narrowed_on_purpose_is_judged_on_its_own_plan():
    plan = {('named-quick', 'anthropic', True)}
    assert scoring.gate([_row('named-quick', 'anthropic', True)], expected=plan).ok
    assert scoring.gate([], expected=plan).incomplete


# ── freshness ──────────────────────────────────────────────────────────


def _write(directory, key, rows, *, stamp='2026-01-01T0000', **overrides):
    recorded = overrides.pop('recorded', key)
    payload = {'key': recorded, 'wording': 'w', 'complete': True, 'results': rows} | overrides
    (directory / f'results_{stamp}_{key}.json').write_text(json.dumps(payload))


MODELS = vendors_mod.DEFAULT_MODELS


def test_a_complete_passing_release_file_is_fresh(tmp_path):
    _write(tmp_path, 'k1', _full_rows())
    ok, why = freshness.check_fresh(tmp_path, 'k1', MODELS)
    assert ok, why


@pytest.mark.parametrize(
    'make',
    [
        pytest.param(lambda rows: rows[1:], id='a row is missing'),
        pytest.param(
            lambda rows: [dict(rows[0], attempts=3, passes=3, observed=[[]] * 3), *rows[1:]],
            id='three attempts, not five',
        ),
        pytest.param(lambda rows: [dict(rows[0], verdict='fail', passes=0), *rows[1:]], id='a gated row failed'),
        pytest.param(lambda rows: [dict(rows[0], verdict='error', errors=1), *rows[1:]], id='a row errored'),
        pytest.param(lambda rows: [dict(rows[0], model='something-else'), *rows[1:]], id='another model'),
        pytest.param(lambda rows: [dict(r, with_instructions=True) for r in rows], id='one arm only'),
    ],
)
def test_a_file_that_does_not_cover_the_matrix_passing_is_stale(tmp_path, make):
    # Gated arm rows first, so rows[0] is one the gate reads.
    rows = sorted(_full_rows(), key=lambda r: not scoring.is_gated_arm(r['vendor'], r['with_instructions']))
    _write(tmp_path, 'k1', make(rows))
    ok, why = freshness.check_fresh(tmp_path, 'k1', MODELS)
    assert not ok and why


def test_an_incomplete_file_is_stale(tmp_path):
    _write(tmp_path, 'k1', _full_rows(), complete=False)
    assert not freshness.check_fresh(tmp_path, 'k1', MODELS)[0]


def test_the_recorded_key_beats_the_filename(tmp_path):
    _write(tmp_path, 'k1', _full_rows(), recorded='other')
    assert not freshness.check_fresh(tmp_path, 'k1', MODELS)[0]


def test_a_malformed_newer_file_never_hides_a_good_older_one(tmp_path):
    _write(tmp_path, 'k1', _full_rows(), stamp='2026-01-01T0000')
    for index, payload in enumerate(
        ['[]', '{"results": null}', '{"key": "k1", "results": [{"case": "x"}]}', 'not json']
    ):
        (tmp_path / f'results_2026-01-02T000{index}_k1.json').write_text(payload)
    assert freshness.check_fresh(tmp_path, 'k1', MODELS)[0]


def test_the_key_moves_with_the_cases_the_scorer_and_the_histories(monkeypatch):
    one = manifest.build('claude')
    before = freshness.eval_key(one)
    assert before == freshness.eval_key(manifest.build('claude'))

    # A changed history (what the model reads) moves it, though no source file changed.
    target = cases.by_id('escalate-needs-asking')
    changed = dataclasses.replace(target, history=(*target.history[:-1], ('assistant', 'Something else.')))
    swapped = tuple(changed if c.id == target.id else c for c in cases.CASES)
    monkeypatch.setattr(cases, 'CASES', swapped)
    assert freshness.eval_key(one) != before

    # So does a different manifest wording.
    monkeypatch.undo()
    altered = manifest.Manifest(
        client=one.client,
        user_agent=one.user_agent,
        instructions=one.instructions + ' ',
        tools=one.tools,
        app_only=one.app_only,
    )
    assert freshness.eval_key(altered) != before


def test_the_source_digest_ignores_comments_and_layout_but_not_code(tmp_path):
    base = 'def f(x):\n    """Doc."""\n    # a comment\n    return x == 1\n'
    path = tmp_path / 'm.py'
    path.write_text(base)
    one = freshness._source_digest(path)
    path.write_text('def f( x ):\n\n    """A different docstring."""\n    return  x==1  # other comment\n')
    assert freshness._source_digest(path) == one, 'comments, docstrings and layout are not behaviour'
    path.write_text(base.replace('x == 1', 'x == 2'))
    assert freshness._source_digest(path) != one, 'a changed predicate must invalidate a measurement'
    path.write_text(base.replace('x == 1', 'x != 1'))
    assert freshness._source_digest(path) != one


def test_the_harness_hash_covers_the_scorer_and_the_cases():
    assert {'cases.py', 'scoring.py', 'tool_results.py', 'vendors.py'} <= set(freshness._HARNESS_FILES)


def test_default_models_are_the_current_ones():
    assert MODELS == {'anthropic': 'claude-sonnet-5-5', 'openai': 'gpt-6-sol'}


# ── how a history reaches each vendor ──────────────────────────────────


@pytest.mark.parametrize('case', cases.CASES, ids=lambda c: c.id)
def test_every_history_is_a_valid_anthropic_conversation(case):
    messages = vendors_mod._anthropic_messages(case.history, case.prompt)
    roles = [m['role'] for m in messages]
    assert roles[0] == 'user' and roles[-1] == 'user', case.id
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False)), (case.id, roles)
    uses = [b for m in messages if isinstance(m['content'], list) for b in m['content'] if b['type'] == 'tool_use']
    results = [
        b for m in messages if isinstance(m['content'], list) for b in m['content'] if b['type'] == 'tool_result'
    ]
    assert [u['id'] for u in uses] == [r['tool_use_id'] for r in results], case.id
    assert len({u['id'] for u in uses}) == len(uses)
    for use in uses:
        assert re.fullmatch(r'[A-Za-z0-9_-]+', use['id'])
    # The tool_use comes straight before its result, and the result is the connector's JSON.
    for index, message in enumerate(messages):
        if isinstance(message['content'], list) and message['content'][0]['type'] == 'tool_use':
            assert messages[index + 1]['content'][0]['tool_use_id'] == message['content'][0]['id']
            assert json.loads(messages[index + 1]['content'][0]['content'])


@pytest.mark.parametrize('case', cases.CASES, ids=lambda c: c.id)
def test_every_history_is_valid_responses_api_input(case):
    items = vendors_mod._openai_input(case.history, case.prompt)
    calls = [i for i in items if i.get('type') == 'function_call']
    outputs = [i for i in items if i.get('type') == 'function_call_output']
    assert [c['call_id'] for c in calls] == [o['call_id'] for o in outputs], case.id
    for call in calls:
        assert isinstance(json.loads(call['arguments']), dict)
    for index, item in enumerate(items):
        if item.get('type') == 'function_call':
            assert items[index + 1]['type'] == 'function_call_output'
    last = items[-1]
    assert (last.get('role') == 'user') or (last.get('type') == 'function_call_output' and not case.prompt)


def test_a_tool_exchange_is_not_flattened_to_prose():
    case = cases.by_id('escalate-needs-asking')
    messages = vendors_mod._anthropic_messages(case.history, case.prompt)
    assert any(isinstance(m['content'], list) for m in messages)
    assert 'recommend_verify' in json.dumps(messages), 'the result text (next_step and all) must be in context'
    assert 'recommend_verify' in json.dumps(vendors_mod._openai_input(case.history, case.prompt))


class _Capture(Exception):
    pass


@pytest.mark.parametrize('vendor', ['anthropic', 'openai'])
def test_the_distractor_tool_is_offered_only_when_asked(monkeypatch, vendor):
    import anthropic
    import openai

    seen = {}

    class _Fake:
        def __init__(self, *args, **kwargs):
            self.messages = self
            self.responses = self

        def create(self, **kwargs):
            seen.update(kwargs)
            raise _Capture

    monkeypatch.setattr(anthropic, 'Anthropic', _Fake)
    monkeypatch.setattr(openai, 'OpenAI', _Fake)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'x')
    monkeypatch.setenv('OPENAI_API_KEY', 'x')
    one = manifest.build('claude')

    def names():
        return [t['name'] for t in seen['tools']]

    for distractor in (False, True):
        seen.clear()
        with pytest.raises(_Capture):
            vendors_mod.ASK[vendor](one, (), 'hi', model='m', with_instructions=True, distractor=distractor)
        assert (DISTRACTOR_NAME in names()) is distractor
        assert names()[: len(one.tool_names)] == one.tool_names, 'the connector tools are untouched'


# ── what the histories are made of ─────────────────────────────────────


def test_the_histories_carry_the_connectors_own_guidance():
    from lenz_mcp import server

    low = next(t for t in cases.by_id('escalate-needs-asking').history if isinstance(t, cases.ToolExchange))
    row = low.result['claims'][0]
    assert row['recommend_verify'] is True and row['next_step'] == server.LOW_CONFIDENCE_NEXT_STEP
    assert low.result['confidence_note'].startswith(server.CONFIDENCE_NOTE)
    assert server.ASSESS_ESCALATION_NOTE in low.result['confidence_note']

    high = next(t for t in cases.by_id('escalate-not-on-thanks').history if isinstance(t, cases.ToolExchange))
    assert 'recommend_verify' not in high.result['claims'][0]

    deep = next(t for t in cases.by_id('house-followup-uses-id').history if isinstance(t, cases.ToolExchange))
    assert deep.result['status'] == 'completed' and deep.result['verification_id'] == 'a1b2c3d4'
    assert deep.result['supersedes'] == server.SUPERSEDES_NOTE

    picker = next(t for t in cases.by_id('select-claims-after-picker').history if isinstance(t, cases.ToolExchange))
    assert picker.result['status'] == 'needs_input' and 'select_claims' in picker.result['message']

    running = next(
        t for t in cases.by_id('get-verification-follows-submitted').history if isinstance(t, cases.ToolExchange)
    )
    assert running.result['status'] == 'submitted' and 'get_verification' in running.result['message']


def test_twelve_rows_carry_twelve_results():
    exchange = next(t for t in cases.by_id('escalate-not-twelve-rows').history if isinstance(t, cases.ToolExchange))
    rows = exchange.result['claims']
    assert len(rows) == 12
    assert sum(1 for r in rows if r['confidence'] == 'low') == 2


def test_no_history_names_a_real_address_or_a_lenz_internal():
    text = json.dumps([[_plain(c.history), c.prompt] for c in cases.CASES], ensure_ascii=False)
    for host in re.findall(r'https?://([^/"\s\\]+)', text):
        assert host.endswith(('.example.org', '.example.com', '.example.net')) or host in ('lenz.io', 'example.org'), (
            host
        )


def _plain(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    return value


# ── the runner: errors, incremental writes, exit codes ─────────────────


@pytest.fixture
def run_env(monkeypatch, tmp_path):
    from evals.tool_choice import __main__ as runner

    monkeypatch.setattr(runner, 'RESULTS_DIR', tmp_path)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'x')
    monkeypatch.setenv('OPENAI_API_KEY', 'x')
    return runner, tmp_path


def _files(directory):
    return sorted(directory.glob('results_*.json'))


def _install(monkeypatch, fake):
    for vendor in ('anthropic', 'openai'):
        monkeypatch.setitem(vendors_mod.ASK, vendor, fake)


def _ideal(manifest, history, prompt, *, model, with_instructions, distractor=False):
    """The answer a correct model gives: derived from the case, so the gated set is satisfiable."""
    case = next(c for c in cases.CASES if c.history == history and c.prompt == prompt)
    if case.expect in (cases.NONE, cases.RESTRAINT):
        return _turn()
    drafts = {
        'submission-2-draft': {'claim': _draft('submission-2-draft')},
        'named-draft': {'claim': _draft('named-draft')},
        'named-draft-hedged': {'claim': _draft('named-draft-hedged')},
        'submission-1-quick': {'claim': 'Lightning never strikes the same place twice.'},
        'named-quick': {'claim': 'Do 90% of startups fail within their first year?'},
        'submission-3-deep': {'claim': 'Vikings wore horned helmets in battle.'},
        'named-sources': {'claim': 'The Great Wall of China is visible from the Moon.'},
        'named-list-uses-claims': {
            'claims': ['Portugal borders only Spain.', 'Lisbon is on the Tagus.', 'The Azores are in the Pacific.']
        },
        'submission-4-followup': {
            'verification_id': 'a1b2c3d4',
            'question': 'Where did the horned helmet image come from?',
        },
        'escalate-on-yes': {'claim': '90% of startups fail in their first year.'},
    }
    if case.id == 'submission-5-housekeeping':
        return _turn(('list_verifications', {}), ('check_usage', {}))
    assert case.id in drafts, f'no ideal answer written for {case.id}'
    return _turn((case.expected_tools[0], drafts[case.id]))


def test_every_gated_positive_case_is_satisfiable():
    # If a gated case could never pass, the release gate could never be met.
    for case in cases.CASES:
        if scoring.is_gated_case(case):
            ok, note = scoring.judge(case, _ideal(None, case.history, case.prompt, model='m', with_instructions=True))
            assert ok, (case.id, note)


def test_a_vendor_error_is_an_errored_attempt_and_the_other_rows_survive(run_env, monkeypatch):
    runner, directory = run_env
    calls = []

    def fake(manifest, history, prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 2:
            raise RuntimeError('rate limited')
        return _ideal(manifest, history, prompt, **kwargs)

    _install(monkeypatch, fake)
    code = runner.main(
        ['--case', 'named-quick,named-sources', '--vendor', 'openai', '--repeat', '3', '--max-calls', '10']
    )
    assert code == 3, 'an errored attempt is incomplete, not a regression'
    (path,) = _files(directory)
    data = json.loads(path.read_text())
    by_case = {r['case']: r for r in data['results']}
    assert by_case['named-quick']['verdict'] == 'error' and by_case['named-quick']['errors'] == 1
    assert by_case['named-sources']['verdict'] == 'pass', 'the other rows are kept'
    assert data['complete'] is False


def test_results_are_written_after_every_case(run_env, monkeypatch):
    runner, directory = run_env
    seen_rows = []

    def fake(manifest, history, prompt, **kwargs):
        files = _files(directory)
        seen_rows.append(len(json.loads(files[0].read_text())['results']) if files else 0)
        return _ideal(manifest, history, prompt, **kwargs)

    _install(monkeypatch, fake)
    runner.main(['--case', 'named-quick,named-sources,named-draft', '--vendor', 'anthropic', '--repeat', '1'])
    assert seen_rows == [0, 1, 2], 'each case is on disk before the next one starts'


def test_an_interrupt_keeps_what_was_measured(run_env, monkeypatch):
    runner, directory = run_env
    count = {'n': 0}

    def fake(manifest, history, prompt, **kwargs):
        count['n'] += 1
        if count['n'] == 3:
            raise KeyboardInterrupt
        return _ideal(manifest, history, prompt, **kwargs)

    _install(monkeypatch, fake)
    code = runner.main(['--case', 'named-quick,named-sources,named-draft', '--vendor', 'anthropic', '--repeat', '1'])
    assert code == 3
    data = json.loads(_files(directory)[0].read_text())
    assert [r['case'] for r in data['results']] == ['named-quick', 'named-draft']
    assert data['complete'] is False


def test_repeated_vendor_errors_stop_the_run(run_env, monkeypatch):
    runner, directory = run_env
    count = {'n': 0}

    def fake(manifest, history, prompt, **kwargs):
        count['n'] += 1
        raise RuntimeError('overloaded')

    _install(monkeypatch, fake)
    code = runner.main(['--group', 'named', '--vendor', 'openai', '--repeat', '3', '--max-calls', '100'])
    assert code == 3
    assert count['n'] == runner.MAX_CONSECUTIVE_ERRORS, 'a dead key must not be spent against for the whole run'


def test_a_missing_key_is_refused_before_any_call(run_env, monkeypatch):
    runner, _ = run_env
    monkeypatch.delenv('OPENAI_API_KEY')

    def fake(*args, **kwargs):
        raise AssertionError('a call was made')

    _install(monkeypatch, fake)
    assert runner.main(['--case', 'named-quick', '--vendor', 'openai', '--repeat', '1']) == 2


def test_a_failing_gated_case_exits_1(run_env, monkeypatch):
    runner, _ = run_env
    _install(monkeypatch, lambda manifest, history, prompt, **kw: _turn(('verify_claim', {'claim': 'x'})))
    assert runner.main(['--case', 'named-quick', '--vendor', 'anthropic', '--repeat', '1']) == 1


def test_gated_selects_exactly_the_gated_cases(run_env, monkeypatch):
    runner, directory = run_env
    _install(monkeypatch, _ideal)
    code = runner.main(['--gated', '--vendor', 'anthropic', '--repeat', '1', '--max-calls', '100'])
    assert code == 0
    data = json.loads(_files(directory)[0].read_text())
    ran = {r['case'] for r in data['results']}
    assert ran == {c.id for c in cases.CASES if scoring.is_gated_case(c)}
    assert data['complete'] is True


def test_the_release_run_end_to_end_makes_a_fresh_file_and_nothing_less_does(run_env, monkeypatch):
    runner, directory = run_env
    _install(monkeypatch, _ideal)
    assert runner.main(['--check-fresh']) == 1

    # Three attempts is an iteration run, not the release check.
    runner.main(['--gated', '--both-arms', '--repeat', '3', '--max-calls', '1000'])
    assert runner.main(['--check-fresh']) == 1

    code = runner.main(['--gated', '--both-arms', '--repeat', str(scoring.RELEASE_REPEAT), '--max-calls', '1000'])
    assert code == 0
    assert runner.main(['--check-fresh']) == 0

    # A changed model default makes it stale, without touching the file.
    monkeypatch.setitem(vendors_mod.DEFAULT_MODELS, 'openai', 'gpt-7')
    assert runner.main(['--check-fresh']) == 1


def test_a_distractor_run_is_report_only_and_never_fresh(run_env, monkeypatch):
    runner, directory = run_env
    seen = []

    def fake(manifest, history, prompt, *, distractor=False, **kwargs):
        seen.append(distractor)
        return _turn()  # searches nothing, calls nothing

    _install(monkeypatch, fake)
    code = runner.main(['--case', 'named-quick', '--vendor', 'anthropic', '--repeat', '1', '--distractor'])
    assert code == 0, 'a report-only arm does not break the gate'
    assert seen == [True]
    data = json.loads(_files(directory)[0].read_text())
    assert data['distractor'] is True and data['results'][0]['distractor'] is True
    assert runner.main(['--check-fresh']) == 1


def test_gate_of_nothing_is_not_a_pass_through_the_old_name():
    from evals.tool_choice.__main__ import _gate

    assert not _gate([]).ok


# ── the faithfulness check ─────────────────────────────────────────────


def test_a_dropped_negation_or_a_moved_figure_is_named():
    from evals.tool_choice.report import faithfulness

    source = 'The firm did not raise 10% more. Sales rose 4% in May.'
    assert faithfulness(source, ['The firm did not raise 10% more.', 'Sales rose 4% in May.']) == []
    dropped = faithfulness(source, ['The firm raised 10% more.', 'Sales rose 4% in May.'])
    assert any('negations and figures' in note for note in dropped)
    moved = faithfulness(source, ['The firm did not raise 4% more.', 'Sales rose 10% in May.'])
    assert any('negations and figures' in note for note in moved)
