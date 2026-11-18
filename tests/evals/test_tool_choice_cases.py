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
        # An empty prompt is a case about the model answering a TOOL RESULT: the
        # history must then end on one, or the model is asked to continue from nothing.
        if not case.prompt.strip():
            assert case.history and isinstance(case.history[-1], cases.ToolExchange), case.id
        assert case.why.strip(), f'{case.id} must say what it protects'
        assert isinstance(case.expect, (str, list)), case.id
        if isinstance(case.expect, list):
            assert case.expect, f'{case.id}: an empty list is `none`, say so'
        for turn in case.history:
            if isinstance(turn, cases.ToolExchange):
                assert turn.name and isinstance(turn.arguments, dict) and isinstance(turn.result, dict), case.id
                continue
            role, text = turn
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
    # second call pass would be vouching for a shape it never checked. Every
    # positive case is now one call per expected tool by default (a second call
    # is a second charge): only a case that NAMES an extra tool may make one.
    promised = {case.id for case in cases.CASES if 'one call' in case.tools_line}
    assert promised, 'no submission case promises one call any more: re-read this test'
    for case_id in promised:
        case = cases.by_id(case_id)
        assert not case.also_allowed, f'{case_id} promises one call and allows another tool'
        assert len(case.expected_tools) == 1, case_id
    for case in cases.CASES:
        # A counted fan-out may size itself against the balance first, as
        # `check_usage`'s description tells the model to before a large batch.
        extras = {'get_verification'} | ({'check_usage', 'list_verifications'} if case.count > 1 else set())
        assert set(case.also_allowed) <= extras, (case.id, case.also_allowed)


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
    from evals.tool_choice.scoring import judge as _judge
    from evals.tool_choice.vendors import Call, Turn

    case = cases.by_id('named-draft')
    whole = {'claim': case.prompt.split(': ', 1)[1].strip('"')}
    one = Turn(calls=[Call('assess_claim', whole)], text='')
    two = Turn(calls=[Call('assess_claim', whole), Call('assess_claim', whole)], text='')
    assert _judge(case, two) == (False, 'called assess_claim 2 times; this case must call it once')
    # And the rule does not over-reach: one call with the whole draft passes.
    assert _judge(case, one) == (True, '')


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


def _case(case_id):
    return next(case for case in cases.CASES if case.id == case_id)


def test_the_out_of_credits_histories_carry_what_their_replies_are_judged_on():
    """A reply can only give the user what the result gave the model: the cost and
    balance, the renewal date, and where there is one the link. It carries no list
    of cheaper checks to run."""
    with_link = cases.DEEP_CHECK_REFUSED[-1].result
    without_link = cases.DEEP_CHECK_REFUSED_OPENAI[-1].result
    for result in (with_link, without_link):
        assert result['status'] == 'quota_exhausted'
        assert result['resets_at']
        assert result['cost'] == 10 and result['credits_remaining'] == 7
        assert 'balance_covers' not in result
        assert 'how' not in str(result)
        assert 'Monthly credits renew on 1 December 2026' in result['message']
        assert "the user's choice" in result['next_step']
    assert cases._PLANS_PAGE in with_link['manage_url']
    assert 'manage_url' not in without_link
    assert cases._PLANS_PAGE not in str(without_link)


GOOD_WALL_REPLY = (
    "Lenz couldn't run that deep check: it costs 10 credits and you have 7. Your credits renew on "
    'December 1, 2026. You can add credits at https://lenz.io/plans?wall=x.'
)
NO_LINK_REPLY = (
    "Lenz couldn't run that deep check: it costs 10 credits and you have 7. Your credits renew on December 1, 2026."
)


@pytest.mark.parametrize(
    ('reply', 'with_link', 'without_link'),
    [
        (GOOD_WALL_REPLY, True, False),
        (NO_LINK_REPLY, False, True),
        # An offer of a cheaper check is allowed, and not required.
        (NO_LINK_REPLY + ' I can run a quick check instead if you want.', False, True),
        (GOOD_WALL_REPLY + ' A lower-depth deep check would also fit; want me to run it?', True, False),
        # The date is required.
        (GOOD_WALL_REPLY.replace('December 1, 2026', 'next month'), False, False),
        # Saying the check did not run is required: the price and the balance alone do not.
        ('A deep check costs 10 credits; you have 7. Credits renew on 1 December 2026.', False, False),
        # Other ways to say it did not run, or why.
        ("I wasn't able to run that. Credits renew on 1 Dec 2026.", False, True),
        ('That check did not run. Credits renew on 2026-12-01.', False, True),
        ('You do not have enough credits for a deep check. Credits renew on 1 December 2026.', False, True),
        ('The deep check didn\u2019t go through. Credits renew on 1 December 2026.', False, True),
        # "below" and "allow" are not a refusal.
        ('You are below the balance this needs; your plan does not allow it until 2026-12-01.', False, False),
    ],
)
def test_the_out_of_credits_replies_are_judged_on_not_run_date_and_link(reply, with_link, without_link):
    assert _case('out-of-credits-gives-the-way-forward').reply(reply) is with_link
    assert _case('out-of-credits-without-a-link').reply(reply) is without_link


@pytest.mark.parametrize(
    'reply',
    [
        "I couldn't run that. Buy more credits. Your credits renew on 1 December 2026.",
        "I couldn't run it; you can upgrade your plan, and credits renew on 1 December 2026.",
        "I couldn't run it; top up at https://example.org/billing. Credits renew on 1 December 2026.",
    ],
)
def test_the_reply_without_a_link_also_carries_no_call_to_buy(reply):
    assert _case('out-of-credits-without-a-link').reply(reply) is False


def test_a_reset_date_in_another_year_is_not_the_reset_date():
    text = "I couldn't run it. Your credits renew on December 1, 2027."
    assert _case('out-of-credits-without-a-link').reply(text) is False
    assert _case('out-of-credits-without-a-link').reply(text.replace('2027', '2026')) is True


@pytest.mark.parametrize('tool', ['verify_claim', 'assess_claim'])
def test_a_check_run_on_its_own_after_the_refusal_fails_whatever_the_reply_says(tool):
    from types import SimpleNamespace

    from evals.tool_choice import scoring

    call = SimpleNamespace(name=tool, arguments={'claim': 'x', 'text': 'x'}, batches=())
    for case_id, reply in (
        ('out-of-credits-gives-the-way-forward', GOOD_WALL_REPLY),
        ('out-of-credits-without-a-link', NO_LINK_REPLY),
    ):
        turn = SimpleNamespace(calls=[call], text=reply, batches=())
        ok, note = scoring.judge(_case(case_id), turn)
        assert not ok and tool in note


def test_a_reply_that_says_it_all_and_calls_nothing_passes():
    from types import SimpleNamespace

    from evals.tool_choice import scoring

    for case_id, reply in (
        ('out-of-credits-gives-the-way-forward', GOOD_WALL_REPLY),
        ('out-of-credits-without-a-link', NO_LINK_REPLY),
    ):
        ok, _ = scoring.judge(_case(case_id), SimpleNamespace(calls=[], text=reply, batches=()))
        assert ok


# ── a quick check that found no claim ────────────────────────────────

NO_CLAIM_CASE = 'no-claim-found-asks-for-one'

GOOD_NO_CLAIM_REPLY = (
    "Lenz didn't check that: it's an opinion, and nothing was charged. Lenz checks factual claims, such as a "
    'fact, a figure, a date, a quote or an attribution. Is there a claim like that you would like me to check?'
)


def test_the_no_claim_history_carries_what_its_reply_is_judged_on():
    """The result the model answers is the connector's own: nothing checked or charged, what
    Lenz checks in the instructions' words, to ask for a claim, and not to send the text again."""
    case = _case(NO_CLAIM_CASE)
    assert case.group == 'tool_results'
    assert case.expects_none and not case.prompt.strip()
    exchange = case.history[-1]
    assert isinstance(exchange, cases.ToolExchange) and exchange.name == 'assess_claim'
    result = exchange.result
    assert result['status'] == 'no_claim'
    step = result['next_step']
    assert step.startswith('Nothing was checked and nothing was charged.')
    assert 'a fact, a figure, a date, a quote or an attribution' in step
    assert 'Ask them for a claim to check' in step
    assert 'Do not send the same text again unchanged' in step
    # The text sent is the opinion the user asked about, and the API's integrator hint stays out.
    assert exchange.arguments == {'claim': cases._OPINION}
    assert '/extract' not in str(result)
    assert case.forbid == ('assess_claim', 'verify_claim')


@pytest.mark.parametrize(
    ('reply', 'passes'),
    [
        (GOOD_NO_CLAIM_REPLY, True),
        # Naming what Lenz checks alone, or asking for a claim alone, with nothing checked said.
        ("I didn't check it: that's an opinion. Lenz checks factual claims, like a date or a figure.", True),
        ('Nothing was checked. Send me a statement you want checked and I will run it.', True),
        ('That was not checked: it is a matter of taste. Which claim should I check instead?', True),
        ('Lenz couldn’t check that. Share a factual claim and I will.', True),
        # Saying only that nothing was checked leaves the user with nowhere to go.
        ('Nothing was checked.', False),
        ('Lenz did not check it because it is an opinion.', False),
        # Describing what Lenz checks without saying this was not checked reads as an answer.
        ('Lenz checks factual claims such as a date or a figure.', False),
        # An opinion answered as if it were a verdict.
        ('Lenz says that is false, and you should check the figure on the label.', False),
        ('', False),
    ],
)
def test_the_no_claim_reply_is_judged_on_nothing_checked_and_what_lenz_checks(reply, passes):
    assert bool(_case(NO_CLAIM_CASE).reply(reply)) is passes


@pytest.mark.parametrize('tool', ['assess_claim', 'verify_claim'])
@pytest.mark.parametrize('claim', [cases._OPINION, 'Many people dislike pineapple on pizza.'])
def test_any_check_after_a_no_claim_fails_whatever_the_reply_says(tool, claim):
    """The same text finds the same nothing, and a reworded claim is one the user did not make."""
    from types import SimpleNamespace

    from evals.tool_choice import scoring

    call = SimpleNamespace(name=tool, arguments={'claim': claim}, batches=())
    turn = SimpleNamespace(calls=[call], text=GOOD_NO_CLAIM_REPLY, batches=())
    ok, note = scoring.judge(_case(NO_CLAIM_CASE), turn)
    assert not ok and tool in note


def test_a_reply_that_says_it_and_calls_nothing_passes_the_no_claim_case():
    from types import SimpleNamespace

    from evals.tool_choice import scoring

    ok, _ = scoring.judge(_case(NO_CLAIM_CASE), SimpleNamespace(calls=[], text=GOOD_NO_CLAIM_REPLY, batches=()))
    assert ok
    ok, note = scoring.judge(_case(NO_CLAIM_CASE), SimpleNamespace(calls=[], text='Nothing was checked.', batches=()))
    assert not ok
