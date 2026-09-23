"""The connector as an assistant user meets it.

The quick check (`assess_claim`) is the default and a first read; a
low-confidence row carries the recommendation for a deep check in the result
itself; a completed deep check carries what the user should see and that it
replaces the quick verdict; a run that outlives the wait says so and names the
way back; `list_verifications` finds a result nobody collected.

Every API fixture here is validated against the public API's own schema (see
`test_fixtures_match_the_public_api_schemas`): the MCP reads nothing but the
public API, so a hand-built dict that drifted from it would prove only that the
test agrees with itself.
"""

import asyncio
import re
import types

import httpx
import pytest

from lenz_mcp import client, config, server
from lenz_mcp.client import ApiResponse


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(server, '_sleep', _instant)
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS', 0.05)
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS_BY_IDENTITY', {'Claude-User': 0.05})


@pytest.fixture(autouse=True)
def _reset_shared_http_client():
    client._http_client = None
    yield
    client._http_client = None


def _as_client(monkeypatch, user_agent, **kwargs):
    """Put a request from `user_agent` in scope, the way the middleware does.

    ONE seam: every per-client decision reads `client.client_profile()`, so a
    patch here moves the wait, the card and its delivery hint together. Patching
    the User-Agent reader alone used to move some and not others.
    """
    monkeypatch.setattr(client, 'client_profile', lambda: client.ClientProfile.from_user_agent(user_agent, **kwargs))


def _ctx(auth='Bearer lenz_testkey'):
    headers = {'authorization': auth} if auth else {}
    request = types.SimpleNamespace(headers=headers)
    return types.SimpleNamespace(request_context=types.SimpleNamespace(request=request))


def _run(coro):
    return asyncio.run(coro)


def _patch_api(monkeypatch, name, response):
    calls = []

    async def _fake(*args, **kwargs):
        calls.append((args, kwargs))
        return response

    monkeypatch.setattr(client, name, _fake)
    return calls


def _tools():
    return {t.name: t for t in _run(server.mcp.list_tools())}


# ── fixtures, shaped by the public API schemas ───────────────────────

_TASK_ID = 'a' * 32

# POST /assess, list form (AssessOut): one row per item, same order.
_ASSESS = {
    'claims': [
        {
            'claim': 'GDPR requires consent for any processing of personal data.',
            'language': 'en',
            'verdict': 'False',
            'confidence': 'low',
            'verification_url': None,
            'error_code': None,
            'candidate_claims': [],
            'identified_claims': [],
            'hint': None,
        },
        {
            'claim': 'The EU AI Act entered into force in August 2024.',
            'language': 'en',
            'verdict': 'True',
            'confidence': 'medium',
            'verification_url': None,
            'error_code': None,
            'candidate_claims': [],
            'identified_claims': [],
            'hint': None,
        },
        {
            'claim': 'Water boils at 100 °C at sea level.',
            'language': 'en',
            'verdict': 'True',
            'confidence': 'high',
            'verification_url': None,
            'error_code': None,
            'candidate_claims': [],
            'identified_claims': [],
            'hint': None,
        },
        {
            'claim': 'asdf qwer',
            'language': 'en',
            'verdict': 'Error',
            'confidence': 'low',
            'verification_url': None,
            'error_code': 'no_claim',
            'candidate_claims': [],
            'identified_claims': [],
            'hint': 'This is a string of letters, not a statement. Send a factual claim.',
        },
    ],
    'error': None,
    'error_code': None,
    'candidate_claims': [],
}

# GET /verify/status/{task_id} `result` and GET /verifications/{id} (ClaimDetailOut).
_RESULT = {
    'verification_id': 'a1b2c3d4',
    'claim': 'GDPR requires consent for any processing of personal data.',
    'visibility': 'private',
    'depth': 'standard',
    'domain': 'law',
    'entities': [{'name': 'GDPR', 'qid': 'Q1172506'}],
    'presumed_intent': '',
    'verdict': 'False',
    'confidence': 'high',
    'lenz_score': 2,
    'key_finding': 'Consent is one of six lawful bases for processing under Article 6 of the GDPR.',
    'executive_summary': 'Article 6 lists six lawful bases; consent is only one of them.',
    'warnings': ['National implementations add conditions for some categories of data.'],
    'created_at': '2026-09-17T09:12:44+00:00',
    'modified_at': None,
    'sources': [
        {
            'source_name': 'EUR-Lex',
            'title': 'Regulation (EU) 2016/679, Article 6',
            'url': 'https://eur-lex.europa.eu/eli/reg/2016/679/oj',
            'snippet': 'Processing shall be lawful only if and to the extent that at least one of the following applies.',
            'date': '2016-04-27',
        },
        {
            'source_name': 'ICO',
            'title': 'A guide to lawful basis',
            'url': 'https://ico.org.uk/for-organisations/lawful-basis/',
            'snippet': '',
            'date': '',
        },
        {'source_name': 'Unlinked', 'title': 'No link', 'url': '', 'snippet': 'dropped', 'date': ''},
    ],
    'audit': {
        'adjudication_summary': '',
        'assessments': [],
        'debate_pro': {'role': '', 'argument': '', 'rebuttal': ''},
        'debate_con': {'role': '', 'argument': '', 'rebuttal': ''},
        'panel_agreement': 'unanimous',
    },
    'language': 'en',
}
_STATUS_COMPLETED = {'status': 'completed', 'task_id': _TASK_ID, 'result': _RESULT}
_PROGRESS = {'step': 'research', 'index': 2, 'total': 5, 'elapsed_seconds': 41, 'poll_after_seconds': 3}
_STATUS_PROCESSING = {'status': 'processing', 'task_id': _TASK_ID, 'progress': _PROGRESS}

# GET /verifications (VerificationListOut).
_LISTING = {
    'items': [
        {
            'verification_id': 'a1b2c3d4',
            'claim': 'GDPR requires consent for any processing of personal data.',
            'domain': 'law',
            'entities': [{'name': 'GDPR', 'qid': 'Q1172506'}],
            'verdict': 'False',
            'confidence': 'high',
            'lenz_score': 2,
            'key_finding': 'Consent is one of six lawful bases for processing under Article 6 of the GDPR.',
            'executive_summary': 'long text the listing does not repeat',
            'created_at': '2026-09-17T09:12:44+00:00',
            'modified_at': None,
            'language': 'en',
        }
    ],
    'total': 14,
    'page': 1,
    'page_size': 10,
}


# ── what the model is told before it decides ─────────────────────────


def test_instructions_lead_with_the_job_and_make_the_quick_check_the_default():
    text = server.mcp.instructions
    lower = text.lower()
    first_sentence = lower.split('. ')[0]
    # The job leads: auditing a draft, a pasted text or the assistant's own answer.
    assert 'draft' in first_sentence
    assert 'previous answer' in first_sentence
    # Triggers, and still never "always use/call Lenz" (over-triggering costs
    # money per call). "Always show the confidence" is about presenting a result.
    assert 'names lenz' in lower
    assert 'whether something is true' in lower
    assert 'fact-check' in lower
    assert 'not for opinions' in lower
    for overreach in ('always use', 'always call', 'always run', 'always check'):
        assert overreach not in lower, overreach
    assert lower.rstrip().endswith('always show the confidence.')
    # The quick check comes first and is a first read, never final.
    assert text.index('`assess_claim`') < text.index('`verify_claim`')
    assert 'quick check' in lower
    assert 'first read' in lower


TRIGGER_SENTENCE = (
    'Use it when the user names Lenz; asks you to check, double-check, verify, confirm or fact-check '
    'something; asks you to audit a draft or your previous answer for factual errors; asks whether '
    'something is true or accurate; or doubts a factual statement you made (“are you sure?”, '
    '“is that right?”). Only when the statement is a factual claim: a fact, a figure, a '
    'date, a quote or an attribution. Not for opinions, predictions, arithmetic, how code behaves '
    'or text with no checkable claim.'
)

# Length budgets. Routing text that grows buries the routing rule, so a later
# edit pays for what it adds.
#
# The host caps how much instruction text it takes, and the instructions sit at
# their budget, so an addition has to replace words rather than append them.
# There is no soft failure: a client that truncates would cut the end of the
# tool guidance and nothing local would show it.
INSTRUCTIONS_MAX_CHARS = 2214
# Raised from 1,237 for the draft sentence: the tool-choice eval
# measured both vendors splitting a pasted draft into `claims` and rewriting it
# on the way — resolved pronouns, invented figures — so the tool's own text has
# to say a pasted text goes in `claim` whole. 170 chars is cheaper than a
# verdict on a claim the user never made.
ASSESS_DESCRIPTION_MAX_CHARS = 1407


def _assess_description() -> str:
    return ' '.join(_tools()['assess_claim'].description.split())


def test_instructions_catch_checks_that_do_not_name_lenz():
    """Double-check, confirm, "is this accurate?" and a user doubting the
    assistant's own factual statement trigger a check too, bounded
    to factual claims, with the non-claims named. The distinctive job leads
    and the generic doubt phrases come last."""
    text = server.mcp.instructions
    assert TRIGGER_SENTENCE in text
    # The job, then the triggers: the first two sentences, mechanics after.
    job_end = text.index('independent sources. ') + len('independent sources. ')
    assert text.index(TRIGGER_SENTENCE) == job_end
    lower = TRIGGER_SENTENCE.lower()
    assert lower.index('fact-check') < lower.index('audit a draft') < lower.index('true or accurate')
    assert lower.index('true or accurate') < lower.index('are you sure?')
    for negative in ('opinions', 'predictions', 'code', 'arithmetic'):
        assert negative in lower.split('not for', 1)[1], negative


def test_instructions_do_not_grow():
    assert len(server.mcp.instructions) <= INSTRUCTIONS_MAX_CHARS


def test_assess_claim_description_carries_the_trigger_phrases():
    """Claude loads a connector's tools on demand by matching their
    descriptions, so the phrases a user types have to be in the tool's own
    text, and in its first two sentences."""
    description = _assess_description()
    phrases = (
        'Use it when the user says things like “fact-check this”, “double-check that”, '
        '“is this accurate?”, “is that true?” or “are you sure?” about a '
        'factual statement.'
    )
    doubted = 'When the user doubts something you said, pass the specific statement being doubted, not the whole conversation.'
    first_sentence_end = description.index('one credit per claim).') + len('one credit per claim).')
    assert description[first_sentence_end:].lstrip().startswith(phrases)
    assert doubted in description
    assert len(description) <= ASSESS_DESCRIPTION_MAX_CHARS


def test_instructions_carry_the_confidence_ladder_and_the_consent_rule():
    lower = server.mcp.instructions.lower()
    for phrase in ('on low', 'recommend', 'on medium', 'dissent', 'offer', 'on high'):
        assert phrase in lower, phrase
    # A high-stakes claim earns a recommendation, never a deep check started unasked.
    assert 'high-stakes for the user' in lower
    assert lower.index('high-stakes') < lower.index('recommend a deep check')
    # Bounded: never a fan-out, never a deep check without a yes, unless asked for.
    assert "the user's yes" in lower
    assert 'every row' in lower
    assert 'asked for sources' in lower


def test_instructions_show_the_reviewers_reasoning_without_calling_it_evidence():
    """A quick-check row now carries `rationale` / `dissent`. The
    instructions say how to present them and nothing about how they are chosen."""
    text = server.mcp.instructions
    sentence = (
        "When a row carries a `rationale`, show it with the verdict as the reviewers' reasoning, never as "
        'sourced evidence; when it carries a `dissent`, say that a reviewer disagreed and why.'
    )
    assert sentence in text
    assert text.index('Present a quick verdict as a first read, not as final.') < text.index(sentence)
    assert 'when a row carries a dissent, offer one' in text
    # The instructions say nothing else about the notes: every sentence that
    # names one is an approved sentence, so no clause on how a note is chosen
    # can be added without failing here.
    flat = ' '.join(text.split())
    note_sentences = [s for s in re.split(r'(?<=[.!?])\s+', flat) if 'rationale' in s or 'dissent' in s]
    assert note_sentences == [
        sentence,
        'Then act on its confidence: on low, or when the claim is high-stakes for the user (legal, medical, '
        'financial, or about to be published), recommend a deep check; on medium, or when a row carries a '
        'dissent, offer one; on high, mention that one is available without pushing it.',
    ]


def test_instructions_carry_the_supersede_rule_and_the_ways_back():
    text = server.mcp.instructions
    lower = text.lower()
    assert 'replaces any earlier quick verdict' in lower
    assert 'apologis' in lower
    assert 'averag' in lower
    assert 'source verification is unavailable' in lower
    assert 'is running' in lower
    assert '`list_verifications`' in text
    assert 'never a prerequisite' in lower
    # A deep check is private: there is no link to surface.
    assert 'link back' not in lower


def test_no_model_facing_text_says_a_deep_check_takes_fifteen_seconds():
    """A deep check usually runs past a minute, at either depth, so no
    model-facing text may promise a check in seconds."""
    texts = [server.mcp.instructions]
    for tool in _tools().values():
        texts.append(tool.description or '')
        texts.extend((p.get('description') or '') for p in (tool.input_schema or {}).get('properties', {}).values())
    for text in texts:
        for wrong in ('~15s', '15 seconds', '~10s', '~90s'):
            assert wrong not in text, (wrong, text[:80])


def test_the_quick_check_result_names_its_panel(monkeypatch):
    _patch_api(monkeypatch, 'assess', ApiResponse(status=200, data={**_ASSESS, 'claims': _ASSESS['claims'][:1]}))
    out = _run(server.assess_claim(_ASSESS['claims'][0]['claim'], _ctx()))
    # The quick check's source label names its panel size; the deep check's
    # copy never states a model count.
    assert out['source'] == 'Lenz fast fact-check (3-model panel)'


def test_tool_descriptions_follow_the_quick_check_first_order():
    tools = _tools()
    assess = ' '.join(tools['assess_claim'].description.lower().split())
    verify = ' '.join(tools['verify_claim'].description.lower().split())
    assert 'quick check' in assess
    assert 'first read' in assess
    assert 'recommend_verify' in assess
    assert 'deep check' in verify
    # The REQUIREMENT, not one phrasing of it: a deep check happens because the
    # user asked or agreed, and never on the model's initiative. Pinning the
    # exact words ("the user's yes") would fail a rewording that keeps the
    # rule or makes it stronger.
    assert 'not the default' in verify
    assert 'never start one unasked' in verify
    assert 'asked for sources' in verify and 'agreed' in verify
    assert 'supersedes' in verify
    assert 'reserve for high-stakes' not in verify
    depth = tools['verify_claim'].input_schema['properties']['depth']['description'].lower()
    assert 'faster' not in depth


# ── the quick check's rows ───────────────────────────────────────────


def test_a_low_confidence_row_carries_the_recommendation(monkeypatch):
    _patch_api(monkeypatch, 'assess', ApiResponse(status=200, data=_ASSESS))
    out = _run(server.assess_claim(ctx=_ctx(), claims=[c['claim'] for c in _ASSESS['claims']]))
    low, medium, high, error = out['claims']
    assert low['recommend_verify'] is True
    assert low['next_step'] == server.LOW_CONFIDENCE_NEXT_STEP
    for row in (medium, high, error):
        assert 'recommend_verify' not in row
        assert 'next_step' not in row
    # An Error row still reads low confidence and is still no reason to check deeper.
    assert error['error'] == 'no_claim'


def test_the_recommendation_never_assumes_reviewer_notes_exist(monkeypatch):
    """The next step is derived from confidence alone: it may not refer to a
    reviewer's reasoning, which is absent on many rows and on every API that
    predates it — and it reads the same with or without those keys."""
    sentence = server.LOW_CONFIDENCE_NEXT_STEP.lower()
    for word in ('reason', 'rationale', 'dissent', 'reviewer', 'note'):
        assert word not in sentence, word

    row = {**_ASSESS['claims'][0], 'rationale': None, 'dissent': None}
    _patch_api(monkeypatch, 'assess', ApiResponse(status=200, data={**_ASSESS, 'claims': [row]}))
    out = _run(server.assess_claim(_ASSESS['claims'][0]['claim'], _ctx()))
    assert out['claims'][0]['recommend_verify'] is True


def test_the_escalation_note_frames_the_quick_verdict_as_a_first_read(monkeypatch):
    _patch_api(monkeypatch, 'assess', ApiResponse(status=200, data={**_ASSESS, 'claims': _ASSESS['claims'][:1]}))
    out = _run(server.assess_claim(_ASSESS['claims'][0]['claim'], _ctx()))
    note = out['confidence_note']
    assert note.startswith(server.CONFIDENCE_NOTE)
    assert server.ASSESS_ESCALATION_NOTE.strip() in note
    assert 'first read' in note
    assert '`verify_claim`' in note
    assert "the user's yes" in note
    assert 'a first read that shows no sources' in note


# ── the completed deep check ─────────────────────────────────────────


def test_completed_result_carries_warnings_and_what_each_source_says(monkeypatch):
    _patch_api(monkeypatch, 'verify_status', ApiResponse(status=200, data=_STATUS_COMPLETED))
    out = _run(server.get_verification(_TASK_ID, _ctx()))
    assert out['status'] == 'completed'
    assert out['warnings'] == _RESULT['warnings']
    assert out['sources_total'] == 2
    first, second = out['sources']
    assert first == {
        'title': 'Regulation (EU) 2016/679, Article 6',
        'url': 'https://eur-lex.europa.eu/eli/reg/2016/679/oj',
        'publisher': 'EUR-Lex',
        'date': '2016-04-27',
        'quote': 'Processing shall be lawful only if and to the extent that at least one of the following applies.',
    }
    # Empty values are left out, not sent as ''.
    assert second == {
        'title': 'A guide to lawful basis',
        'url': 'https://ico.org.uk/for-organisations/lawful-basis/',
        'publisher': 'ICO',
    }
    assert out['presentation'] == server.PRESENTATION_NOTE
    assert out['supersedes'] == server.SUPERSEDES_NOTE


def _stored_detail(monkeypatch):
    _patch_api(monkeypatch, 'verification_detail', ApiResponse(status=200, data=_RESULT))
    return _run(server.get_verification('a1b2c3d4', _ctx()))


def _awaited_by_task(monkeypatch):
    _patch_api(monkeypatch, 'verify_status', ApiResponse(status=200, data=_STATUS_COMPLETED))
    return _run(server.get_verification(_TASK_ID, _ctx()))


def _verify_in_time(monkeypatch):
    _patch_api(monkeypatch, 'verify', ApiResponse(status=202, data={'task_id': _TASK_ID, 'status': 'queued'}))
    _patch_api(monkeypatch, 'verify_status', ApiResponse(status=200, data=_STATUS_COMPLETED))
    return _run(server.verify_claim(_RESULT['claim'], _ctx()))


def _one_selection(monkeypatch):
    _patch_api(
        monkeypatch,
        'select',
        ApiResponse(
            status=200, data={'batch_id': 'b1', 'items': [{'task_id': _TASK_ID, 'claim_text': _RESULT['claim']}]}
        ),
    )
    _patch_api(monkeypatch, 'verify_status', ApiResponse(status=200, data=_STATUS_COMPLETED))
    return _run(server.select_claims('p' * 32, [_RESULT['claim']], _ctx()))


def _widget_poll(monkeypatch):
    _patch_api(monkeypatch, 'verify_status', ApiResponse(status=200, data=_STATUS_COMPLETED))
    return _run(server.get_verification_widget(_TASK_ID, _ctx()))


@pytest.mark.parametrize('path', [_stored_detail, _awaited_by_task, _verify_in_time, _one_selection, _widget_poll])
def test_every_completed_deep_check_says_it_supersedes_the_quick_verdict(monkeypatch, path):
    """The server is stateless and cannot know what the quick check said, so
    the rule rides on every completed result, whichever tool delivered it."""
    out = path(monkeypatch)
    assert out['status'] == 'completed'
    assert out['supersedes'] == server.SUPERSEDES_NOTE
    assert out['presentation'] == server.PRESENTATION_NOTE
    lower = out['supersedes'].lower()
    assert 'replaces any earlier quick verdict' in lower
    assert 'changed' in lower


@pytest.mark.parametrize('warnings', [None, '', 'one string', {'a': 1}, [None, 3, '  ', 'kept']])
def test_warnings_are_always_a_list_of_text(warnings):
    out = server._completed_result({**_RESULT, 'warnings': warnings})
    assert isinstance(out['warnings'], list)
    assert all(isinstance(w, str) and w.strip() for w in out['warnings'])
    if isinstance(warnings, list):
        assert out['warnings'] == ['kept']


def test_warnings_missing_from_an_older_payload():
    result = {k: v for k, v in _RESULT.items() if k != 'warnings'}
    assert server._completed_result(result)['warnings'] == []


def test_no_sources():
    out = server._completed_result({**_RESULT, 'sources': []})
    assert out['sources'] == []
    assert out['sources_total'] == 0


def test_url_less_and_malformed_sources_are_dropped():
    out = server._completed_result({**_RESULT, 'sources': [None, 'x', {'title': 'T', 'url': ''}, {'title': 'T'}]})
    assert out['sources'] == []
    assert out['sources_total'] == 0


def test_a_source_with_no_quote_has_no_quote_key():
    source = {**_RESULT['sources'][0], 'snippet': ''}
    out = server._completed_result({**_RESULT, 'sources': [source]})
    assert 'quote' not in out['sources'][0]


def test_a_passage_at_the_limit_passes_uncut():
    """A long snippet is a passage, not a short quote.
    It is never cut (a cut can drop a negation): up to the limit it passes
    whole."""
    assert server.SOURCE_QUOTE_MAX_CHARS == 600
    snippet = ('The committee did not approve the measure in its final session. ' * 10)[:600]
    out = server._completed_result({**_RESULT, 'sources': [{**_RESULT['sources'][0], 'snippet': snippet}]})
    assert out['sources'][0]['quote'] == snippet.strip()


def test_a_passage_over_the_limit_shows_no_quote():
    source = {
        **_RESULT['sources'][0],
        'snippet': 'A' * 300 + '. U.S. Government No. 5 did not approve it. ' + 'B' * 300,
    }
    row = server._completed_result({**_RESULT, 'sources': [source]})['sources'][0]
    assert 'quote' not in row
    assert row['url'] == source['url']
    assert row['publisher'] == 'EUR-Lex'


def test_a_cjk_quote_under_the_limit_passes_uncut():
    snippet = '委员会在最后一次会议上没有批准该措施。' * 10
    assert len(snippet) <= server.SOURCE_QUOTE_MAX_CHARS
    out = server._completed_result({**_RESULT, 'sources': [{**_RESULT['sources'][0], 'snippet': snippet}]})
    assert out['sources'][0]['quote'] == snippet


# ── a run that outlives the wait ─────────────────────────────────────


def test_a_deep_check_still_running_says_so_and_names_the_way_back(monkeypatch):
    _patch_api(monkeypatch, 'verify', ApiResponse(status=202, data={'task_id': _TASK_ID, 'status': 'queued'}))
    _patch_api(monkeypatch, 'verify_status', ApiResponse(status=200, data=_STATUS_PROCESSING))
    out = _run(server.verify_claim(_RESULT['claim'], _ctx()))
    assert out['status'] == 'submitted'
    assert out['task_id'] == _TASK_ID
    assert out['depth'] == 'standard'
    assert out['elapsed_seconds'] == 41
    assert (out['index'], out['total']) == (2, 5)
    message = out['message']
    assert message.startswith('The check has started and is still running.')
    assert 'Tell the user it is running' in message
    assert '`get_verification`' in message
    assert '`list_verifications`' in message


def test_a_low_depth_check_still_running_carries_its_depth(monkeypatch):
    _patch_api(monkeypatch, 'verify', ApiResponse(status=202, data={'task_id': _TASK_ID, 'status': 'queued'}))
    _patch_api(monkeypatch, 'verify_status', ApiResponse(status=200, data=_STATUS_PROCESSING))
    out = _run(server.verify_claim(_RESULT['claim'], _ctx(), depth='low'))
    assert out['depth'] == 'low'


def test_a_check_already_running_leads_with_that(monkeypatch):
    _patch_api(monkeypatch, 'verify', ApiResponse(status=409, data={'detail': 'in progress', 'task_id': _TASK_ID}))
    _patch_api(monkeypatch, 'verify_status', ApiResponse(status=200, data=_STATUS_PROCESSING))
    out = _run(server.verify_claim(_RESULT['claim'], _ctx()))
    assert out['message'].startswith('This claim is already being checked.')
    assert 'still running' not in out['message'].split('.')[0]


def test_get_verification_still_running_names_the_way_back(monkeypatch):
    _patch_api(monkeypatch, 'verify_status', ApiResponse(status=200, data=_STATUS_PROCESSING))
    out = _run(server.get_verification(_TASK_ID, _ctx()))
    assert out['status'] == 'processing'
    assert out['task_id'] == _TASK_ID
    assert out['elapsed_seconds'] == 41
    # The status API carries no depth, so none is invented.
    assert 'depth' not in out
    wait = int(config.VERIFY_WAIT_SECONDS)
    assert out['message'].startswith(f'Still running after {wait} seconds.')
    assert '`list_verifications`' in out['message']


# ── how long one call waits for a deep check ─────────────────────────

# Claude's tool-call ceiling, measured with the dev probe connector
# (scripts/probe). On the LEGACY protocol (2026-09-17) calls returned at
# 210 s and were cut at ~240 s. On the MODERN protocol (2026-07-28), which Claude
# negotiates with the live server, 150 s completed and 210 s was cut
# (2026-09-18, with a direct 210 s control call that completed). The
# exact modern ceiling between 150 and 210 s is unmeasured, so the ratchet is
# the longest call SEEN to complete.
CLAUDE_MODERN_PROVEN_SECONDS = 150

# Measured 2026-09-18 with the same probe connector in ChatGPT: a tool call was
# dropped at 119.8 s and the user was shown "HTTP 504". Progress notifications
# do not extend it. The whole CALL has to land under about 110 s, not just the
# wait loop, so the margin here is what pays for the last poll and the HTTP hop.
CHATGPT_TOOL_CALL_CUT_SECONDS = 119


def _fresh_config():
    import importlib.util

    spec = importlib.util.spec_from_file_location('lenz_mcp_config_fresh', config.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_wait_defaults():
    fresh = _fresh_config()
    assert fresh.VERIFY_WAIT_SECONDS_BY_IDENTITY == {
        'Claude-User': 130,
        'openai-mcp': 100,
        # The same ChatGPT app, from the build that began sending this suffix
        # on 2026-09-23. Under the identity key it matched no row and fell to
        # the 45 s default, which is the bug this row closes.
        'openai-mcp (ChatGPT)': 100,
        'openai-mcp (Codex)': 100,
        'openai-mcp (Responses API)': 45,
    }
    assert fresh.VERIFY_WAIT_SECONDS == 45
    assert fresh.VERIFY_POLL_INTERVAL == 3


def test_every_measured_wait_stays_under_the_cut():
    """Any request timeout in front of the server must also outlast these
    waits; that is a deployment setting, not checked here."""
    fresh = _fresh_config()
    # Claude's whole call (the wait plus one last poll, 15 s at worst) must end
    # inside the longest call seen to complete on the modern protocol.
    assert fresh.VERIFY_WAIT_SECONDS_BY_IDENTITY['Claude-User'] <= CLAUDE_MODERN_PROVEN_SECONDS - 15
    # ChatGPT's cut is much tighter, and it showed the user a 504 rather than a
    # late answer: the whole call must end under ~110 s, with margin for
    # the last poll and the HTTP hop.
    assert fresh.VERIFY_WAIT_SECONDS_BY_IDENTITY['openai-mcp'] <= CHATGPT_TOOL_CALL_CUT_SECONDS - 15


@pytest.mark.parametrize(
    ('user_agent', 'claude'),
    [
        ('Claude-User', True),
        ('Claude-User/1.0', True),
        ('openai-mcp/1.0.0', False),
        ('claude-code/2.1.4', False),
        ('node', False),
        ('', False),
    ],
)
def test_only_the_measured_client_gets_the_long_wait(monkeypatch, user_agent, claude):
    """ChatGPT's timeout is reported at ~60 s and the TypeScript SDK's default
    is 60 s: an unknown or absent User-Agent keeps the short wait."""
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS_BY_IDENTITY', {'Claude-User': 210.0})
    _as_client(monkeypatch, user_agent)
    assert server._verify_wait_seconds() == (210.0 if claude else config.VERIFY_WAIT_SECONDS)


# One token, two clients, two ceilings (measured 2026-09-18): the ChatGPT app
# cuts at 119.8 s, its Responses-API connector at 59.8 s and then RETRIES once.
# A wait keyed on the token alone would give that developer two dropped calls
# and a 504 on every slow check.
@pytest.mark.parametrize(
    ('user_agent', 'wait'),
    [
        ('Claude-User', 210.0),
        ('Claude-User/1.0', 210.0),
        ('openai-mcp/1.0.0', 100.0),
        ('openai-mcp/1.0.0 (Codex)', 100.0),
        ('openai-mcp/1.0.0 (Responses API)', 45.0),
        # An unrecognised suffix must NOT inherit its token's row.
        ('openai-mcp/1.0.0 (Something New)', 45.0),
        ('node', 45.0),
        ('', 45.0),
    ],
)
def test_the_wait_is_read_through_the_real_request_context(monkeypatch, user_agent, wait):
    """Not a stubbed helper: the per-request context the server really reads.

    2.x removed the SDK's `request_ctx`; the request's identity now lives in
    `lenz_mcp.client`'s own contextvar, bound by `lenz_mcp.middleware` from the
    inbound User-Agent and read back by `client_user_agent`. `bind_client_user_agent`
    is that same binding, so everything below the header hop is the real path
    (and `test_dual_era` drives the header hop itself, inside a real request).
    """
    monkeypatch.setattr(
        config,
        'VERIFY_WAIT_SECONDS_BY_IDENTITY',
        {'Claude-User': 210.0, 'openai-mcp': 100.0, 'openai-mcp (Codex)': 100.0, 'openai-mcp (Responses API)': 45.0},
    )
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS', 45.0)
    reset = client.bind_client_user_agent(user_agent)
    try:
        assert server._verify_wait_seconds() == wait
    finally:
        reset()


def _simulated_clock(monkeypatch, polls, *, finish_at, credential_ok=None):
    """Time advances only when the wait sleeps. When `credential_ok` is given,
    each status poll's minted credential is checked with it at that moment."""
    import time as time_module

    clock = {'now': 1_000_000.0}
    monkeypatch.setattr(time_module, 'time', lambda: clock['now'])
    monkeypatch.setattr(time_module, 'monotonic', lambda: clock['now'])

    async def _advance(seconds):
        clock['now'] += seconds

    monkeypatch.setattr(server, '_sleep', _advance)
    start = clock['now']

    async def _status(authorization, task_id):
        polls.append(authorization)
        minted = authorization and authorization.startswith('Bearer ') and not authorization.startswith('Bearer lenz_')
        if minted and credential_ok is not None and not credential_ok(authorization.removeprefix('Bearer ')):
            return ApiResponse(status=401, data={'detail': 'expired'})
        if clock['now'] - start >= finish_at:
            return ApiResponse(status=200, data=_STATUS_COMPLETED)
        return ApiResponse(status=200, data=_STATUS_PROCESSING)

    monkeypatch.setattr(client, 'verify_status', _status)


def test_an_api_key_wait_forwards_the_key_on_every_poll(monkeypatch):
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS_BY_IDENTITY', {'Claude-User': 210.0})
    _as_client(monkeypatch, 'Claude-User')
    polls = []
    _simulated_clock(monkeypatch, polls, finish_at=150)
    out = _run(server.get_verification(_TASK_ID, _ctx(auth='Bearer lenz_testkey')))
    assert out['status'] == 'completed'
    assert set(polls) == {'Bearer lenz_testkey'}


def test_claude_still_running_names_the_wait_it_got(monkeypatch):
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS_BY_IDENTITY', {'Claude-User': 2.5})
    _as_client(monkeypatch, 'Claude-User')
    _patch_api(monkeypatch, 'verify_status', ApiResponse(status=200, data=_STATUS_PROCESSING))
    out = _run(server.get_verification(_TASK_ID, _ctx()))
    assert out['message'].startswith('Still running after 2 seconds.')


def test_an_exhausted_wait_is_reported_from_the_wait_itself(monkeypatch):
    """The signal, driven through the REAL loop — not by calling the emitter.

    `mcp_verify_wait_exhausted` is the one line saying a client's wait is too
    short, and the runbook's first symptom. A test that calls
    `decisions.log_wait_exhausted` directly proves the line's shape and nothing
    about whether `_await_verification` ever reaches it: deleting the call from
    the loop left the whole suite green. So this drives `verify_claim` — the
    tool whose wait matters most, and NOT one of the callers that writes a
    still-running message — until the budget runs out, and reads the collector.

    It pins the tool NAME too: that is what tells a constant stream from one
    surface apart from ordinary ceiling hits spread across all three tools.
    """
    import contextlib
    import logging

    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    @contextlib.contextmanager
    def _collecting():
        # At the logger: `lenz_mcp.decisions` is in `observability.INFO_LOGGERS`
        # and so does not propagate to caplog's root handler.
        log = logging.getLogger('lenz_mcp.decisions')
        handler = _Collect(level=logging.INFO)
        previous = log.level
        log.setLevel(logging.INFO)
        log.addHandler(handler)
        try:
            yield
        finally:
            log.removeHandler(handler)
            log.setLevel(previous)

    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS_BY_IDENTITY', {'Claude-User': 5.0})
    _as_client(monkeypatch, 'Claude-User')
    _patch_api(monkeypatch, 'verify', ApiResponse(status=202, data={'task_id': _TASK_ID}))
    # Never finishes, so the budget is what ends the call.
    _simulated_clock(monkeypatch, [], finish_at=10_000)

    with _collecting():
        out = _run(server.verify_claim('The claim.', _ctx()))

    assert out['status'] == 'submitted', out
    lines = [m for m in messages if m.startswith('mcp_verify_wait_exhausted ')]
    assert len(lines) == 1, messages
    fields = dict(part.split('=', 1) for part in lines[0].split(' ')[1:])
    assert fields == {'identity': 'Claude-User', 'wait': '5', 'tool': 'verify_claim'}


def test_no_model_facing_text_hard_codes_the_wait():
    # The per-client verify description went with the skybridge widget, so the
    # instructions and the tool docstrings are the whole model-facing surface.
    texts = [server.mcp.instructions]
    for tool in _tools().values():
        texts.append(tool.description or '')
    for text in texts:
        flat = ' '.join(text.split())
        for pinned in ('45 seconds', 'up to 45', '210 seconds', 'up to 210'):
            assert pinned not in flat, (pinned, flat[:80])


def test_a_multi_claim_selection_names_no_wait(monkeypatch):
    _patch_api(
        monkeypatch,
        'select',
        ApiResponse(
            status=200,
            data={
                'batch_id': 'b1',
                'items': [{'task_id': 'a' * 32, 'claim_text': 'one'}, {'task_id': 'b' * 32, 'claim_text': 'two'}],
            },
        ),
    )
    out = _run(server.select_claims(_TASK_ID, ['one', 'two'], _ctx()))
    assert out['status'] == 'submitted'
    assert 'seconds' not in out['message']
    assert 'get_verification' in out['message']


# ── check_usage is not a step before a check ─────────────────────────


def test_check_usage_hands_over_a_first_check(monkeypatch):
    _patch_api(
        monkeypatch,
        'me_usage',
        ApiResponse(status=200, data={'plan': 'free', 'credits': {'remaining': 300}, 'costs': {'verify': 10}}),
    )
    out = _run(server.check_usage(_ctx()))
    assert out['credits_remaining'] == 300
    assert out['next'] == server.USAGE_NEXT_NOTE
    assert 'Check with Lenz whether' in out['next']
    assert 'Check the claims in this draft with Lenz.' in out['next']
    assert 'Use Lenz to' not in out['next']
    assert 'not a prerequisite' in _tools()['check_usage'].description.lower()


# ── list_verifications: a result nobody collected ────────────────────


def test_list_verifications_returns_the_recent_deep_checks(monkeypatch):
    calls = _patch_api(monkeypatch, 'list_verifications', ApiResponse(status=200, data=_LISTING))
    out = _run(server.list_verifications(_ctx()))
    assert calls == [(('Bearer lenz_testkey',), {'page_size': server.RECENT_CHECKS_LIMIT})]
    assert server.RECENT_CHECKS_LIMIT == 10
    assert out['status'] == 'ok'
    assert out['total'] == 14
    assert out['checks'] == [
        {
            'verification_id': 'a1b2c3d4',
            'claim': 'GDPR requires consent for any processing of personal data.',
            'verdict': 'False',
            'lenz_score': 2,
            'confidence': 'high',
            'key_finding': 'Consent is one of six lawful bases for processing under Article 6 of the GDPR.',
            'checked_at': '2026-09-17T09:12:44+00:00',
        }
    ]
    assert out['message'] == server.RECENT_CHECKS_MESSAGE


def test_list_verifications_with_no_history(monkeypatch):
    _patch_api(monkeypatch, 'list_verifications', ApiResponse(status=200, data={**_LISTING, 'items': [], 'total': 0}))
    out = _run(server.list_verifications(_ctx()))
    assert out['status'] == 'ok'
    assert out['checks'] == []
    assert out['total'] == 0
    assert out['message'] == server.NO_RECENT_CHECKS_MESSAGE
    assert 'quick checks are not stored' in out['message']


def test_list_verifications_skips_malformed_rows(monkeypatch):
    listing = {**_LISTING, 'items': [None, 'x', {'claim': 'no id'}, _LISTING['items'][0]], 'total': 'n/a'}
    _patch_api(monkeypatch, 'list_verifications', ApiResponse(status=200, data=listing))
    out = _run(server.list_verifications(_ctx()))
    assert [c['verification_id'] for c in out['checks']] == ['a1b2c3d4']
    assert out['total'] == 1


@pytest.mark.parametrize(
    ('response', 'status'),
    [
        (ApiResponse(status=503, data={'code': 'capacity', 'retry_after': 90}), 'service_unavailable'),
        (ApiResponse(status=401, data={'detail': 'Invalid key'}), 'auth_required'),
        (ApiResponse(status=0, data={}), 'error'),
    ],
)
def test_list_verifications_maps_an_api_failure(monkeypatch, response, status):
    _patch_api(monkeypatch, 'list_verifications', response)
    out = _run(server.list_verifications(_ctx()))
    assert out['status'] == status
    assert 'checks' not in out


def test_list_verifications_without_a_key():
    assert _run(server.list_verifications(_ctx(auth=None)))['status'] == 'auth_required'


def test_list_verifications_is_one_get_scoped_by_the_callers_key(monkeypatch):
    """It must never start or charge a check, and it lists only what the
    caller's own credential can read: one GET, the caller's Authorization
    forwarded verbatim, no idempotency key (a write marker), no body."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_LISTING)

    real_client = httpx.AsyncClient

    def _factory(*_args, **kwargs):
        kwargs.pop('transport', None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(client.httpx, 'AsyncClient', _factory)
    out = _run(server.list_verifications(_ctx(auth='Bearer lenz_callerkey')))

    assert out['status'] == 'ok'
    assert len(seen) == 1
    request = seen[0]
    assert request.method == 'GET'
    assert request.url.path.endswith('/api/v1/verifications')
    assert dict(request.url.params) == {'page': '1', 'page_size': '10'}
    assert request.headers['authorization'] == 'Bearer lenz_callerkey'
    assert 'idempotency-key' not in request.headers
    assert request.content == b''


def test_list_verifications_is_advertised_as_a_read():
    tool = _tools()['list_verifications']
    assert tool.title == 'Your recent checks'
    assert tool.annotations.title == 'Your recent checks'
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.input_schema.get('properties', {}) == {}
    description = ' '.join(tool.description.lower().split())
    assert 'quick checks' in description and 'not stored' in description
    assert 'still running' in description
    assert 'no credits' in description


# The instruction budget lives ONCE, as INSTRUCTIONS_MAX_CHARS above. A second
# constant here would be the same ratchet at the current length: two numbers
# for one rule, where the lower one silently becomes the real budget and the
# reason for the other is lost.


def test_the_instructions_say_nothing_about_a_card():
    # The model cannot tell whether a card rendered (measured: same tool, same
    # result, one chat drew it and one showed text only), so the instructions
    # must stay true for a client that renders none. An omission rule would
    # degrade into a bare verdict exactly where the card is absent — the
    # complaint the card was built to answer.
    flat = ' '.join(server.mcp.instructions.split()).lower()
    for phrase in ['card', 'widget', 'do not repeat']:
        assert phrase not in flat, phrase


def test_a_connector_retry_of_the_same_check_replays_it(monkeypatch):
    """OpenAI's API connector RETRIES a dropped tool call once, by itself.

    Measured 2026-09-18: a call it cut at 59.76 s was re-sent and cut again at
    59.81 s before the model was told 504. So a slow deep check is submitted
    TWICE from one user action, and the second submission must replay the
    first — not start and charge a second check. The idempotency key is derived
    from the call's content, so the two carry the same key and the API replays.
    (The same shape shows up when a client calls the tool from a fresh
    conversation: two POSTs seconds apart, replayed.)
    """
    import lenz_mcp.client as real_client

    keys = []

    async def _request(method, path, authorization, **kwargs):
        keys.append(kwargs.get('idempotency_key'))
        # What the API does with a replayed key: the SAME task, once.
        return ApiResponse(status=202, data={'task_id': 'r' * 32, 'status': 'queued'})

    monkeypatch.setattr(real_client, '_request', _request)
    first = _run(real_client.verify(None, text='The claim.', language='', depth='standard'))
    second = _run(real_client.verify(None, text='The claim.', language='', depth='standard'))
    assert first.data['task_id'] == second.data['task_id']
    assert len(keys) == 2 and keys[0] == keys[1] and keys[0]
    # A different claim is a different key, or a retry would replay the wrong run.
    _run(real_client.verify(None, text='Another claim.', language='', depth='standard'))
    assert keys[2] != keys[0]


def test_the_claims_cap_the_model_is_told_is_the_one_the_api_enforces():
    """`assess_claim` tells the model how many items `claims` may carry. The
    API owns that number and rejects past it, so a tool text that says 20 over
    an API that takes 15 would have the model send lists that fail. The
    connector's copy must equal the API's limit."""
    claims = _tools()['assess_claim'].input_schema['properties']['claims']['description']
    assert f'up to {config.ASSESS_MAX_CLAIMS} claims' in claims


# The WHOLE tool call has to land under the client's cut, not just the wait
# loop. If the budget started after the submission, then with one last poll
# still running at the deadline, a slow submit plus a slow last poll would take
# the call to wait + ~28 s: 128 s for the ChatGPT app against its measured 119.8 s cut (a 504
# on the app's ordinary path), and 158 s for Claude's 130 s wait against
# the 150 s proven to complete. Each HTTP hop is bounded at
# 15 s by the client, so 14 s is a realistic worst case for one slow hop.
#
# Pinned to MEASURED ceilings only: for Claude the longest call seen to complete
# on the modern protocol (150 s; 210 s was cut), for the ChatGPT app its 119.8 s
# cut. See CLAUDE_MODERN_PROVEN_SECONDS.
_SLOW_HOP = 14.0


def _slow_api_clock(monkeypatch):
    import time as time_module

    clock = {'now': 5_000_000.0}
    monkeypatch.setattr(time_module, 'monotonic', lambda: clock['now'])

    async def _advance(seconds):
        clock['now'] += seconds

    monkeypatch.setattr(server, '_sleep', _advance)
    return clock


@pytest.mark.parametrize(
    ('user_agent', 'cut'),
    [('Claude-User', CLAUDE_MODERN_PROVEN_SECONDS), ('openai-mcp/1.0.0', CHATGPT_TOOL_CALL_CUT_SECONDS)],
)
@pytest.mark.parametrize('tool', ['verify_claim', 'select_claims', 'get_verification'])
def test_a_waiting_call_ends_inside_the_cut_even_when_the_api_is_slow(monkeypatch, user_agent, cut, tool):
    assert cut in (CLAUDE_MODERN_PROVEN_SECONDS, CHATGPT_TOOL_CALL_CUT_SECONDS)
    # The REAL waits: the suite shrinks them to keep tests fast, which would
    # make this test measure nothing.
    fresh = _fresh_config()
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS_BY_IDENTITY', dict(fresh.VERIFY_WAIT_SECONDS_BY_IDENTITY))
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS', fresh.VERIFY_WAIT_SECONDS)
    monkeypatch.setattr(config, 'VERIFY_POLL_INTERVAL', fresh.VERIFY_POLL_INTERVAL)
    clock = _slow_api_clock(monkeypatch)
    reset = client.bind_client_user_agent(user_agent)
    wait = server._verify_wait_seconds()
    assert wait == fresh.VERIFY_WAIT_SECONDS_BY_IDENTITY[client.client_identity()], 'the real wait'
    entered = clock['now']

    async def _slow_submit(*args, **kwargs):
        clock['now'] += _SLOW_HOP
        if tool == 'verify_claim':
            return ApiResponse(status=202, data={'task_id': 't' * 32})
        return ApiResponse(status=202, data={'items': [{'task_id': 't' * 32, 'claim_text': 'X'}]})

    async def _status(authorization, task_id):
        # Every poll is slow, so a deadline timed from after the submit shows up
        # for every client. (Slowing only the polls near the deadline let the old
        # loop pass for Claude by phase luck.) The last poll STARTING at the
        # deadline is the next test's case.
        clock['now'] += _SLOW_HOP
        return {'status': 'processing', 'task_id': task_id}

    monkeypatch.setattr(client, 'verify', _slow_submit)
    monkeypatch.setattr(client, 'select', _slow_submit)
    monkeypatch.setattr(server, '_verification_result', _status)
    if tool == 'get_verification':
        # get_verification submits nothing; slow its own checks before the wait
        # instead, so a deadline started late would show here too.
        real_sendable = server._sendable_id

        def _slow_sendable(ident):
            clock['now'] += _SLOW_HOP
            return real_sendable(ident)

        monkeypatch.setattr(server, '_sendable_id', _slow_sendable)
    try:
        if tool == 'verify_claim':
            out = _run(server.verify_claim('The claim.', _ctx()))
        elif tool == 'select_claims':
            out = _run(server.select_claims('t' * 32, ['X'], _ctx()))
        else:
            out = _run(server.get_verification('t' * 32, _ctx()))
    finally:
        reset()
    elapsed = clock['now'] - entered
    # get_verification hands the still-running run back as-is; the other two as `submitted`.
    assert out['status'] in ('submitted', 'processing'), out
    # The invariant, for every client: the whole call ends by the wait plus ONE
    # poll. Before the fix it was the wait plus the submit plus a poll.
    assert elapsed <= wait + _SLOW_HOP + 1, f'{tool} for {user_agent}: {elapsed:.1f} s for a {wait:.0f} s wait'
    # And inside the client's measured cut.
    assert elapsed < cut, f'{tool} for {user_agent} took {elapsed:.1f} s against a {cut} s cut'
    if user_agent.startswith('openai-mcp'):
        # The ChatGPT app shows the user a 504 at 119.8 s: keep the whole call under ~115 s.
        assert elapsed <= 115, f'{tool} took {elapsed:.1f} s; the ChatGPT app cuts at 119.8 s'


@pytest.mark.parametrize(
    ('user_agent', 'cut'),
    [('Claude-User', CLAUDE_MODERN_PROVEN_SECONDS), ('openai-mcp/1.0.0', CHATGPT_TOOL_CALL_CUT_SECONDS)],
)
def test_a_slow_last_poll_at_the_deadline_still_lands_inside_the_cut(monkeypatch, user_agent, cut):
    # The boundary: quick polls until the deadline, then one last poll that
    # starts at the deadline and takes the client's full 15 s timeout.
    fresh = _fresh_config()
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS_BY_IDENTITY', dict(fresh.VERIFY_WAIT_SECONDS_BY_IDENTITY))
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS', fresh.VERIFY_WAIT_SECONDS)
    monkeypatch.setattr(config, 'VERIFY_POLL_INTERVAL', fresh.VERIFY_POLL_INTERVAL)
    clock = _slow_api_clock(monkeypatch)
    reset = client.bind_client_user_agent(user_agent)
    wait = server._verify_wait_seconds()
    entered = clock['now']
    last_poll_started = []

    async def _submit(*args, **kwargs):
        clock['now'] += 1.0
        return ApiResponse(status=202, data={'task_id': 't' * 32})

    async def _status(authorization, task_id):
        at_deadline = clock['now'] - entered >= wait
        if at_deadline:
            last_poll_started.append(clock['now'] - entered)
        clock['now'] += 15.0 if at_deadline else 0.5
        return {'status': 'processing', 'task_id': task_id}

    monkeypatch.setattr(client, 'verify', _submit)
    monkeypatch.setattr(server, '_verification_result', _status)
    try:
        out = _run(server.verify_claim('The claim.', _ctx()))
    finally:
        reset()
    elapsed = clock['now'] - entered
    assert out['status'] == 'submitted', out
    assert last_poll_started, 'the scenario must reach a poll at the deadline'
    assert elapsed <= wait + 15.0
    assert elapsed < cut, f'{user_agent}: {elapsed:.1f} s against a {cut} s ceiling'


# ── What the model may repeat to the user ────────────────────────────
# The model paraphrases a result string to the user, so a string it may repeat
# says what to SAY and names a tool only in a separate instruction sentence, and
# never mentions credits in the sayable part (the copy rules). In the 2026-09-18
# sitting Claude told a user "Lenz recommends a deep check (verify_claim) …
# costs more credits", straight from `next_step`.
_TOOL_NAMES = (
    'verify_claim',
    'assess_claim',
    'get_verification',
    'list_verifications',
    'check_usage',
    'ask_followup',
    'select_claims',
)


def _clauses(text):

    # A semicolon ends a clause as surely as a full stop: "Then call X; tell the
    # user that Y is running" must not hide its second half behind the first.
    return [c for c in re.split(r'(?<=[.!?;])\s*', text.strip()) if c]


def _say_do_violations(text):

    out = []
    for clause in _clauses(text):
        for name in _TOOL_NAMES:
            for match in re.finditer(re.escape(name), clause):
                # A tool may be named only as the object of an instruction.
                before = clause[: match.start()].rstrip('`')
                if not re.search(r'(?:\b[Cc]all|\btool is)\s*$', before):
                    out.append(f'names {name} outside an instruction: {clause!r}')
        if re.search(r'credit', clause, re.IGNORECASE) and not clause.startswith('Do not mention'):
            out.append(f'mentions credits: {clause!r}')
    return out


def test_the_guard_catches_what_it_is_for():
    assert _say_do_violations('Then call `get_verification`; tell the user that verify_claim is running.')
    assert _say_do_violations('Tell the user.Recommend `verify_claim`.')
    assert _say_do_violations('It costs more Credits.')
    assert _say_do_violations('Offer `select_claims`.')
    assert not _say_do_violations('Tell the user. Do not mention tool names or credits. Then call `verify_claim`.')


def test_low_confidence_next_step_is_the_approved_say_do_text():
    assert server.LOW_CONFIDENCE_NEXT_STEP == (
        'Lenz is not confident in this quick verdict. Tell the user that a deep check would investigate '
        'the claim against independent sources and takes about a minute to a minute and a half, and ask '
        'whether to run it. Do not mention tool names or credits to the user. If they say yes, call '
        '`verify_claim` with this claim.'
    )


@pytest.mark.parametrize(
    'name',
    ['LOW_CONFIDENCE_NEXT_STEP', 'ASSESS_ESCALATION_NOTE', 'PRESENTATION_NOTE', 'SUPERSEDES_NOTE', 'USAGE_NEXT_NOTE'],
)
def test_result_strings_keep_tool_names_and_credits_out_of_what_the_user_hears(name):
    assert _say_do_violations(getattr(server, name)) == []


# Exempt BY NAME, not by loosening the pattern. Running out is
# the one moment credits ARE the message: an assistant user who hits the wall
# must be told why in plain numbers (the copy rules for an assistant user). The
# rule is about the OFFER of a deep check, where a price is noise. Those
# messages are built in `_quota_message`, never a constant, so they are pinned
# by their own test below instead.
_SAY_DO_EXEMPT: frozenset[str] = frozenset()


def _module_strings():
    return {
        name: value
        for name, value in vars(server).items()
        if name.isupper() and isinstance(value, str) and name not in _SAY_DO_EXEMPT
    }


def test_every_result_string_in_the_server_keeps_the_split():
    strings = _module_strings()
    # A scan that reads nothing passes forever: name what it must see.
    for must in ('LOW_CONFIDENCE_NEXT_STEP', 'STILL_RUNNING_AFTER_WAIT', 'FOLLOWUP_NOT_COMPLETED', 'ASSESS_NOTES_NOTE'):
        assert must in strings, must
    bad = {name: v for name, value in strings.items() if (v := _say_do_violations(value.replace('{seconds}', '130')))}
    assert bad == {}


def test_the_whole_assess_confidence_note_keeps_the_split():
    note = server.CONFIDENCE_NOTE + server.ASSESS_ESCALATION_NOTE + server.ASSESS_NOTES_NOTE
    assert _say_do_violations(note) == []
    assert '  ' not in note


def test_quota_messages_name_credits_and_nothing_to_buy():
    for body in ({'credits_remaining': 3, 'cost': 10}, {'credits_remaining': 0, 'cost': 10}, {}):
        text = server._quota_message(body)
        assert 'credits' in text
        for word in ('plan', 'upgrade', 'price', '$', '€', 'Pro'):
            assert word not in text, (word, text)


@pytest.mark.parametrize('already_running', [False, True])
@pytest.mark.parametrize('card', [False, True])
def test_the_still_running_text_keeps_tool_names_out_of_what_the_user_hears(monkeypatch, already_running, card):
    monkeypatch.setattr(server, '_card_active', lambda: card)
    text = server._submitted_message(already_running=already_running)
    assert _say_do_violations(text) == []
    assert 'Do not mention tool names' in text
