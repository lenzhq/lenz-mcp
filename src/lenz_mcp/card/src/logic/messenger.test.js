import { test } from 'node:test';
import assert from 'node:assert/strict';

import { CEILING_MS, createMessenger } from './messenger.js';

// Sends are serialized on a promise chain, so a second message is one more
// turn of the queue away: drain it rather than guessing at ticks.
const drain = () => new Promise((resolve) => setImmediate(resolve));

const check = (id, order, extra = {}) => ({ verificationId: id, order, verdict: 'True', claim: `Claim ${order}`, ...extra });

// One ledger per conversation, shared by every card in it — the real shape.
function makeLedger(held = []) {
  const ids = new Set(held);
  return {
    announced: () => [...ids],
    reserve(wanted) {
      const mine = wanted.filter((id) => !ids.has(id));
      for (const id of mine) ids.add(id);
      return mine;
    },
  };
}

function harness({ sent = [], ledger = makeLedger(sent) } = {}) {
  const timers = [];
  const messages = [];
  const messenger = createMessenger({
    ledger,
    send: (checks) => {
      messages.push(checks);
      return Promise.resolve();
    },
    timer: (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length;
    },
    cancel: (id) => {
      if (timers[id - 1]) timers[id - 1].cancelled = true;
    },
  });
  return { messenger, messages, timers, ledger };
}

test('one check is one message, as soon as it lands', async () => {
  const { messenger, messages } = harness();
  messenger.started('a');
  messenger.completed('a', check('v1', 0));
  await drain();
  assert.equal(messages.length, 1);
  assert.deepEqual(messages[0].map((c) => c.verificationId), ['v1']);
});

test('five picked checks are ONE message, when the fifth settles', async () => {
  const { messenger, messages } = harness();
  for (const key of ['a', 'b', 'c', 'd', 'e']) messenger.started(key);
  messenger.completed('a', check('v1', 0));
  messenger.completed('b', check('v2', 1));
  messenger.completed('c', check('v3', 2));
  messenger.completed('d', check('v4', 3));
  assert.equal(messages.length, 0, 'nothing while any check is still running');
  messenger.completed('e', check('v5', 4));
  await drain();
  assert.equal(messages.length, 1);
  assert.deepEqual(messages[0].map((c) => c.verificationId), ['v1', 'v2', 'v3', 'v4', 'v5']);
});

test('the message lists checks in the order they were STARTED, not finished', async () => {
  const { messenger, messages } = harness();
  for (const key of ['a', 'b', 'c']) messenger.started(key);
  // Row 3 finishes first, row 1 last.
  messenger.completed('c', check('v3', 2));
  messenger.completed('b', check('v2', 1));
  messenger.completed('a', check('v1', 0));
  await drain();
  assert.deepEqual(messages[0].map((c) => c.verificationId), ['v1', 'v2', 'v3']);
});

test('a failed check is not announced, and does not hold the others back', async () => {
  const { messenger, messages } = harness();
  messenger.started('a');
  messenger.started('b');
  messenger.completed('a', check('v1', 0));
  assert.equal(messages.length, 0);
  messenger.failed('b');
  await drain();
  assert.equal(messages.length, 1);
  assert.deepEqual(messages[0].map((c) => c.verificationId), ['v1'], 'only the one with a verdict');
});

test('every check failing says nothing at all', async () => {
  const { messenger, messages } = harness();
  messenger.started('a');
  messenger.started('b');
  messenger.failed('a');
  messenger.failed('b');
  await drain();
  assert.deepEqual(messages, []);
});

test('a stuck check does not hold the rest past the ceiling, and the remainder follows', async () => {
  const { messenger, messages, timers } = harness();
  messenger.started('a');
  messenger.started('b');
  messenger.completed('a', check('v1', 0));
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, CEILING_MS);
  timers[0].fn();
  await drain();
  assert.deepEqual(messages[0].map((c) => c.verificationId), ['v1'], 'what has landed goes out');
  // The straggler lands later and is announced once, on its own.
  messenger.completed('b', check('v2', 1));
  await drain();
  assert.equal(messages.length, 2, 'at most two messages per batch');
  assert.deepEqual(messages[1].map((c) => c.verificationId), ['v2']);
});

test('a check already announced is never announced again', async () => {
  const { messenger, messages } = harness({ sent: ['v1'] });
  messenger.started('a');
  messenger.completed('a', check('v1', 0));
  await drain();
  assert.deepEqual(messages, [], 'a re-mount, a reload or a second card posts nothing');
});

test('what was announced is recorded in the ledger, so a reload knows', async () => {
  const { messenger, ledger } = harness();
  messenger.started('a');
  messenger.completed('a', check('v1', 0));
  await drain();
  assert.deepEqual(ledger.announced(), ['v1']);
});

test('two cards sharing a conversation announce a check once between them', async () => {
  // Two mounted cards each hold their own messenger; the ledger is what stops
  // the second from posting a second user turn for one verification.
  const ledger = makeLedger();
  const first = harness({ ledger });
  const second = harness({ ledger });
  first.messenger.started('a');
  first.messenger.completed('a', check('v1', 0));
  await drain();
  second.messenger.started('b');
  second.messenger.completed('b', check('v1', 0));
  await drain();
  assert.equal(first.messages.length, 1);
  assert.deepEqual(second.messages, [], 'the second card must not repeat it');
});

test('the ceiling fires once per batch, never a third message', async () => {
  const { messenger, messages, timers } = harness();
  for (const key of ['a', 'b', 'c']) messenger.started(key);
  messenger.completed('a', check('v1', 0));
  timers[0].fn();
  await drain();
  assert.equal(messages.length, 1);
  // B lands while C still runs: the ceiling must NOT re-arm, or B goes out on
  // its own and C makes a third message.
  messenger.completed('b', check('v2', 1));
  await drain();
  assert.equal(timers.length, 1, 'no second ceiling for this batch');
  assert.equal(messages.length, 1, 'B waits for the batch to settle');
  messenger.completed('c', check('v3', 2));
  await drain();
  assert.equal(messages.length, 2, 'at most two messages per batch');
  assert.deepEqual(messages[1].map((m) => m.verificationId), ['v2', 'v3']);
});

test('a ledger that cannot record still lets the card speak, once per mount', async () => {
  // Storage may be absent (a sandbox with no same-origin). The message matters
  // more than the guard: the card says it, and its own in-memory set stops it
  // repeating within this mount.
  const messenger = createMessenger({ ledger: null, send: () => Promise.resolve() });
  messenger.started('a');
  messenger.completed('a', check('v1', 0));
  assert.equal(messenger.state().sent.length, 1);
});

test('a completion landing mid-send is not included twice', async () => {
  const { messenger, messages } = harness();
  messenger.started('a');
  messenger.completed('a', check('v1', 0));
  messenger.completed('a', check('v1', 0));
  await drain();
  assert.equal(messages.length, 1);
  assert.deepEqual(messages[0].map((c) => c.verificationId), ['v1']);
});
