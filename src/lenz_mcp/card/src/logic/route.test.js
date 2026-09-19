import { test } from 'node:test';
import assert from 'node:assert/strict';

import { buttonStrength, changedFrom, deepResult, modelRanIt, routePayload, routeQuick, tally } from './route.js';
import * as copy from '../copy.js';
import { confidenceBucket, domainOf, formatElapsed, isWebUrl, stageLabel, text, verdictKey } from './format.js';

test('a single quick-check row', () => {
  const out = routeQuick({
    status: 'ok',
    claims: [
      {
        claim: ' X ',
        verdict: 'False',
        confidence: 'low',
        rationale: 'Because.',
        dissent: 'Not so.',
        recommend_verify: true,
        next_step: 'Recommend a deep check.',
      },
    ],
  });
  assert.equal(out.view, 'single');
  assert.deepEqual(out.row, {
    claim: 'X',
    verdict: 'False',
    confidence: 'low',
    rationale: 'Because.',
    dissent: 'Not so.',
    recommend: true,
    error: '',
    hint: '',
  });
});

test('lists, errors, nothing to check and envelopes route to their views', () => {
  assert.equal(routeQuick(undefined).view, 'waiting');
  assert.equal(routeQuick({ status: 'ok', claims: [{ claim: 'A' }, { claim: 'B' }] }).view, 'list');
  assert.equal(routeQuick({ status: 'ok', claims: [{ claim: 'A', verdict: 'Error', error: 'no_claim' }] }).view, 'row-error');
  assert.deepEqual(routeQuick({ status: 'no_claim', message: 'An opinion.' }), { view: 'nothing', message: 'An opinion.' });
  assert.equal(routeQuick({ status: 'auth_required' }).view, 'reconnect');
  // Each of these has its own frame; see the failure test below.
  for (const status of ['error', 'forbidden', 'whatever']) {
    assert.equal(routeQuick({ status }).view, 'did-not-finish', status);
  }
});

test('non-string payload values never reach the page as anything but text', () => {
  const out = routeQuick({ status: 'ok', claims: [{ claim: 'X', verdict: { toString: () => '<b>' }, rationale: 42 }] });
  assert.equal(out.row.verdict, '');
  assert.equal(out.row.rationale, '');
  assert.equal(text(['a']), '');
});

test('button strength follows the recommendation, confidence and dissent', () => {
  const row = (o) => ({ confidence: 'high', dissent: '', recommend: false, ...o });
  assert.equal(buttonStrength(row({ recommend: true })), 'filled');
  assert.equal(buttonStrength(row({ confidence: 'low' })), 'filled');
  assert.equal(buttonStrength(row({ confidence: 'medium' })), 'outlined');
  assert.equal(buttonStrength(row({ dissent: 'No.' })), 'outlined');
  assert.equal(buttonStrength(row({})), 'quiet');
});

test('a deep result keeps only safe shapes', () => {
  const out = deepResult({
    verification_id: 'abcd1234',
    claim: 'X',
    verdict: 'False',
    lenz_score: 11,
    confidence: 'weird',
    warnings: ['a', 3, ' b '],
    sources: [{ title: 'T', url: 'javascript:alert(1)' }, null, { url: 'https://ok.example/' }, {}],
    sources_total: 1,
  });
  assert.equal(out.score, null);
  assert.equal(out.confidence, 'medium');
  assert.deepEqual(out.warnings, ['a', 'b']);
  assert.equal(out.sources.length, 2);
  assert.equal(out.sourcesTotal, 2);
  assert.equal(deepResult({ verification_id: '<script>' }).verificationId, '');
});

test('the changed line appears only when the verdict changed', () => {
  assert.equal(changedFrom('Mostly False', 'False'), 'Mostly False');
  assert.equal(changedFrom('False', 'false'), '');
  assert.equal(changedFrom('', 'False'), '');
});

test('format helpers', () => {
  assert.equal(verdictKey('Mostly True'), 'mostly-true');
  assert.equal(verdictKey('Error'), 'unknown');
  assert.equal(confidenceBucket('HIGH'), 'high');
  assert.equal(stageLabel('research'), 'Finding sources');
  assert.equal(stageLabel('starting'), null);
  assert.equal(stageLabel('toString'), null);
  assert.equal(formatElapsed(75), '1:15');
  assert.equal(isWebUrl('https://a.example/x'), true);
  for (const bad of ['javascript:alert(1)', 'data:text/html,x', '//a.example', 'https://', 'ftp://a', 42]) {
    assert.equal(isWebUrl(bad), false, String(bad));
  }
  assert.equal(domainOf('https://www.bls.gov/x'), 'bls.gov');
});

test('the tally counts each row by its current verdict, and error rows apart', () => {
  const rows = [
    { claim: 'a', verdict: 'False' },
    { claim: 'b', verdict: 'Mostly False' },
    { claim: 'c', verdict: 'Mixed' },
    { claim: 'd', verdict: 'True' },
    { claim: 'e', verdict: 'Mostly True' },
    { claim: 'f', verdict: 'Error', error: 'upstream_unavailable' },
    { claim: 'g', verdict: '' },
  ];
  assert.deepEqual(tally(rows), { wrong: 2, mixed: 1, hold: 2, error: 2 });
  assert.equal(copy.tallyLine(tally(rows)), '2 look wrong · 1 mixed · 2 hold up · 2 could not be checked');
  assert.equal(copy.tallyLine(tally([{ claim: 'a', verdict: 'False' }])), '1 looks wrong');
  assert.equal(copy.tallyLine(tally([{ claim: 'a', verdict: 'True' }])), '1 holds up');
  assert.equal(copy.tallyLine({ wrong: 0, mixed: 0, hold: 3, error: 0 }), '3 hold up');
});

test('a deep check the model ran routes to its own card, whatever state it is in', () => {
  // The quick shapes still route as before.
  assert.equal(routePayload({ status: 'ok', claims: [{ claim: 'a', verdict: 'True' }] }).view, 'single');
  assert.equal(routePayload({ status: 'ok', claims: [{ claim: 'a' }, { claim: 'b' }] }).view, 'list');
  assert.equal(routePayload({ status: 'no_claim', message: 'x' }).view, 'nothing');
  assert.equal(routePayload(undefined).view, 'waiting');
  assert.equal(routePayload({ status: 'auth_required' }).view, 'reconnect');

  // Completed: the card shows the result the model already has.
  const done = routePayload({ status: 'completed', verification_id: 'abcd1234', claim: 'c', verdict: 'False', lenz_score: 2, confidence: 'high' });
  assert.equal(done.view, 'deep');
  assert.equal(done.result.verificationId, 'abcd1234');

  // Submitted: the card adopts the run and polls it.
  const running = routePayload({ status: 'submitted', task_id: 'a'.repeat(32), claim: 'The claim.', step: 'research', index: 2, total: 5, elapsed_seconds: 20 });
  assert.deepEqual(running, { view: 'deep-running', taskId: 'a'.repeat(32), claim: 'The claim.', stage: 'Finding sources', index: 2, total: 5, elapsed: 20 });
  // A task id that could not be sent is not a run.
  assert.equal(routePayload({ status: 'submitted', task_id: '' }).view, 'did-not-finish');
  assert.equal(routePayload({ status: 'submitted', task_id: 'no spaces allowed' }).view, 'did-not-finish');

  // needs_input is the picker.
  const picker = routePayload({ status: 'needs_input', reason: 'multi_claim', task_id: 't1', message: 'Which one?', claims: [{ text: 'a' }, { text: 'b' }] });
  assert.equal(picker.view, 'picker');
  assert.deepEqual(picker.claims, ['a', 'b']);

  // A deep tool can fail the whole call too, and lands on the same frames as a
  // quick one: `failed` is not a shape the card can read, an outage is.
  assert.equal(routePayload({ status: 'failed', message: 'x' }).view, 'did-not-finish');
  assert.equal(routePayload({ status: 'service_unavailable' }).view, 'outage');
});

// Two words for one situation: verify_claim says `submitted`, get_verification
// says `processing` (server.py). Reading only one showed a paid, running check
// as "did not finish" and never polled it.
test('a running deep check is adopted whichever word the server used for it', () => {
  for (const status of ['submitted', 'processing']) {
    const route = routePayload({ status, task_id: 'abc-123', step: 'research', index: 2, total: 5 });
    assert.equal(route.view, 'deep-running', status);
    assert.equal(route.taskId, 'abc-123');
    assert.equal(route.stage, 'Finding sources');
  }
});

// Every field is attacker-controlled, and String({toString: null}) THROWS.
test('a payload field that cannot be coerced to a string does not throw', () => {
  const hostile = { toString: null };
  assert.equal(routePayload({ status: 'submitted', task_id: 'ok', step: hostile }).stage, null);
  assert.equal(routePayload({ status: 'completed', confidence: hostile, verdict: 'True' }).result.confidence, 'medium');
  assert.equal(routePayload({ status: 'submitted', task_id: hostile }).view, 'did-not-finish');
});

// A card the model ran stores nothing, and the probe that asks whether storage
// works is itself a write.
test('a model-run payload is known as one, so no store is ever opened for it', () => {
  for (const status of ['completed', 'submitted', 'processing', 'needs_input']) {
    assert.equal(modelRanIt({ status }), true, status);
  }
  for (const payload of [null, undefined, 'x', {}, { status: 'quick' }, { status: 'error' }]) {
    assert.equal(modelRanIt(payload), false);
  }
});

// Seven whole-call envelopes, each on its own frame rather than one generic
// "did not finish", including two that clear on their own after a stated wait.
test('each whole-call failure routes to the frame that answers it', () => {
  assert.equal(routeQuick({ status: 'service_unavailable', retry_after_seconds: 90 }).view, 'outage');
  assert.equal(routeQuick({ status: 'service_unavailable', retry_after_seconds: 90 }).retryAfter, 90);
  assert.equal(routeQuick({ status: 'rate_limited', retry_after_seconds: 3600 }).retryAfter, 3600);
  // A wait we cannot trust is no wait: the frame falls back to its own words.
  for (const bad of [null, undefined, -5, 0, '90', 1.5, { toString: null }]) {
    assert.equal(routeQuick({ status: 'rate_limited', retry_after_seconds: bad }).retryAfter, 0);
  }
  assert.equal(routeQuick({ status: 'quota_exhausted' }).view, 'quota');
  assert.equal(routeQuick({ status: 'in_progress' }).view, 'in-progress');
  assert.equal(routeQuick({ status: 'already_resolved' }).view, 'already-resolved');
  // Nothing actionable stays on the one honest frame.
  for (const status of ['forbidden', 'invalid_request', 'error', 'who-knows']) {
    assert.equal(routeQuick({ status }).view, 'did-not-finish', status);
  }
});

test('the wait reads in words, and never says "shortly"', () => {
  assert.equal(copy.retryIn(0), 'Try again in a minute.');
  assert.equal(copy.retryIn(45), 'Try again in about a minute.');
  assert.equal(copy.retryIn(120), 'Try again in about 2 minutes.');
  assert.equal(copy.retryIn(3600), 'Try again in about an hour.');
  assert.equal(copy.retryIn(7200), 'Try again in about 2 hours.');
});

// needs_input shows the picker, not an interim frame.
test('needs_input is the picker, over the full option list in the caller order', () => {
  const route = routePayload({
    status: 'needs_input',
    task_id: 'parent-1',
    claims: [{ text: 'One.' }, { text: 'Two.' }, 'Three.', { claim: 'Four.' }, { text: '' }, null],
    message: 'Show the user this list and call select_claims',
    resolve_with: 'Call select_claims',
  });
  assert.equal(route.view, 'picker');
  assert.equal(route.taskId, 'parent-1');
  assert.deepEqual(route.claims, ['One.', 'Two.', 'Three.', 'Four.']);
  // The model's instructions are not the card's content.
  assert.equal(route.message, undefined);
  assert.equal(route.resolveWith, undefined);
});

test('a picker with nothing to send back is not a picker', () => {
  // No parent id, or an id we could not send: there is nothing to resolve.
  assert.equal(routePayload({ status: 'needs_input', claims: [{ text: 'One.' }] }).view, 'did-not-finish');
  assert.equal(routePayload({ status: 'needs_input', task_id: 'a b', claims: [{ text: 'One.' }] }).view, 'did-not-finish');
  assert.equal(routePayload({ status: 'needs_input', task_id: 'ok', claims: [] }).view, 'did-not-finish');
});

test('a heading for one check does not say "1 claims"', () => {
  // The list card is always two or more; the picker can start exactly one.
  assert.equal(copy.listHeading(1), '1 claim checked');
  assert.equal(copy.listHeading(5), '5 claims checked');
  assert.equal(copy.pickerRunningHeading(1), 'Checking 1 claim');
  assert.equal(copy.pickerRunningHeading(5), 'Checking 5 claims');
  assert.equal(copy.pickerButton(1), 'Check 1 claim');
  assert.equal(copy.pickerButton(3), 'Check 3 claims');
});
