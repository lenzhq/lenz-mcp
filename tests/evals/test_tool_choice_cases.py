"""The tool-choice eval's dataset, checked without calling a model.

This is the half that runs in CI. It cannot tell you whether a model picks the
right tool — that costs money and lives in the eval itself — but it can tell you
the dataset is honest: every case well formed, every tool it names real and
visible to a model, and the eight submission cases present with the wording we
submit.
"""

from __future__ import annotations

import pytest

from evals.tool_choice import cases as cases
from evals.tool_choice import manifest


@pytest.fixture(scope='module')
def visible() -> set[str]:
    return set(manifest.build('claude').tool_names)


def test_every_case_is_well_formed():
    seen = set()
    for case in cases.CASES:
        assert case.id not in seen, f'duplicate case id: {case.id}'
        seen.add(case.id)
        assert case.group in cases.GROUPS, (case.id, case.group)
        assert case.prompt.strip(), case.id
        assert case.why.strip(), f'{case.id} must say what it protects'
        assert isinstance(case.expect, (str, list)), case.id
        if isinstance(case.expect, list):
            assert case.expect, f'{case.id}: an empty list is `none`, say so'
        for role, text in case.history:
            assert role in ('user', 'assistant'), (case.id, role)
            assert text.strip(), case.id


def test_a_restraint_case_names_its_ceilings(visible):
    for case in cases.CASES:
        if case.expect == cases.RESTRAINT:
            assert case.at_most, f'{case.id} is judged by at_most and gives none'
            for name, limit in case.at_most:
                assert name in visible, (case.id, name)
                assert limit >= 1, (case.id, name, limit)
        else:
            assert not case.at_most, f'{case.id} sets at_most without expect=RESTRAINT'


def test_every_tool_a_case_names_exists_and_a_model_can_see_it(visible):
    for case in cases.CASES:
        for name in [*case.expected_tools, *case.allowed_tools]:
            assert name in visible, f'{case.id} expects {name}, which no model-visible manifest has'
        for name in case.forbid:
            assert name in visible, f'{case.id} forbids {name}, which is not a tool a model could call'


def test_no_case_expects_a_card_only_tool():
    # A card tool is hidden from the model on purpose. A case expecting one would
    # be testing something no model is offered — and would pass forever once the
    # card flag turned it on for one client.
    app_only = set(manifest.build('claude').app_only) | {
        'start_verification_widget',
        'get_verification_widget',
        'select_claims_widget',
    }
    for case in cases.CASES:
        for name in case.expected_tools:
            assert name not in app_only, f'{case.id} expects the card-only {name}'


def test_the_eight_submission_cases_are_present():
    submission = cases.by_group('openai_submission')
    assert len(submission) == 8, [c.id for c in submission]
    positives = [c for c in submission if not c.expects_none]
    negatives = [c for c in submission if c.expects_none]
    assert len(positives) == 5, [c.id for c in positives]
    assert len(negatives) == 3, [c.id for c in negatives]
    # These carry the submission's own text, so they need the two extra fields.
    for case in submission:
        assert case.scenario.strip(), case.id
        assert case.expected_output.strip(), case.id


def test_the_submission_text_is_rendered_from_the_cases():
    # What we submit and what we test are one object. If this drifts, the
    # dashboard and the eval disagree and nobody finds out until the submission is checked.
    text = cases.render_submission()
    for case in cases.by_group('openai_submission'):
        assert case.prompt in text, case.id
        assert case.expected_output in text, case.id
    assert text.count('### ') == 8
    # The tool names the reviewer reads are the ones the eval asserts.
    assert '`assess_claim`' in text and '`verify_claim`' in text and '`ask_followup`' in text


def test_every_group_is_exercised():
    for group in cases.GROUPS:
        assert cases.by_group(group), f'{group} has no cases'


def test_the_negative_groups_are_worth_having():
    # The guard against the instructions going broad is only as good as its
    # population: a single "must not fire" case would prove nothing.
    assert len(cases.by_group('must_not_fire')) >= 8
    assert sum(1 for c in cases.CASES if c.expects_none) >= 10


def test_the_unnamed_trigger_phrases_are_covered():
    # The trigger wording is what activation depends on, and it is the thing most
    # likely to be edited by someone who does not know that.
    prompts = ' '.join(c.prompt.lower() for c in cases.by_group('unnamed_triggers'))
    for phrase in ['are you sure', 'is that right', 'double-check', 'fact-check', 'is it true']:
        assert phrase in prompts, phrase


def test_the_same_phrase_about_code_must_not_fire():
    # The pair that makes the triggers meaningful: identical words, different
    # subject, opposite expectation.
    trigger = cases.by_id('trigger-are-you-sure')
    quiet = cases.by_id('quiet-are-you-sure-about-code')
    assert trigger.prompt == quiet.prompt
    assert trigger.expect == 'assess_claim'
    assert quiet.expect == cases.NONE


def test_the_manifest_hash_covers_what_the_model_is_shown():
    one = manifest.build('claude')
    assert len(one.hash()) == 16
    # The same code gives the same hash; a different wording gives a different one.
    assert one.hash() == manifest.build('claude').hash()
    altered = manifest.Manifest(
        client=one.client,
        user_agent=one.user_agent,
        instructions=one.instructions + ' ',
        tools=one.tools,
        app_only=one.app_only,
    )
    assert altered.hash() != one.hash(), 'an edit to the instructions must move the hash'


def test_the_instructions_reach_the_manifest():
    # If this is ever empty the eval would silently measure a model that was
    # shown no instructions at all — which is a REAL condition for some clients
    # (see the README's client table), but it must never happen by accident.
    assert manifest.build('claude').instructions.strip()


def test_every_client_sees_the_same_model_visible_tools():
    # Per-client tailoring changes the card meta and hides card-only tools; it
    # must never change WHICH tools a model can call, or a case's expectation
    # would depend on who is asking.
    baseline = manifest.build('claude').tool_names
    for client in manifest.CLIENTS:
        assert manifest.build(client).tool_names == baseline, client


def test_a_manifest_carries_annotations_and_meta_KEY_NAMES_only():
    # The golden set needs both (a card change is itself a change to `_meta` keys), and
    # a `_meta` VALUE must never reach a committed file: ChatGPT's carries the
    # user's city, region, timezone and coordinates.
    for client in manifest.CLIENTS:
        for tool in manifest.build(client).tools:
            assert isinstance(tool['annotations'], dict), tool['name']
            assert isinstance(tool['meta_keys'], list), tool['name']
            for path in tool['meta_keys']:
                assert isinstance(path, str) and path, tool['name']
                # A path, never a value: dotted key names and nothing else.
                assert all(part for part in path.split('.')), path


def test_the_hash_ignores_what_does_not_steer_a_choice():
    # A golden shows annotations and meta keys; the hash must not, or a result
    # file would be invalidated by a change that cannot have altered a choice.
    one = manifest.build('claude')
    tools = [dict(tool) for tool in one.tools]
    tools[0]['annotations'] = {'readOnlyHint': False, 'title': 'changed'}
    tools[0]['meta_keys'] = ['ui.somethingElse']
    altered = manifest.Manifest(
        client=one.client,
        user_agent=one.user_agent,
        instructions=one.instructions,
        tools=tools,
        app_only=one.app_only,
    )
    assert altered.hash() == one.hash()
    # A description change DOES move it.
    tools[0]['description'] = (tools[0]['description'] or '') + ' extra'
    assert (
        manifest.Manifest(
            client=one.client,
            user_agent=one.user_agent,
            instructions=one.instructions,
            tools=tools,
            app_only=one.app_only,
        ).hash()
        != one.hash()
    )


def test_the_wording_is_the_same_for_every_client():
    # Per-client tailoring changes meta and hides card tools; it must never
    # change the WORDING, or a case measured on one client would not describe
    # another. The hash is the assertion.
    baseline = manifest.build('claude').hash()
    for client in manifest.CLIENTS:
        assert manifest.build(client).hash() == baseline, client


# ── the runner refuses what would spend wrongly ────────────────────────────
# None of these make a model call: each is refused before the first one, and
# the vendor functions are replaced with ones that fail the test if reached.


@pytest.fixture
def no_spend(monkeypatch):
    from evals.tool_choice import vendors

    def _forbidden(*args, **kwargs):
        raise AssertionError('a paid call was attempted')

    monkeypatch.setitem(vendors.ASK, 'anthropic', _forbidden)
    monkeypatch.setitem(vendors.ASK, 'openai', _forbidden)


def _main(argv):
    from evals.tool_choice.__main__ import main

    return main(argv)


def test_repeat_below_one_is_refused(no_spend):
    # 0 attempts scored `passes == repeat` as 0 == 0: an all-pass file, no calls.
    assert _main(['--repeat', '0']) == 2
    assert _main(['--repeat', '-1']) == 2


@pytest.mark.parametrize('argv', [['--group', ','], ['--group', ' '], ['--case', ''], ['--case', ' , ']])
def test_an_explicitly_empty_selector_is_refused_not_widened(no_spend, argv):
    # Given-but-empty must not read as "no filter", which is every case in the suite.
    assert _main(argv + ['--max-calls', '100000']) == 2


def test_an_empty_manifest_is_refused_before_any_call(no_spend, monkeypatch):
    from evals.tool_choice import manifest as manifest_mod

    empty = manifest_mod.Manifest(client='claude', user_agent='x', instructions='', tools=[], app_only=[])
    monkeypatch.setattr(manifest_mod, 'build', lambda *a, **k: empty)
    assert _main(['--case', 'named-quick', '--repeat', '1']) == 2


def test_one_call_is_enforced_wherever_it_is_promised():
    # The submission tells a reviewer "one call" for these; an eval that let a
    # second call pass would be vouching for a shape it never checked. The
    # drafts are one call by definition.
    promised = {case.id for case in cases.CASES if 'one call' in case.tools_line}
    drafts = {'submission-2-draft', 'named-draft', 'named-draft-hedged'}
    enforced = {case.id for case in cases.CASES if case.single_call}
    assert promised, 'no submission case promises one call any more: re-read this test'
    assert promised | drafts <= enforced


def test_the_drafts_are_judged_on_their_whole_text():
    # Two fragments must not be enough ("Eiffel 1950" alone would pass), and the
    # hedged case must check the hedges it exists to protect.
    for case_id in ('submission-2-draft', 'named-draft', 'named-draft-hedged'):
        case = cases.by_id(case_id)
        draft = case.prompt.split('"', 1)[1].rsplit('"', 1)[0]
        check = case.expect_args['claim']
        assert check(draft), case_id
        assert check('  ' + draft.upper() + '  '), 'case and whitespace are normalised, nothing else'
        first_sentence = draft.split('. ')[0]
        assert not check(first_sentence), f'{case_id}: one sentence of the draft must not pass'
    hedged = cases.by_id('named-draft-hedged').expect_args['claim']
    unhedged = 'Roughly 40% of new EV models launched in 2025 missed their delivery targets.'
    assert not hedged(unhedged), 'dropping "Analysts say" must fail'


def test_a_second_call_on_a_draft_case_fails_the_attempt():
    from evals.tool_choice.__main__ import _judge
    from evals.tool_choice.vendors import Call, Turn

    case = cases.by_id('named-draft')
    whole = {'claim': case.prompt.split(': ', 1)[1].strip('"')}
    one = Turn(calls=[Call('assess_claim', whole)], text='')
    two = Turn(calls=[Call('assess_claim', whole), Call('assess_claim', whole)], text='')
    assert _judge(case, two) == (False, "made 2 calls (['assess_claim', 'assess_claim']); this case must be one call")
    # And the rule does not over-reach: one call with the whole draft passes.
    assert _judge(case, one) == (True, '')


def _write_result(directory, wording, rows, stamp='2026-01-01T0000', *, recorded=None):
    import json

    payload = {'wording': wording if recorded is None else recorded, 'results': rows}
    (directory / f'results_{stamp}_{wording}.json').write_text(json.dumps(payload))


def _row(case_id, vendor, *, with_instructions=True, attempts=1):
    return {'case': case_id, 'vendor': vendor, 'with_instructions': with_instructions, 'attempts': attempts}


def test_check_fresh_needs_the_submission_set_not_just_a_filename(no_spend, monkeypatch, tmp_path):
    from evals.tool_choice import __main__ as runner

    wording = manifest.build('claude').hash()
    monkeypatch.setattr(runner, 'RESULTS_DIR', tmp_path)
    submission = [case.id for case in cases.by_group('openai_submission')]

    # One case, without instructions: the shape of the n=20 cell files.
    _write_result(tmp_path, wording, [_row('named-quick', 'openai', with_instructions=False)])
    assert _main(['--check-fresh']) == 1

    # The whole submission set, but one vendor only.
    _write_result(tmp_path, wording, [_row(cid, 'openai') for cid in submission], stamp='2026-01-01T0001')
    assert _main(['--check-fresh']) == 1

    # The whole submission set, both vendors, with instructions: fresh.
    rows = [_row(cid, vendor) for cid in submission for vendor in ('anthropic', 'openai')]
    _write_result(tmp_path, wording, rows, stamp='2026-01-01T0002')
    assert _main(['--check-fresh']) == 0


def test_the_vendor_clients_never_retry(monkeypatch):
    # The SDKs retry twice by default: one planned call, up to three billed
    # requests, none counted against --max-calls.
    import anthropic
    import openai

    from evals.tool_choice import vendors

    seen = {}

    class _Stop(Exception):
        pass

    def _capture(name):
        def _make(*args, **kwargs):
            seen[name] = kwargs.get('max_retries')
            raise _Stop

        return _make

    monkeypatch.setattr(anthropic, 'Anthropic', _capture('anthropic'))
    monkeypatch.setattr(openai, 'OpenAI', _capture('openai'))
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'x')
    monkeypatch.setenv('OPENAI_API_KEY', 'x')
    one = manifest.build('claude')
    for ask in vendors.ASK.values():
        with pytest.raises(_Stop):
            ask(one, (), 'hi', model='m', with_instructions=True)
    assert seen == {'anthropic': 0, 'openai': 0}


# Tools a submission line may name WITHOUT the eval asserting them, each with
# why. The eval is single-turn and executes nothing, so a call that only
# happens on a LATER turn is out of its reach; naming one here is a deliberate,
# visible gap rather than an unenforced promise.
_LATER_TURN_ONLY = {
    'get_verification': 'follows verify_claim on the next turn when the check is still running',
}


def test_a_submission_tools_line_names_every_tool_the_case_asserts(visible):
    import re

    for case in cases.by_group('openai_submission'):
        if not case.tools_line:
            continue
        # Both directions: every tool the case asserts is named...
        for name in case.expected_tools:
            assert f'`{name}`' in case.tools_line, (case.id, name)
        # ...and every TOOL the line names is asserted, or is a declared
        # later-turn call. (`claim` and other argument names are not tools.)
        named_tools = set(re.findall(r'`(\w+)`', case.tools_line)) & visible
        unasserted = named_tools - set(case.expected_tools) - set(_LATER_TURN_ONLY)
        assert not unasserted, f'{case.id} promises {unasserted}, which the eval does not assert'


def test_check_fresh_trusts_the_recorded_wording_not_the_filename(no_spend, monkeypatch, tmp_path):
    from evals.tool_choice import __main__ as runner

    wording = manifest.build('claude').hash()
    monkeypatch.setattr(runner, 'RESULTS_DIR', tmp_path)
    rows = [_row(case.id, vendor) for case in cases.by_group('openai_submission') for vendor in ('anthropic', 'openai')]
    # Named with the current hash, but it RECORDS another wording: not fresh.
    _write_result(tmp_path, wording, rows, recorded='0000000000000000')
    assert _main(['--check-fresh']) == 1


@pytest.mark.parametrize('payload', ['[]', '{"results": null}', '{"results": [{"case": "x"}]}', 'not json'])
def test_a_malformed_newer_file_never_hides_a_good_older_one(no_spend, monkeypatch, tmp_path, payload):
    from evals.tool_choice import __main__ as runner

    wording = manifest.build('claude').hash()
    monkeypatch.setattr(runner, 'RESULTS_DIR', tmp_path)
    rows = [_row(case.id, vendor) for case in cases.by_group('openai_submission') for vendor in ('anthropic', 'openai')]
    _write_result(tmp_path, wording, rows, stamp='2026-01-01T0000')
    (tmp_path / f'results_2026-01-02T0000_{wording}.json').write_text(payload)
    assert _main(['--check-fresh']) == 0
