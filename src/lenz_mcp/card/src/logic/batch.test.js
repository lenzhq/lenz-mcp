import { test } from 'node:test';
import assert from 'node:assert/strict';

import { batchEndMessage, batchProgress, batchRows, isStopped, recordRun, restoreBatch, withoutRow } from './batch.js';
import { tally } from './route.js';
import * as copy from '../copy.js';

test('a stopped check stays counted when its row remounts and reports idle, then running', () => {
  let runs = {};
  runs = recordRun(runs, 4, { kind: 'running', elapsed: 20 });
  runs = recordRun(runs, 5, { kind: 'running', elapsed: 20 });
  runs = recordRun(runs, 4, { kind: 'completed', elapsed: 0 });
  assert.deepEqual(batchProgress([4, 5], runs), { total: 2, done: 1, running: true, elapsed: 20 });
  // Row 4 changes group: a fresh mount reports idle, then its recovery runs.
  runs = recordRun(runs, 4, { kind: 'idle', elapsed: 0 });
  runs = recordRun(runs, 4, { kind: 'running', elapsed: 0 });
  runs = recordRun(runs, 4, { kind: 'submitting', elapsed: 0 });
  assert.equal(batchProgress([4, 5], runs).done, 1);
  runs = recordRun(runs, 5, { kind: 'failed', elapsed: 0 });
  assert.deepEqual(batchProgress([4, 5], runs), { total: 2, done: 2, running: false, elapsed: 0 });
  runs = recordRun(runs, 5, { kind: 'idle', elapsed: 0 });
  assert.equal(batchProgress([4, 5], runs).done, 2);
});

test('"Try again" on a row that did not finish is a new attempt: its check counts as running again', () => {
  let runs = recordRun({}, 4, { kind: 'completed', attempt: 0 });
  runs = recordRun(runs, 5, { kind: 'failed', attempt: 0 });
  assert.equal(batchProgress([4, 5], runs).running, false);
  runs = recordRun(runs, 5, { kind: 'submitting', attempt: 1 });
  assert.deepEqual(batchProgress([4, 5], runs), { total: 2, done: 1, running: true, elapsed: 0 });
  runs = recordRun(runs, 5, { kind: 'running', elapsed: 12, attempt: 1 });
  assert.equal(batchProgress([4, 5], runs).done, 1);
  runs = recordRun(runs, 5, { kind: 'completed', attempt: 1 });
  assert.equal(batchProgress([4, 5], runs).running, false);
  // The same attempt, or an older one, reporting again changes nothing.
  assert.equal(recordRun(runs, 5, { kind: 'idle', attempt: 0 }), runs);
  assert.equal(recordRun(runs, 5, { kind: 'running', attempt: 1 }), runs);
});

test('the count never goes backwards over any sequence of reports from one attempt', () => {
  const kinds = ['idle', 'submitting', 'running', 'completed', 'failed', 'quota', 'unavailable', 'unrecoverable'];
  let seed = 7;
  const next = () => (seed = (seed * 48271) % 2147483647);
  let runs = {};
  let last = 0;
  for (let step = 0; step < 2000; step += 1) {
    runs = recordRun(runs, next() % 3, { kind: kinds[next() % kinds.length], elapsed: next() % 300 });
    const { done } = batchProgress([0, 1, 2], runs);
    assert.ok(done >= last, `went from ${last} to ${done}`);
    last = done;
  }
});

test('an unchanged report keeps the same object, so the list does not re-render', () => {
  const runs = recordRun({}, 1, { kind: 'running', elapsed: 9 });
  assert.equal(recordRun(runs, 1, { kind: 'running', elapsed: 9 }), runs);
  assert.equal(recordRun(runs, 1, null), runs);
});

test('the longest elapsed time decides "taking longer than usual"', () => {
  const runs = { 2: { kind: 'running', elapsed: 196 }, 3: { kind: 'running', elapsed: 204 } };
  assert.equal(batchProgress([2, 3], runs).elapsed, 204);
  assert.deepEqual(batchProgress(null, runs), { total: 0, done: 0, running: false, elapsed: 0 });
  assert.equal(isStopped({ kind: 'running' }), false);
  assert.equal(isStopped(undefined), false);
});

test('the summary counts verdicts in the tally\'s words, and a stopped check as could not be checked', () => {
  const runs = { 4: { kind: 'completed' }, 5: { kind: 'completed' }, 6: { kind: 'failed' } };
  const verdicts = { 4: 'True', 5: 'False' };
  const counts = tally(batchRows([4, 5, 6], runs, (i) => verdicts[i]));
  assert.equal(copy.batchDone(counts), 'Checked against sources · 1 looks wrong · 1 holds up · 1 could not be checked');
  // A completed row whose verdict has not reached the list yet is not a verdict.
  assert.deepEqual(batchRows([4], { 4: { kind: 'completed' } }, () => ''), [{ error: 'stopped' }]);
});

test('a reopened batch lets go of a row that found nothing to recover', () => {
  assert.deepEqual(withoutRow([4, 5], 4), [5]);
  assert.equal(withoutRow([5], 5), null);
  const batch = [4, 5];
  assert.equal(withoutRow(batch, 2), batch, 'a row outside the batch changes nothing');
  assert.equal(withoutRow(null, 4), null);
  // With the row gone, the rest can finish.
  assert.equal(batchProgress(withoutRow([4, 5], 4), { 5: { kind: 'completed' } }).running, false);
});

test('the summary joins the result of the batch row that ended the batch, never another row\'s message', () => {
  const summary = 'Checked against sources · 1 looks wrong · 1 holds up';
  // Row 2 is outside the batch and spoke last; row 5 stopped last of the batch.
  const said = { 4: 'Claim 5: Result: True, 9 out of 10, confidence high', 5: 'Claim 6: Result: False, 2 out of 10, confidence high', 2: 'Claim 3: Finding sources' };
  assert.equal(batchEndMessage([4, 5], said, { 4: 1, 5: 2, 2: 3 }, summary), `Claim 6: Result: False, 2 out of 10, confidence high. ${summary}`);
  assert.equal(batchEndMessage([4, 5], said, { 4: 3, 5: 2 }, summary), `Claim 5: Result: True, 9 out of 10, confidence high. ${summary}`);
  // Nothing said by the ending row: the summary alone.
  assert.equal(batchEndMessage([4, 5], {}, { 4: 1, 5: 2 }, summary), summary);
  assert.equal(batchEndMessage([4, 5], said, {}, summary), summary);
});

test('a saved batch keeps only rows that exist and still have a check of their own', () => {
  const has = (i) => i !== 3;
  assert.deepEqual(restoreBatch([4, 5], 8, has), [4, 5]);
  assert.deepEqual(restoreBatch([3, 5], 8, has), [5]);
  assert.equal(restoreBatch([3], 8, has), null);
  assert.deepEqual(restoreBatch([5, 5, 9, -1, 1.5, '2', 2], 8, () => true), [5, 2]);
  assert.equal(restoreBatch('4,5', 8, has), null);
  assert.equal(restoreBatch(undefined, 8, has), null);
});

test('the status line and the summary, singular and plural', () => {
  assert.equal(copy.batchRunning(2, 0), 'Checking 2 claims against sources');
  assert.equal(copy.batchRunning(2, 1), 'Checking 2 claims against sources · 1 of 2 done');
  assert.equal(copy.batchRunning(5, 3), 'Checking 5 claims against sources · 3 of 5 done');
  // A reopened card can restore a batch of one, when the other row's record is gone.
  assert.equal(copy.batchRunning(1, 0), 'Checking 1 claim against sources');
  assert.equal(copy.batchDone({ wrong: 2, mixed: 0, hold: 0, norecord: 0, error: 0 }), 'Checked against sources · 2 look wrong');
  assert.equal(copy.batchDone({ wrong: 0, mixed: 0, hold: 1, norecord: 1, error: 0 }), 'Checked against sources · 1 holds up · 1 has no public record');
  assert.equal(copy.quickCheckVerdict('Mostly False'), 'Quick check: Mostly False');
});
