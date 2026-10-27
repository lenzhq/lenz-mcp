"""What the connector's tools really answer, for the eval's conversation histories.

A model decides what to do next from the RESULT of the last tool as much as from
the instructions: the escalation rules ("recommend a deep check on low
confidence, never start one without a yes", "never deep-check every row") are
repeated inside the results themselves (`next_step`, `confidence_note`, the
`message` of a `needs_input`). A history that stands in assistant PROSE for a
tool result leaves all of that text out of context, so the eval would be
measuring a conversation the connector never produces.

So each result here comes out of the connector's OWN tool code (`assess_claim`,
`verify_claim`, ...) with only the Lenz API stubbed, the same technique the
card's fixtures use (`src/lenz_mcp/card/fixtures/build_fixtures.py`). The API
bodies fed in are invented, in the API's exact shape; a change to a note or to
the mapping changes the history the next run sees, and the freshness key covers
the built histories (`freshness.harness_hash`), so a result measured before the
change is stale.

Nothing here makes a network call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest import mock

from evals.tool_choice import _path  # noqa: F401  (puts src/ on sys.path)

PROD_FRONTEND = 'https://lenz.io'


class _Ctx:
    request_context = None


def _stub_for(response: Any):
    async def _stub(*args: Any, **kwargs: Any) -> Any:
        return response

    return _stub


def _run(fn_name: str, args: tuple, stubs: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Call the connector's real tool function with the API stubbed."""
    from lenz_mcp import client, config, server
    from lenz_mcp.client import ApiResponse

    async def _go() -> dict[str, Any]:
        patches = [
            mock.patch.object(server, '_authorization', lambda ctx: 'Bearer lenz_eval'),
            # The card is off in the eval's manifest, so no result carries a card note.
            mock.patch.object(config, 'CARD_ENABLED', False),
            mock.patch.object(config, 'FRONTEND_URL', PROD_FRONTEND),
        ]
        for name, (status, data) in stubs.items():
            patches.append(mock.patch.object(client, name, _stub_for(ApiResponse(status=status, data=data))))
        for patch in patches:
            patch.start()
        try:
            return await getattr(server, fn_name)(*args, _Ctx(), **kwargs)
        finally:
            for patch in reversed(patches):
                patch.stop()

    # The connector logs an ERROR when no request is bound (no client profile); here
    # there is no request on purpose, and the line would print on every import.
    logging.disable(logging.CRITICAL)
    try:
        result = asyncio.run(_go())
    finally:
        logging.disable(logging.NOTSET)
    assert isinstance(result, dict), fn_name
    return result


def assess_row(
    claim: str, verdict: str, confidence: str, rationale: str = '', dissent: str = '', rewrite: str = ''
) -> dict[str, Any]:
    """One row of a `POST /assess` body, in the API's shape."""
    return {
        'hint': None,
        'claim': claim,
        'dissent': dissent or None,
        'verdict': verdict,
        'language': 'en',
        'rationale': rationale or None,
        'confidence': confidence,
        'failure_code': None,
        'verification_url': None,
        'identified_claims': [],
        'suggested_rewrite': rewrite or None,
    }


def assess_result(rows: list[dict[str, Any]], more: list[str] = ()) -> dict[str, Any]:
    """What `assess_claim` returns for these API rows (notes, next_step and all).
    `more` are the claims the text held past one quick check, unchecked."""
    body = {'failure': None, 'claims': rows, 'more_claims': list(more)}
    return _run('assess_claim', ('x',), {'assess': (200, body)})


def verification_running_result(arguments: dict[str, Any]) -> dict[str, Any]:
    """What `get_verification` returns for a check the eval answers: still running."""
    from lenz_mcp import config

    task_id = str(arguments.get('task_id') or '')
    with mock.patch.object(config, 'verify_wait_seconds', lambda identity: 0):
        return _run(
            'get_verification',
            (task_id,),
            {
                'verify_status': (
                    200,
                    {
                        'status': 'processing',
                        'progress': {'step': 'research', 'index': 2, 'total': 5, 'elapsed_seconds': 41},
                    },
                )
            },
        )


def list_recent_result(arguments: dict[str, Any]) -> dict[str, Any]:
    """What `list_verifications` returns while a batch is still running: none finished yet."""
    return _run(
        'list_verifications', (), {'list_verifications': (200, {'items': [], 'total': 0, 'page': 1, 'page_size': 10})}
    )


def quota_exhausted_result(arguments: dict[str, Any]) -> dict[str, Any]:
    """What `verify_claim` returns once the balance no longer covers a deep check."""
    return _run(
        'verify_claim', (str(arguments.get('claim') or 'x'),), {'verify': (402, {'cost': 10, 'credits_remaining': 0})}
    )


def balance_limited(started: int):
    """`verify_claim` answers for a balance that covers `started` deep checks:
    the first ones start, every later one is refused for credits, as the API
    does. One closure per attempt, so each attempt starts from a full balance."""

    def factory():
        count = {'n': 0}

        def answer(arguments: dict[str, Any]) -> dict[str, Any]:
            count['n'] += 1
            if count['n'] <= started:
                return verify_started_result(arguments)
            return quota_exhausted_result(arguments)

        return answer

    factory.per_attempt = True
    return factory


# The claim behind each task_id `verify_started_result` handed out, so a later
# `get_verification` on it can return that claim's finished check.
_STARTED: dict[str, str] = {}


def verification_finished_result(arguments: dict[str, Any]) -> dict[str, Any]:
    """What `get_verification` returns for a check the eval started: finished.

    A real wait outlasts most deep checks, so the first poll usually returns
    the result; an eval that answered "still running" forever would only show
    a model waiting on checks that never end.
    """
    task_id = str(arguments.get('task_id') or '')
    claim = _STARTED.get(task_id, 'The claim.')
    body = verification_body(
        verification_id=task_id[:8] or 'a0a0a0a0',
        claim=claim,
        verdict='True',
        score=9,
        confidence='high',
        key_finding='The public record confirms it.',
        summary='Independent sources confirm the claim.',
        rewrite=None,
        sources=[('registry.example.org', 'Company register', 'The register confirms the claim.')],
    )
    return _run('get_verification', (task_id,), {'verify_status': (200, {'status': 'completed', 'result': body})})


def verify_started_result(arguments: dict[str, Any]) -> dict[str, Any]:
    """What `verify_claim` returns for a call the eval answers: the check has
    started and is still running, with a task_id of its own."""
    import hashlib

    claim = str(arguments.get('claim') or '')
    task_id = hashlib.sha256(claim.encode()).hexdigest()[:32]
    _STARTED[task_id] = claim
    return verify_running_result(claim, task_id)


def verification_body(
    *,
    verification_id: str,
    claim: str,
    verdict: str,
    score: int,
    confidence: str,
    key_finding: str,
    summary: str,
    rewrite: str | None,
    sources: list[tuple[str, str, str]],
) -> dict[str, Any]:
    """A completed verification, in the API's shape. `sources` is (name, title, snippet)."""
    return {
        'verification_id': verification_id,
        'claim': claim,
        'visibility': 'private',
        'depth': 'standard',
        'verdict': verdict,
        'confidence': confidence,
        'lenz_score': score,
        'key_finding': key_finding,
        'executive_summary': summary,
        'suggested_rewrite': rewrite,
        'warnings': [],
        'sources': [
            {
                'source_name': name,
                'title': title,
                'url': f'https://{name}/{i}',
                'snippet': snippet,
                'date': '2024-05-01',
            }
            for i, (name, title, snippet) in enumerate(sources, 1)
        ],
    }


def verify_completed_result(claim: str, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """What `verify_claim` returns when the check finishes inside the call."""
    return _run(
        'verify_claim',
        (claim,),
        {'verify': (202, {'task_id': task_id}), 'verify_status': (200, {'status': 'completed', 'result': body})},
    )


def verify_running_result(claim: str, task_id: str) -> dict[str, Any]:
    """What `verify_claim` returns when the check outlasts the call's wait.

    The wait budget is zeroed for the stub: the first poll still runs and reads
    `processing`, which is exactly the real ceiling case.
    """
    from lenz_mcp import config

    with mock.patch.object(config, 'verify_wait_seconds', lambda identity: 0):
        return _run(
            'verify_claim',
            (claim,),
            {
                'verify': (202, {'task_id': task_id}),
                'verify_status': (
                    200,
                    {
                        'status': 'processing',
                        'progress': {'step': 'research', 'index': 2, 'total': 5, 'elapsed_seconds': 41},
                    },
                ),
            },
        )


def verify_needs_input_result(claim: str, task_id: str, offered: list[str]) -> dict[str, Any]:
    """What `verify_claim` returns for a text that holds several claims."""
    return _run(
        'verify_claim',
        (claim,),
        {
            'verify': (202, {'task_id': task_id}),
            'verify_status': (
                200,
                {'status': 'needs_input', 'reason': 'multi_claim', 'claims': [{'claim': text} for text in offered]},
            ),
        },
    )


def citecheck_running_result(text: str, citecheck_id: str) -> dict[str, Any]:
    """What `check_citations` returns when the check outlasts the call's wait."""
    from lenz_mcp import config

    with mock.patch.object(config, 'verify_wait_seconds', lambda identity: 0):
        return _run(
            'check_citations',
            (text,),
            {
                'citecheck': (202, {'citecheck_id': citecheck_id, 'status': 'queued'}),
                'citecheck_status': (
                    200,
                    {
                        'citecheck_id': citecheck_id,
                        'status': 'checking',
                        'poll_after_seconds': 10,
                        'summary': {
                            'citations_selected': 3,
                            'citation_checks': {'checked': 1, 'unchecked': 0, 'failed': 0},
                        },
                    },
                ),
            },
        )


def citecheck_row(index: int, url: str, statement: str, finding: str, snippet: str = '', rationale: str = '') -> dict:
    """One `citations[]` row of a finished citation check, in the API's shape."""
    supported = finding in ('supported', 'partly_supported', 'contradicted', 'not_in_source')
    return {
        'index': index,
        'reference': url,
        'cited_url': url,
        'doi': None,
        'statement': statement,
        'quotes': [],
        'position': None,
        'result': {
            'finding': finding,
            'source': 'support' if supported else None,
            'is_issue': finding in ('contradicted', 'not_in_source', 'page_not_found'),
        },
        'check': {
            'status': 'completed',
            'page_read': 'full',
            'support': finding if supported else 'unchecked',
            'snippet': snippet or None,
            'rationale': rationale or None,
            'unchecked_reason': None,
            'hint': None,
            'failure': None,
        },
    }


def citecheck_completed_result(
    text: str, citecheck_id: str, rows: list[dict[str, Any]], more: list[tuple[str, str]] = ()
) -> dict[str, Any]:
    """What `check_citations` returns for a finished check. `more` is (url, sentence) pairs left unchecked."""
    issues = [
        {
            'citation_index': r['index'],
            'reference': r['reference'],
            'cited_url': r['cited_url'],
            'doi': None,
            'statement': r['statement'],
            'quotes': [],
            'position': None,
            'finding': r['result']['finding'],
            'source': r['result']['source'],
            'snippet': r['check']['snippet'],
            'rationale': r['check']['rationale'],
            'failure': None,
        }
        for r in rows
        if r['result']['is_issue']
    ]
    body = {
        'citecheck_id': citecheck_id,
        'status': 'completed',
        'outcome': 'issues_found' if issues else 'clean',
        'summary': {
            'citations_found': len(rows) + len(more),
            'citations_selected': len(rows),
            'citation_limit': 20,
            'citation_limit_exceeded': bool(more),
            'citation_checks': {'checked': len(rows), 'unchecked': 0, 'failed': 0},
            'citation_issues': len(issues),
        },
        'credits': {'charged': len(rows)},
        'citations': rows,
        'citation_issues': issues,
        'citation_failures': [],
        'failure': None,
        'more_citations': [
            {'index': len(rows) + i, 'reference': url, 'cited_url': url, 'doi': None, 'sentence': sentence}
            for i, (url, sentence) in enumerate(more)
        ],
    }
    return _run(
        'check_citations',
        (text,),
        {
            'citecheck': (202, {'citecheck_id': citecheck_id, 'status': 'queued'}),
            'citecheck_status': (200, body),
        },
    )


def usage_result(credits_remaining: int) -> dict[str, Any]:
    """What `check_usage` returns for an account holding `credits_remaining` credits."""
    body = {
        'plan': 'pro',
        'credits': {'remaining': credits_remaining, 'total': credits_remaining, 'resets_at': '2026-11-01T00:00:00Z'},
        'costs': {'assess': 1, 'verify': 10, 'ask': 1},
    }
    return _run('check_usage', (), {'me_usage': (200, body)})
