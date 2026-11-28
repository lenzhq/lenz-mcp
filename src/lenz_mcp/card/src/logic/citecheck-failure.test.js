import { test } from 'node:test';
import assert from 'node:assert/strict';

import * as copy from '../copy.js';
import { citecheckFailure } from './citecheck-failure.js';

// A failed check as server.py's `_finished_citecheck` hands it to the card.
const failed = (fields) => ({
  status: 'failed',
  citecheck_id: 'c1',
  outcome: 'incomplete',
  summary: {},
  credits_charged: 0,
  message: 'A sentence for the model, never shown.',
  next_step: 'An instruction for the model, never shown.',
  citation_issues: [],
  citations: [],
  source: 'Lenz citation check',
  ...fields,
});

test('no citations in the text: nothing to check, not a failure on our side', () => {
  const frame = citecheckFailure(failed({ failure_reason: 'no_citations', failure_class: 'invalid_input', retryable: false }));
  assert.deepEqual(frame, {
    heading: copy.CITECHECK_NO_CITATIONS_HEADING,
    lines: [copy.CITECHECK_WHAT_COUNTS_UNCHARGED],
  });
  assert.equal(copy.CITECHECK_NO_CITATIONS_HEADING, 'No citations found in this text');
  assert.equal(copy.CITECHECK_WHAT_COUNTS_UNCHARGED, 'Lenz can check links, DOIs and numbered references. Nothing was charged.');
  assert.equal(copy.CITECHECK_WHAT_COUNTS, 'Lenz can check links, DOIs and numbered references.');
  // No charge stated where the record does not say 0 (a refused start has none).
  for (const payload of [
    failed({ failure_reason: 'no_citations', credits_charged: undefined }),
    { status: 'invalid_request', code: 'no_citations', source: 'Lenz citation check' },
  ]) {
    assert.deepEqual(citecheckFailure(payload), { heading: copy.CITECHECK_NO_CITATIONS_HEADING, lines: [copy.CITECHECK_WHAT_COUNTS] });
  }
});

test('no citations, in the older spellings and field names', () => {
  for (const payload of [
    failed({ failure_reason: 'no_checkable_citations' }),
    failed({ failure: { code: 'no_citations' } }),
    failed({ error_code: 'no_checkable_citations' }),
    { status: 'invalid_request', code: 'no_citations', source: 'Lenz citation check' },
  ]) {
    assert.equal(citecheckFailure(payload).heading, copy.CITECHECK_NO_CITATIONS_HEADING, JSON.stringify(payload));
  }
});

test('a service was down: try again in a few minutes, nothing charged only when the record says 0', () => {
  const frame = citecheckFailure(
    failed({ failure_reason: 'upstream_unavailable', failure_class: 'upstream_unavailable', retryable: true }),
  );
  assert.deepEqual(frame, { heading: copy.CITECHECK_RETRY_HEADING, lines: [copy.CITECHECK_RETRY_LATER_UNCHARGED] });
  assert.equal(copy.CITECHECK_RETRY_HEADING, 'Lenz could not check these citations');
  assert.equal(copy.CITECHECK_RETRY_LATER_UNCHARGED, 'Nothing was charged. Try again in a few minutes.');
  assert.equal(copy.CITECHECK_RETRY_LATER, 'Try again in a few minutes.');
  // Retryable alone (an older code), or the older field names, read the same.
  assert.deepEqual(citecheckFailure(failed({ failure_reason: 'timeout', retryable: true })), frame);
  assert.deepEqual(citecheckFailure(failed({ failure: { failure_class: 'upstream_unavailable' } })), frame);
  for (const charged of [2, undefined, null]) {
    assert.deepEqual(
      citecheckFailure(failed({ failure_reason: 'upstream_unavailable', retryable: true, credits_charged: charged })),
      { heading: copy.CITECHECK_RETRY_HEADING, lines: [copy.CITECHECK_RETRY_LATER] },
    );
  }
});

test('the input could not be checked as sent', () => {
  assert.deepEqual(citecheckFailure(failed({ failure_reason: 'page_unreadable', failure_class: 'invalid_input', retryable: false })), {
    heading: copy.CITECHECK_INVALID_HEADING,
    lines: [copy.CITECHECK_SEND_FULL_TEXT, copy.NOTHING_CHARGED],
  });
  // Refused at the start (a 422): no check exists, so no charge line and no replay line.
  assert.deepEqual(citecheckFailure({ status: 'invalid_request', code: 'url_input', message: 'x', source: 'Lenz citation check' }), {
    heading: copy.CITECHECK_INVALID_HEADING,
    lines: [copy.CITECHECK_SEND_FULL_TEXT],
  });
});

test('a check that cannot be found, or was removed', () => {
  assert.deepEqual(citecheckFailure({ status: 'not_found', message: 'x', citation_check_id: 'c1' }), {
    heading: copy.CITECHECK_UNREADABLE_HEADING,
    lines: [copy.CITECHECK_ASK_AGAIN],
  });
  assert.deepEqual(citecheckFailure({ status: 'not_found', gone: true, message: 'x', citation_check_id: 'c1' }), {
    heading: copy.CITECHECK_GONE_HEADING,
    lines: [copy.CITECHECK_GONE_BODY, copy.CITECHECK_ASK_AGAIN],
  });
});

test('a read that failed says nothing about the check or its charge', () => {
  assert.deepEqual(citecheckFailure({ status: 'error', message: 'x', citation_check_id: 'c1', credits_charged: 0 }), {
    heading: copy.CITECHECK_UNREADABLE_HEADING,
    lines: [copy.CITECHECK_ASK_AGAIN],
  });
});

test('anything else: our side, and the same text replays it', () => {
  const frame = citecheckFailure(failed({ failure_reason: 'internal', failure_class: 'internal', retryable: false }));
  assert.deepEqual(frame, {
    heading: copy.FAILED_HEADING,
    lines: [copy.CITECHECK_OUR_SIDE, copy.NOTHING_CHARGED, copy.CITECHECK_SAME_RESULT],
  });
  assert.deepEqual(citecheckFailure(failed({})), frame);
});

test('"Nothing was charged" only where the check itself says it charged nothing', () => {
  for (const charged of [1, 3, null, undefined, '0', -0.5]) {
    const frame = citecheckFailure(failed({ failure_reason: 'internal', credits_charged: charged }));
    assert.ok(!frame.lines.includes(copy.NOTHING_CHARGED), String(charged));
  }
});

test('never asks for the same input to run again, and never shows the server text', () => {
  const payloads = [
    failed({ failure_reason: 'no_citations' }),
    failed({ failure_reason: 'upstream_unavailable', retryable: true }),
    failed({ failure_reason: 'page_unreadable', failure_class: 'invalid_input' }),
    failed({ failure_reason: 'internal' }),
    { status: 'invalid_request', code: 'url_input', message: 'x' },
    { status: 'not_found', message: 'x' },
    { status: 'not_found', gone: true, message: 'x' },
    { status: 'error', message: 'x' },
  ];
  for (const p of payloads) {
    const { heading, lines } = citecheckFailure(p);
    for (const s of [heading, ...lines]) {
      assert.notEqual(s, copy.ASK_AGAIN);
      assert.notEqual(s, copy.FAILED_DEFAULT);
      assert.ok(!s.includes('never shown') && s !== 'x', s);
    }
  }
});
