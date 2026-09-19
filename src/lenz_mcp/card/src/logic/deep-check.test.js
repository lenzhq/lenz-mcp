import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createDeepCheck, POLL_MS, LONG_POLL_MS, MAX_POLL_ERRORS } from './deep-check.js';

function fakeClock() {
  let now = 0;
  let next = 1;
  const timers = new Map();
  return {
    setTimeout(fn, ms) {
      const id = next++;
      timers.set(id, { at: now + ms, fn });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    pending() {
      return [...timers.values()].map((t) => t.at - now).sort((a, b) => a - b);
    },
    async advance(ms) {
      now += ms;
      for (const [id, t] of [...timers]) {
        if (t.at <= now) {
          timers.delete(id);
          t.fn();
        }
      }
      await flush();
    },
  };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

// A host whose tool answers are scripted per tool name, recording every call.
function fakeHost(script) {
  const calls = [];
  return {
    calls,
    async callTool(name, args) {
      calls.push({ name, args });
      const queue = script[name] || [];
      const answer = queue.length > 1 ? queue.shift() : queue[0];
      if (answer instanceof Error) throw answer;
      return typeof answer === 'function' ? answer(args) : answer;
    },
  };
}

function memoryStore() {
  let rec = null;
  return {
    load: () => rec,
    save: (value) => {
      rec = { ...value };
    },
    clear: () => {
      rec = null;
    },
    get value() {
      return rec;
    },
  };
}

const processing = (step, index, elapsed = 10) => ({ status: 'processing', step, index, total: 5, elapsed_seconds: elapsed });
const completed = { status: 'completed', verification_id: 'abcd1234', verdict: 'False', claim: 'X' };

function make(script, extra = {}) {
  const clock = fakeClock();
  const host = fakeHost(script);
  const store = 'store' in extra ? extra.store : memoryStore();
  const states = [];
  const check = createDeepCheck({
    claim: 'The claim.',
    host,
    store,
    clock,
    onChange: (s) => states.push(s),
  });
  return { check, clock, host, store, states };
}

test('a press submits once, runs, polls and completes in place', async () => {
  const { check, clock, host, store } = make({
    start_verification_widget: [{ status: 'submitted', task_id: 't1' }],
    get_verification_widget: [processing('research', 2), processing('debate', 3), completed],
  });
  await check.start();
  assert.equal(check.state().kind, 'running');
  assert.equal(check.state().taskId, 't1');
  assert.deepEqual(store.value, { taskId: 't1' });
  assert.deepEqual(host.calls[0], {
    name: 'start_verification_widget',
    args: { claim: 'The claim.' },
  });

  await clock.advance(POLL_MS);
  assert.equal(check.state().stage, 'Finding sources');
  assert.equal(check.state().index, 2);
  await clock.advance(POLL_MS);
  assert.equal(check.state().stage, 'Weighing both sides');
  await clock.advance(POLL_MS);
  assert.equal(check.state().kind, 'completed');
  assert.equal(check.state().result.verification_id, 'abcd1234');
  assert.deepEqual(store.value, { taskId: 't1', verificationId: 'abcd1234' });
  assert.deepEqual(clock.pending(), []);
});

test('a double click never submits twice', async () => {
  const { check, host } = make({
    start_verification_widget: [{ status: 'submitted', task_id: 't1' }],
    get_verification_widget: [processing('research', 2)],
  });
  await Promise.all([check.start(), check.start(), check.start()]);
  await check.start();
  assert.equal(host.calls.filter((c) => c.name === 'start_verification_widget').length, 1);
});

test('an unknown step keeps the last known stage and never prints a raw name', async () => {
  const { check, clock } = make({
    start_verification_widget: [{ status: 'submitted', task_id: 't1' }],
    get_verification_widget: [processing('research', 2), processing('starting', 0), processing('Adjudicating', 4)],
  });
  await check.start();
  await clock.advance(POLL_MS);
  await clock.advance(POLL_MS);
  assert.equal(check.state().stage, 'Finding sources');
  await clock.advance(POLL_MS);
  assert.equal(check.state().stage, 'Finding sources');
});

test('polls slow down after three minutes', async () => {
  const { check, clock } = make({
    start_verification_widget: [{ status: 'submitted', task_id: 't1' }],
    get_verification_widget: [processing('adjudication', 4, 200)],
  });
  await check.start();
  await clock.advance(POLL_MS);
  assert.deepEqual(clock.pending(), [LONG_POLL_MS]);
});

test('poll errors back off, never resubmit, and stop at the cap as unavailable', async () => {
  const { check, clock, host } = make({
    start_verification_widget: [{ status: 'submitted', task_id: 't1' }],
    get_verification_widget: [new Error('bridge gone')],
  });
  await check.start();
  let waited = POLL_MS;
  for (let i = 0; i < MAX_POLL_ERRORS; i += 1) {
    await clock.advance(waited);
    const next = clock.pending()[0];
    if (i < MAX_POLL_ERRORS - 1) {
      assert.ok(next > waited || next === 30000, `backoff grows: ${next} after ${waited}`);
      waited = next;
    }
  }
  assert.equal(check.state().kind, 'unavailable');
  assert.deepEqual(clock.pending(), []);
  assert.equal(host.calls.filter((c) => c.name === 'start_verification_widget').length, 1);
});

test('an error envelope counts as a poll error, not a result', async () => {
  const { check, clock } = make({
    start_verification_widget: [{ status: 'submitted', task_id: 't1' }],
    get_verification_widget: [{ status: 'error', message: 'x' }, processing('research', 2)],
  });
  await check.start();
  await clock.advance(POLL_MS);
  assert.equal(check.state().kind, 'running');
  assert.equal(check.state().errors, 1);
  await clock.advance(clock.pending()[0]);
  assert.equal(check.state().errors, 0);
  assert.equal(check.state().stage, 'Finding sources');
});

test('polls never overlap', async () => {
  let release;
  const { check, clock, host } = make({
    start_verification_widget: [{ status: 'submitted', task_id: 't1' }],
    get_verification_widget: [() => new Promise((resolve) => (release = resolve))],
  });
  await check.start();
  await clock.advance(POLL_MS);
  await check.pollNow();
  await check.pollNow();
  assert.equal(host.calls.filter((c) => c.name === 'get_verification_widget').length, 1);
  release(processing('research', 2));
  await flush();
  assert.equal(check.state().stage, 'Finding sources');
});

test('a late answer for another task is dropped', async () => {
  let release;
  const { check, clock } = make({
    start_verification_widget: [{ status: 'submitted', task_id: 't1' }],
    get_verification_widget: [() => new Promise((resolve) => (release = resolve))],
  });
  await check.start();
  await clock.advance(POLL_MS);
  check.dispose();
  release(completed);
  await flush();
  assert.equal(check.state().kind, 'running');
});

test('a failed run: retryable offers a retry naming the failed task; not retryable does not', async () => {
  const { check, clock, host } = make({
    start_verification_widget: [
      { status: 'submitted', task_id: 't1' },
      { status: 'submitted', task_id: 't2' },
    ],
    get_verification_widget: [
      { status: 'failed', failure_class: 'upstream_unavailable', retryable: true },
      processing('research', 2),
    ],
  });
  await check.start();
  await clock.advance(POLL_MS);
  assert.deepEqual(
    { kind: check.state().kind, retryable: check.state().retryable, taskId: check.state().taskId },
    { kind: 'failed', retryable: true, taskId: 't1' },
  );
  await check.start();
  assert.deepEqual(host.calls.at(-1), {
    name: 'start_verification_widget',
    args: { claim: 'The claim.', retry_of: 't1' },
  });
  assert.equal(check.state().taskId, 't2');
});

test('a run that failed for good cannot be retried from the card', async () => {
  const { check, clock, host } = make({
    start_verification_widget: [{ status: 'submitted', task_id: 't1' }],
    get_verification_widget: [{ status: 'failed', failure_class: 'invalid_input', retryable: false }],
  });
  await check.start();
  await clock.advance(POLL_MS);
  assert.equal(check.state().failureClass, 'invalid_input');
  await check.start();
  assert.equal(host.calls.filter((c) => c.name === 'start_verification_widget').length, 1);
});

test('out of credits and short of credits are told apart', async () => {
  for (const [empty, expected] of [[true, true], [false, false]]) {
    const { check } = make({ start_verification_widget: [{ status: 'quota_exhausted', balance_empty: empty }] });
    await check.start();
    assert.deepEqual(check.state(), { kind: 'quota', empty: expected });
  }
});

test('a submission the bridge lost is a retryable failure with no task to name', async () => {
  const { check } = make({ start_verification_widget: [new Error('bridge gone')] });
  await check.start();
  assert.deepEqual(check.state(), { kind: 'failed', taskId: null, failureClass: '', retryable: true });
});

test('reopen: recovers a finished check by its stored verification_id, never by text', async () => {
  const store = memoryStore();
  store.save({ taskId: 't1', verificationId: 'abcd1234' });
  const { check, host } = make({ get_verification_widget: [completed] }, { store });
  await check.recover();
  assert.equal(check.state().kind, 'completed');
  assert.deepEqual(store.value, { taskId: 't1', verificationId: 'abcd1234' });
  assert.deepEqual(host.calls, [{ name: 'get_verification_widget', args: { task_id: 'abcd1234' } }]);
});

test('reopen: resumes a running check by its stored task_id', async () => {
  const store = memoryStore();
  store.save({ taskId: 't1' });
  const { check, host } = make({ get_verification_widget: [processing('debate', 3)] }, { store });
  await check.recover();
  assert.equal(check.state().kind, 'running');
  assert.equal(check.state().stage, 'Weighing both sides');
  assert.deepEqual(host.calls[0], { name: 'get_verification_widget', args: { task_id: 't1' } });
});

test('reopen: a check that is gone is unrecoverable', async () => {
  const store = memoryStore();
  store.save({ taskId: 't1' });
  const { check } = make({ get_verification_widget: [{ status: 'not_found' }] }, { store });
  await check.recover();
  assert.equal(check.state().kind, 'unrecoverable');
});

test('reopen with nothing stored stays idle and calls nothing', async () => {
  const { check, host } = make({});
  await check.recover();
  assert.equal(check.state().kind, 'idle');
  assert.deepEqual(host.calls, []);
});

test('nothing is ever submitted without start()', async () => {
  const { check, clock, host } = make({ start_verification_widget: [{ status: 'submitted', task_id: 't1' }] });
  await clock.advance(60000);
  await check.recover();
  assert.equal(host.calls.filter((c) => c.name === 'start_verification_widget').length, 0);
});

test("a recovered completion keeps the row's stored ids", async () => {
  const store = memoryStore();
  store.save({ taskId: 't1', verificationId: 'abcd1234' });
  const { check } = make({ get_verification_widget: [completed] }, { store });
  await check.recover();
  assert.deepEqual(store.value, { taskId: 't1', verificationId: 'abcd1234' });
});

test('a stored run the server no longer knows is cleared, so the next mount starts fresh', async () => {
  const store = memoryStore();
  store.save({ taskId: 'gone' });
  const { check } = make({ get_verification_widget: [{ status: 'not_found' }] }, { store });
  await check.recover();
  assert.equal(check.state().kind, 'unrecoverable');
  assert.equal(store.value, null);
});

test('a completion says whether it was recovered or just finished', async () => {
  const store = memoryStore();
  store.save({ taskId: 't1', verificationId: 'abcd1234' });
  const { check } = make({ get_verification_widget: [completed] }, { store });
  await check.recover();
  assert.equal(check.state().kind, 'completed');
  assert.equal(check.state().recovered, true);

  const fresh = make({ start_verification_widget: [{ status: 'submitted', task_id: 't9' }], get_verification_widget: [completed] });
  await fresh.check.start();
  await fresh.clock.advance(POLL_MS);
  assert.equal(fresh.check.state().kind, 'completed');
  assert.equal(fresh.check.state().recovered, false);
});

test('a retry whose answer was lost keeps its retry identity, so the next press joins the same retry', async () => {
  const { check, clock, host } = make({
    start_verification_widget: [{ status: 'submitted', task_id: 'A' }, { status: 'error', message: 'lost' }, { status: 'submitted', task_id: 'B' }],
    get_verification_widget: [{ status: 'failed', failure_class: 'upstream_unavailable', retryable: true }],
  });
  await check.start();
  await clock.advance(POLL_MS);
  assert.equal(check.state().kind, 'failed');
  await check.start();
  assert.equal(check.state().kind, 'failed');
  await check.start();
  const starts = host.calls.filter((c) => c.name === 'start_verification_widget').map((c) => c.args);
  assert.deepEqual(starts, [{ claim: 'The claim.' }, { claim: 'The claim.', retry_of: 'A' }, { claim: 'The claim.', retry_of: 'A' }]);
});

test('an accepted start is remembered even when the card is torn down before the answer lands', async () => {
  let release;
  const store = memoryStore();
  const clock = fakeClock();
  const host = {
    capabilities: {},
    callTool: () => new Promise((resolve) => { release = resolve; }),
  };
  const check = createDeepCheck({ claim: 'The claim.', host, store, clock });
  const pending = check.start();
  check.dispose();
  release({ status: 'submitted', task_id: 'kept' });
  await pending;
  assert.deepEqual(store.value, { taskId: 'kept' });
});

test('a run the model started is adopted from its task_id and polled to the end', async () => {
  // A model-started card keeps no record: the task_id is in the tool result.
  const { check, clock, host, store } = make({ get_verification_widget: [processing('debate', 3), completed] }, { store: null });
  await check.adopt('t-model', { stage: 'Finding sources', index: 2, total: 5, elapsed: 20 });
  assert.equal(check.state().kind, 'running');
  assert.equal(check.state().taskId, 't-model');
  assert.equal(check.state().stage, 'Weighing both sides');
  await clock.advance(POLL_MS);
  assert.equal(check.state().kind, 'completed');
  assert.deepEqual(host.calls.map((c) => c.name), ['get_verification_widget', 'get_verification_widget']);
  assert.equal(store, null, 'no store is given, and nothing throws for the want of one');
});

test('adopting is refused once something else is going on', async () => {
  const { check, host } = make({
    start_verification_widget: [{ status: 'submitted', task_id: 't1' }],
    get_verification_widget: [processing('research', 2)],
  });
  await check.start();
  await check.adopt('t-other');
  assert.equal(check.state().taskId, 't1');
  assert.equal(host.calls.filter((c) => c.name === 'start_verification_widget').length, 1);
});
