"""Which Lenz tool should a model choose, and when should it choose none.

The single source of truth for the tool-choice eval AND for the text we submit
to OpenAI: the eight ``openai_submission`` cases carry the submission's own
wording, and ``--print-submission`` renders the dashboard text from them. What
we submit and what we test cannot drift, because they are the same object.

A case is data, never a model judgement:

``id``            unique, stable; result files are keyed on it.
``group``         one of GROUPS; gates are per group.
``history``       prior turns as ``(role, text)``. An assistant turn may be a
                  plain answer (the unnamed triggers need one to doubt) or a canned
                  tool result, which is passed as an assistant turn describing
                  what came back rather than as a real tool-result block: the
                  eval never executes a tool, and a model given a fabricated
                  tool_use id would be reasoning about a call it never made.
``prompt``        the user's turn under test.
``expect``        a tool name, an ordered list of names, or ``NONE``.
``expect_args``   optional predicates on the chosen call's arguments.
``forbid``        tools that must not be called, whatever else happens.
``at_most``       with ``expect=RESTRAINT``: the only tools that may be called,
                  each with a ceiling. Zero calls always passes.
``why``           what this case is protecting. Read it before changing a case:
                  several of these exist because a real edit broke them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

NONE = 'none'
# Judged by `at_most` instead of by a tool name: the case is about RESTRAINT,
# where calling nothing is as good an answer as calling one thing (asking the
# user which claims matter is the best possible behaviour, so zero calls must
# not read as a failure).
RESTRAINT = 'restraint'

GROUPS = (
    'openai_submission',
    'named',
    'unnamed_triggers',
    'must_not_fire',
    'escalation',
    'housekeeping',
    'language',
)


@dataclass(frozen=True)
class Case:
    id: str
    group: str
    prompt: str
    expect: str | list[str]
    why: str
    history: tuple[tuple[str, str], ...] = ()
    expect_args: dict[str, Callable[[Any], bool]] = field(default_factory=dict)
    forbid: tuple[str, ...] = ()
    at_most: tuple[tuple[str, int], ...] = ()
    # The whole answer must be ONE call. A draft is one `assess_claim` with the
    # text in `claim`; a second call is a second charge, and a judge that read
    # only the first call would pass it.
    single_call: bool = False
    # The submission's "Tool triggered" line, when the call SHAPE matters to a
    # reviewer (one call; the whole text in `claim`; `get_verification` may
    # follow on its own). Must name every tool in `expect` -- a test holds it
    # to that, so the prose cannot drift from what the eval asserts.
    tools_line: str = ''
    # Only the submission cases carry these: they render the dashboard text.
    scenario: str = ''
    expected_output: str = ''

    @property
    def expects_none(self) -> bool:
        return self.expect == NONE

    @property
    def expected_tools(self) -> list[str]:
        if self.expect in (NONE, RESTRAINT):
            return []
        return [self.expect] if isinstance(self.expect, str) else list(self.expect)

    @property
    def allowed_tools(self) -> list[str]:
        return [name for name, _ in self.at_most]


# A quick result the model can escalate from, phrased as the assistant's own
# report of what came back. Kept here so every escalation case reads the same.
QUICK_LOW = (
    'assistant',
    'Lenz checked that as a first read: Mostly False, with LOW confidence. '
    'The reviewers noted the figure is repeated widely but traced to no source. '
    'A deep check against sources is available if you want one.',
)
QUICK_HIGH = (
    'assistant',
    'Lenz checked that as a first read: True, with high confidence. '
    'A deep check against sources is available if you want one.',
)
DEEP_DONE = (
    'assistant',
    'The deep check finished: False, 2/10, high confidence (verification a1b2c3d4). It drew on 14 sources.',
)


def _whole_draft(prompt: str):
    """A `claim` predicate: the WHOLE draft quoted in `prompt`, every sentence.

    Checking fragments alone ('Eiffel' and '1950') would pass a claim of just
    "Eiffel 1950", and the hedged case would never check the hedges it exists
    to protect. Whitespace and case are normalised; wording, figures and hedges
    are not.
    """
    import re

    draft = prompt.split('"', 1)[1].rsplit('"', 1)[0]
    sentences = [s.strip(' ."') for s in re.split(r'(?<=\.)\s+', draft) if s.strip(' ."')]

    def _norm(text: str) -> str:
        return ' '.join(text.split()).lower()

    def check(value) -> bool:
        return isinstance(value, str) and all(_norm(s) in _norm(value) for s in sentences)

    return check


CASES: tuple[Case, ...] = (
    # ── The eight OpenAI submission cases, verbatim ───────────────────
    Case(
        id='submission-1-quick',
        single_call=True,
        tools_line='`assess_claim` (one call).',
        group='openai_submission',
        scenario='The user doubts a common belief and asks for a check.',
        prompt='Check with Lenz: lightning never strikes the same place twice.',
        expect='assess_claim',
        forbid=('verify_claim',),
        expected_output=(
            'Within about 25 seconds, a verdict of False (or Mostly False) with high '
            'confidence, presented as a quick first read, plus a short reasoning that tall '
            'or isolated objects are struck repeatedly. ChatGPT mentions a deep check with '
            'sources is available and does NOT start one.'
        ),
        why='The default path. A deep check here would cost ten credits for a claim one credit settles.',
    ),
    Case(
        id='submission-2-draft',
        tools_line='`assess_claim` (one call, the whole text in `claim`).',
        single_call=True,
        group='openai_submission',
        scenario='The user pastes a paragraph they are about to publish.',
        prompt=(
            'Check the claims in this draft with Lenz: "The Eiffel Tower was completed in 1889 '
            "for the World's Fair. It stands about 330 metres tall. It remained the tallest "
            'structure in the world until 1950."'
        ),
        expect='assess_claim',
        expect_args={
            # `claim` is set below to the WHOLE quoted draft (_whole_draft).
            # `claims` is for a list the USER separated. A model that splits a
            # pasted draft itself replaces the service's reading with its own,
            # and the case could still LOOK right (three verdicts appear).
            'claims': lambda v: not v,
        },
        forbid=('verify_claim', 'select_claims'),
        expected_output=(
            "Three separate verdicts in one reply: completed in 1889 for the World's Fair — True; "
            'about 330 metres tall — True; tallest until 1950 — False (surpassed in 1930 by the '
            'Chrysler Building). ChatGPT points out the one wrong sentence. No deep check is started.'
        ),
        why='The WHOLE text goes in one call. Splitting it into three calls charges three times.',
    ),
    Case(
        id='submission-3-deep',
        single_call=True,
        tools_line='`verify_claim` (one call; if the check runs long, ChatGPT follows with `get_verification` on its own, with no user action).',
        group='openai_submission',
        scenario='The user wants evidence they can cite.',
        prompt='Use Lenz to verify with sources: Vikings wore horned helmets in battle.',
        expect='verify_claim',
        expect_args={'depth': lambda v: v in (None, '', 'standard')},
        expected_output=(
            'ChatGPT says a deep check is running (about a minute to a minute and a half). Then: '
            'verdict False (or Mostly False) with a 1-10 score and the confidence, a key finding '
            'that no horned helmet from the Viking age has been found and the image comes from '
            '19th-century art and costume, how many sources the check drew on, and the top sources '
            'with links and what each says.'
        ),
        why='"with sources" is the consent the instructions treat as asking for a deep check.',
    ),
    Case(
        id='submission-4-followup',
        tools_line='`ask_followup` (with the verification_id from case 3).',
        group='openai_submission',
        scenario='Right after case 3, in the same chat.',
        history=(DEEP_DONE,),
        prompt='Ask Lenz: where did the horned helmet image come from?',
        expect='ask_followup',
        expect_args={'verification_id': lambda v: v == 'a1b2c3d4'},
        forbid=('verify_claim', 'assess_claim'),
        expected_output=(
            "A short answer grounded in case 3's sources (19th-century Romanticism; costume design "
            "for Wagner's operas is the usual attribution). No new check is started."
        ),
        why='A follow-up must reuse the verification, not pay for a second check of the same claim.',
    ),
    Case(
        id='submission-5-housekeeping',
        tools_line='`list_verifications`, then `check_usage`.',
        group='openai_submission',
        scenario='The user comes back later. Runs in a NEW chat after case 3.',
        prompt='Show my recent Lenz checks, and how many credits I have left.',
        expect=['list_verifications', 'check_usage'],
        forbid=('assess_claim', 'verify_claim'),
        expected_output=(
            'A list whose newest entry is the Viking helmets check with its verdict and score; then '
            'the remaining credits. Nothing is charged, and no plan, price or upgrade link appears.'
        ),
        why='Two housekeeping tools in one turn, and neither charges.',
    ),
    Case(
        id='submission-n1-opinion',
        group='openai_submission',
        scenario='An opinion.',
        prompt='Is Rome a nicer city to live in than Paris?',
        expect=NONE,
        expected_output='ChatGPT answers itself. No Lenz tool runs: there is no checkable claim.',
        why='The instructions must not read a comparison of tastes as a factual claim.',
    ),
    Case(
        id='submission-n2-arithmetic',
        group='openai_submission',
        scenario='Arithmetic.',
        prompt='What is 17% of 2,340?',
        expect=NONE,
        expected_output='ChatGPT answers 397.8 itself. No Lenz tool runs.',
        why='Arithmetic is named in the instructions as out of scope.',
    ),
    Case(
        id='submission-n3-writing',
        group='openai_submission',
        scenario='A writing task.',
        prompt='Write a two-line poem about lightning.',
        expect=NONE,
        expected_output=('ChatGPT writes it. No Lenz tool runs, although the topic matches case 1.'),
        why='The same subject as case 1 with none of the intent: topic must not trigger.',
    ),
    # ── Named: the user says "Lenz" ───────────────────────────────────
    Case(
        id='named-quick',
        group='named',
        prompt='Check with Lenz whether indeed 90% of startups fail within their first year.',
        expect='assess_claim',
        forbid=('verify_claim',),
        why='Naming Lenz is the strongest signal there is; the quick check is still the default.',
    ),
    Case(
        id='named-draft',
        single_call=True,
        group='named',
        prompt=(
            'Check the claims in this draft with Lenz: "Norway produces most of Europe\'s salmon. '
            'Its aquaculture industry began in the 1970s."'
        ),
        expect='assess_claim',
        expect_args={
            # `claim` is set below to the WHOLE quoted draft (_whole_draft).
            'claims': lambda v: not v,
        },
        why=(
            'A draft goes in whole, in one call, and in `claim` — not split into `claims` by the '
            'model. Measured 2026-09-18: claude-opus-5 called assess_claim with `claim` empty, '
            'which is why the argument is checked and not just the tool.'
        ),
    ),
    Case(
        id='named-sources',
        group='named',
        prompt='Use Lenz to verify with sources: the Great Wall of China is visible from the Moon.',
        expect='verify_claim',
        # `standard` IS the server's default, so a model naming it explicitly
        # changes nothing. `low` is the only wrong answer: a model that passes
        # depth='standard' explicitly is right, and an earlier version of this
        # assertion counted it as a failure.
        expect_args={'depth': lambda v: v in (None, '', 'standard')},
        why=(
            'Asking for sources is consent for the deep check, without a separate yes. `depth` '
            'must never be `low`: the server owns the choice, and a model economising would halve '
            'the evidence behind a verdict the user asked to be able to cite.'
        ),
    ),
    Case(
        id='named-list-uses-claims',
        group='named',
        prompt=(
            'Check these with Lenz: (1) Portugal borders only Spain. (2) Lisbon is on the Tagus. '
            '(3) The Azores are in the Pacific.'
        ),
        expect='assess_claim',
        expect_args={'claims': lambda v: isinstance(v, list) and len(v) == 3},
        why=(
            'The other half of the draft pair: when the USER has separated the claims, `claims` is '
            'the right argument. If a model gets this one wrong in the opposite direction it is '
            "the tool description that is unclear, not the model's judgement."
        ),
    ),
    Case(
        id='named-draft-hedged',
        single_call=True,
        group='named',
        prompt=(
            'Check the claims in this draft with Lenz: "Analysts say roughly 40% of new EV models '
            'launched in 2025 missed their delivery targets. The shortfall was worst in Europe. '
            'Battery costs fell by about a fifth over the same period."'
        ),
        expect='assess_claim',
        expect_args={
            # The whole text, with the hedge and the figure as the user wrote them.
            # `claim` is set below to the WHOLE quoted draft (_whole_draft).
            'claims': lambda v: not v,
        },
        why=(
            'Where model-side splitting would do real damage. "Analysts say roughly 40%" is a '
            'hedged attribution: a split that drops "Analysts say" turns a report of what analysts '
            'claim into a claim about the world, and one that rounds "roughly 40%" or "about a '
            'fifth" changes what is being checked. Added after an earlier run showed claude-sonnet-5 '
            'rewriting a draft it split.'
        ),
    ),
    # ── The unnamed triggers ──────────────────────────────────────────
    Case(
        id='trigger-are-you-sure',
        group='unnamed_triggers',
        history=(('user', 'How tall is the Eiffel Tower?'), ('assistant', 'It is about 330 metres tall.')),
        prompt='are you sure?',
        expect='assess_claim',
        why='Doubting a factual statement the assistant just made is a check request.',
    ),
    Case(
        id='trigger-is-that-right',
        group='unnamed_triggers',
        history=(
            ('user', 'When did the EU AI Act enter into force?'),
            ('assistant', 'It entered into force in August 2024.'),
        ),
        prompt='is that right?',
        expect='assess_claim',
        why='The same doubt, second phrasing.',
    ),
    Case(
        id='trigger-double-check',
        group='unnamed_triggers',
        history=(
            ('user', 'What share of the ocean is unexplored?'),
            ('assistant', 'About 80% of the ocean remains unexplored.'),
        ),
        prompt='double-check that',
        expect='assess_claim',
        why='The same doubt, imperative phrasing.',
    ),
    Case(
        id='trigger-fact-check-paragraph',
        group='unnamed_triggers',
        prompt=(
            'Can you fact-check this paragraph: "Insulin was discovered in 1921 in Toronto. '
            'The patent was sold for one dollar."'
        ),
        expect='assess_claim',
        why='"fact-check" names the job without naming Lenz.',
    ),
    Case(
        id='trigger-is-it-true',
        group='unnamed_triggers',
        prompt='Is it true that honey never spoils?',
        expect='assess_claim',
        why='A yes/no question about a fact is a checkable claim.',
    ),
    # ── Must not fire ─────────────────────────────────────────────────
    Case(
        id='quiet-opinion',
        group='must_not_fire',
        prompt='Which is the better programming language, Rust or Go?',
        expect=NONE,
        why='A matter of taste.',
    ),
    Case(
        id='quiet-prediction',
        group='must_not_fire',
        prompt='Will Bitcoin be above 200k by next summer?',
        expect=NONE,
        why='A prediction has no verifiable present state.',
    ),
    Case(
        id='quiet-arithmetic',
        group='must_not_fire',
        prompt='What is 4,890 divided by 12?',
        expect=NONE,
        why='Named as out of scope.',
    ),
    Case(
        id='quiet-code-review',
        group='must_not_fire',
        prompt='Is this Python correct? def add(a, b): return a - b',
        expect=NONE,
        why='Code is named as out of scope.',
    ),
    Case(
        id='quiet-poem',
        group='must_not_fire',
        prompt='Write a haiku about the sea.',
        expect=NONE,
        why='A writing task.',
    ),
    Case(
        id='quiet-are-you-sure-about-code',
        group='must_not_fire',
        history=(
            ('user', 'Does list.sort() return a new list?'),
            ('assistant', 'No, list.sort() sorts in place and returns None.'),
        ),
        prompt='are you sure?',
        expect=NONE,
        why=(
            'The doubt trigger phrase after an answer about CODE. This is the case that tells '
            'us the triggers read the SUBJECT and not just the words.'
        ),
    ),
    Case(
        id='quiet-small-talk',
        group='must_not_fire',
        prompt='Thanks, that was helpful!',
        expect=NONE,
        why='Nothing is being asked.',
    ),
    Case(
        id='quiet-summarise',
        group='must_not_fire',
        prompt=(
            'Summarise this for me: "The committee met on Tuesday and agreed to postpone the vote '
            'until the autumn session."'
        ),
        expect=NONE,
        why='A summary request over text that happens to contain assertions.',
    ),
    # ── Escalation discipline ─────────────────────────────────────────
    Case(
        id='escalate-on-yes',
        group='escalation',
        history=(('user', 'Do 90% of startups fail in year one?'), QUICK_LOW),
        prompt='yes, go deeper',
        expect='verify_claim',
        why='Low confidence plus an explicit yes is the one clean consent.',
    ),
    Case(
        id='escalate-needs-asking',
        group='escalation',
        history=(('user', 'Do 90% of startups fail in year one?'), QUICK_LOW),
        prompt='hm, interesting',
        expect=NONE,
        forbid=('verify_claim',),
        why=(
            'Low confidence is a reason to OFFER a deep check, never to start one. Ten credits '
            'must not leave on a shrug.'
        ),
    ),
    Case(
        id='escalate-not-on-thanks',
        group='escalation',
        history=(('user', 'Is the Eiffel Tower 330 metres tall?'), QUICK_HIGH),
        prompt='thanks',
        expect=NONE,
        forbid=('verify_claim',),
        why='High confidence and no request: nothing more to do.',
    ),
    Case(
        id='escalate-not-twelve-rows',
        group='escalation',
        history=(
            ('user', 'Check the claims in this report: [12 sentences]'),
            (
                'assistant',
                'Lenz checked 12 claims as a first read. Two look wrong: "revenue grew 40% in 2025" '
                '(Mostly False, low confidence) and "churn fell below 2%" (Mostly False, low '
                'confidence). The other ten hold up.',
            ),
        ),
        prompt='check them all properly',
        expect=RESTRAINT,
        # `check_usage` is ALLOWED here, and that is not a concession: its own
        # description says "Call this … or before a large batch to size it
        # against credits_remaining. It is not a prerequisite: never call it
        # before an ordinary check." A model that checks the balance before 12
        # deep checks is following that sentence exactly. It is not the
        # instructions contradicting themselves: the rule is in the tool's
        # description. `house-no-usage-preflight` guards the other half: before
        # a SINGLE check it is still a failure.
        at_most=(('verify_claim', 2), ('check_usage', 1)),
        forbid=('assess_claim',),
        why=(
            'The instructions say to name at most the two that matter and never to deep-check '
            'every row: twelve deep checks is 120 credits from one sentence. Calling NOTHING and '
            'asking which ones matter is the BEST answer, not a failure, so the '
            'ceiling is what is judged and zero passes. Re-running the quick check is forbidden: '
            'the result is already in the history. Sizing a 12-row batch against the balance first '
            "is allowed, because check_usage's description tells the model to do exactly that."
        ),
    ),
    # ── Housekeeping ──────────────────────────────────────────────────
    Case(
        id='house-earlier-check',
        group='housekeeping',
        prompt='What did that check I ran yesterday say?',
        expect='list_verifications',
        forbid=('assess_claim', 'verify_claim'),
        why='Looking something up must not re-run it.',
    ),
    Case(
        id='house-credits',
        group='housekeeping',
        prompt='How many Lenz credits do I have left?',
        expect='check_usage',
        why='The one tool that answers it.',
    ),
    Case(
        id='house-no-usage-preflight',
        group='housekeeping',
        prompt='Check with Lenz: the Sahara is larger than Brazil.',
        expect='assess_claim',
        forbid=('check_usage',),
        why=(
            'check_usage is never a prerequisite. A model that checks the balance first turns '
            'one call into two and reads as asking permission to spend.'
        ),
    ),
    Case(
        id='house-followup-uses-id',
        group='housekeeping',
        history=(DEEP_DONE,),
        prompt='What did the sources actually say about that?',
        expect='ask_followup',
        expect_args={'verification_id': lambda v: v == 'a1b2c3d4'},
        forbid=('verify_claim',),
        why='The id is in the history; a new check would charge again for an answer we hold.',
    ),
    # ── Language ──────────────────────────────────────────────────────
    Case(
        id='language-german-unset',
        group='language',
        history=(('user', 'Hallo, ich hätte eine Frage.'), ('assistant', 'Gerne, worum geht es?')),
        prompt='Stimmt es, dass Deutschland 2023 mehr Strom exportiert als importiert hat?',
        expect='assess_claim',
        expect_args={'language': lambda v: v in (None, '')},
        why=(
            'The description says to leave `language` unset unless the user asks for an output '
            'language. A model that helpfully sets "de" changes the output language the user did not ask for.'
        ),
    ),
)

# The drafts are judged against their WHOLE quoted text, built from each case's
# own prompt so the two can never disagree. `expect_args` is a dict, so this
# fills it in place on the frozen cases.
_DRAFTS = ('submission-2-draft', 'named-draft', 'named-draft-hedged')
for _case in CASES:
    if _case.id in _DRAFTS:
        _case.expect_args['claim'] = _whole_draft(_case.prompt)


def by_group(group: str) -> list[Case]:
    return [case for case in CASES if case.group == group]


def by_id(case_id: str) -> Case:
    for case in CASES:
        if case.id == case_id:
            return case
    raise KeyError(case_id)


SUBMISSION_IDS = tuple(case.id for case in CASES if case.group == 'openai_submission')


def render_submission() -> str:
    """The OpenAI dashboard text, rendered FROM the cases.

    So the eight cases we test and the eight we submit cannot drift: there is
    one object, and this is its other rendering.
    """
    positives = [c for c in by_group('openai_submission') if not c.expects_none]
    negatives = [c for c in by_group('openai_submission') if c.expects_none]
    lines = ['# Lenz — OpenAI app test cases', '', '## Positive cases', '']
    for i, case in enumerate(positives, 1):
        tools = case.tools_line or ', '.join(f'`{name}`' for name in case.expected_tools)
        label = 'Tools triggered' if len(case.expected_tools) > 1 else 'Tool triggered'
        lines += [
            f'### {i}. {case.scenario}',
            f'- **User prompt:** `{case.prompt}`',
            f'- **{label}:** {tools}',
            f'- **Expected output:** {case.expected_output}',
            '',
        ]
    lines += ['## Negative cases (Lenz should NOT be called)', '']
    for i, case in enumerate(negatives, 1):
        lines += [
            f'### N{i}. {case.scenario}',
            f'- **User prompt:** `{case.prompt}`',
            f'- **Expected:** {case.expected_output}',
            '',
        ]
    return '\n'.join(lines)
