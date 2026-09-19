import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  TASK_RECORD_TTL_MS,
  VERIFICATION_RECORD_TTL_MS,
  createAnnouncedStore,
  createCardStore,
  createPickStore,
  createRowStore,
  rowKey,
} from './store.js';

function fakeStorage({ broken = false } = {}) {
  const map = new Map();
  return {
    map,
    getItem: (k) => {
      if (broken) throw new Error('SecurityError');
      return map.has(k) ? map.get(k) : null;
    },
    setItem: (k, v) => {
      if (broken) throw new Error('SecurityError');
      map.set(k, String(v));
    },
    removeItem: (k) => map.delete(k),
  };
}

test('a row record round-trips under a key made from the claim and its quick verdict', () => {
  const storage = fakeStorage();
  const store = createRowStore(storage, { claim: 'The claim.', quickVerdict: 'False' });
  assert.equal(store.available, true);
  store.save({ taskId: 't1' });
  assert.deepEqual(store.load(), { taskId: 't1' });
  store.save({ taskId: 't1', verificationId: 'abcd1234' });
  assert.deepEqual(store.load(), { taskId: 't1', verificationId: 'abcd1234' });
  assert.notEqual(rowKey('The claim.', 'False'), rowKey('The claim.', 'True'));
  assert.notEqual(rowKey('The claim.', 'False'), rowKey('Another claim.', 'False'));
});

test('only known fields of the right shape come back', () => {
  const storage = fakeStorage();
  const store = createRowStore(storage, { claim: 'C', quickVerdict: 'True' });
  storage.setItem(rowKey('C', 'True'), JSON.stringify({ taskId: '<b>', verificationId: 'nothex!!', pushedVersion: 'x', extra: 1 }));
  assert.equal(store.load(), null);
  storage.setItem(rowKey('C', 'True'), 'not json');
  assert.equal(store.load(), null);
});

test('a sandbox without storage degrades to no persistence, never an error', () => {
  const store = createRowStore(fakeStorage({ broken: true }), { claim: 'C', quickVerdict: 'True' });
  assert.equal(store.available, false);
  assert.equal(store.load(), null);
  store.save({ taskId: 't1' });
  assert.equal(store.load(), null);
  const none = createRowStore(undefined, { claim: 'C', quickVerdict: 'True' });
  assert.equal(none.available, false);
});

test('a record expires: a task id after a day (the run is gone), a verification id after a week', () => {
  const storage = fakeStorage();
  let t = 1_000_000;
  const store = createRowStore(storage, { claim: 'C', quickVerdict: 'True', now: () => t });
  store.save({ taskId: 't1' });
  t += 23 * 3600 * 1000;
  assert.deepEqual(store.load(), { taskId: 't1' });
  t += 2 * 3600 * 1000;
  assert.equal(store.load(), null);
  assert.equal(storage.map.size, 0, 'an expired record is removed');

  store.save({ taskId: 't2', verificationId: 'abcd1234' });
  t += 6 * 24 * 3600 * 1000;
  assert.deepEqual(store.load(), { taskId: 't2', verificationId: 'abcd1234' });
  t += 2 * 24 * 3600 * 1000;
  assert.equal(store.load(), null);
});

test('clear removes the record', () => {
  const storage = fakeStorage();
  const store = createRowStore(storage, { claim: 'C', quickVerdict: 'True' });
  store.save({ taskId: 't1' });
  store.clear();
  assert.equal(store.load(), null);
});

test('a record written for another claim under the same key is not recovered', () => {
  const storage = fakeStorage();
  const a = createRowStore(storage, { claim: 'Claim A', quickVerdict: 'False' });
  a.save({ taskId: 't1', verificationId: 'abcd1234' });
  // Force a collision: the same key, read for a different claim.
  const b = createRowStore(storage, { claim: 'Claim B', quickVerdict: 'False' });
  storage.setItem(rowKey('Claim B', 'False'), storage.getItem(rowKey('Claim A', 'False')));
  assert.equal(b.load(), null);
  assert.deepEqual(a.load(), { taskId: 't1', verificationId: 'abcd1234' });
});

test('the card record counts what the model has been told, per row set', () => {
  const storage = fakeStorage();
  let t = 1_000;
  const rows = ['a\u0000False', 'b\u0000True'];
  const card = createCardStore(storage, { rows, now: () => t });
  assert.equal(card.load(), 0);
  card.save(2);
  assert.equal(card.load(), 2);
  // Another row set is another card.
  assert.equal(createCardStore(storage, { rows: ['a\u0000False'], now: () => t }).load(), 0);
  // And it expires with the verification records.
  t += 8 * 24 * 3600 * 1000;
  assert.equal(card.load(), 0);
  assert.equal(createCardStore(storage, { rows: [] }).load(), 0, 'no rows, no record');
});

// Asking whether storage works writes a probe key. A card with nothing to
// remember must not make that write.
test('a card store with no rows touches storage not at all', () => {
  const writes = [];
  const spy = { setItem: (k) => writes.push(k), getItem: () => null, removeItem: (k) => writes.push(`remove:${k}`) };
  const store = createCardStore(spy, { rows: [] });
  assert.equal(store.available, false);
  assert.equal(store.load(), 0);
  store.save(3);
  assert.deepEqual(writes, []);
  // A storeless card is the same, rows or no rows.
  assert.equal(createCardStore(null, { rows: ['a', 'b'] }).available, false);
});

// The picker's record. It is what stops a second charge.
test('the pick record keeps the selection that was SENT, before anything started', () => {
  const store = createPickStore(fakeStorage(), { parent: 'parent-1' });
  store.save({ requested: ['A.', 'B.'] });
  assert.deepEqual(store.load(), { picks: [], requested: ['A.', 'B.'] });
  store.save({ picks: [{ claim: 'A.', taskId: 't-a' }], requested: ['A.', 'B.'] });
  assert.deepEqual(store.load(), { picks: [{ claim: 'A.', taskId: 't-a' }], requested: ['A.', 'B.'] });
});

test('a child id the card could not send back is never stored', () => {
  const store = createPickStore(fakeStorage(), { parent: 'p' });
  store.save({
    picks: [
      { claim: 'A.', taskId: 'bad/id?' },
      { claim: 'B.', taskId: 'x'.repeat(129) },
      { claim: 'C.', taskId: '' },
      { claim: '', taskId: 'orphan' },
      { claim: 'D.', taskId: 'good-1' },
      { claim: 'D again.', taskId: 'good-1' },
    ],
  });
  assert.deepEqual(store.load().picks, [{ claim: 'D.', taskId: 'good-1' }]);
});

test('one picker cannot read another picker record', () => {
  const storage = fakeStorage();
  createPickStore(storage, { parent: 'parent-1' }).save({ picks: [{ claim: 'A.', taskId: 't-a' }] });
  assert.deepEqual(createPickStore(storage, { parent: 'parent-2' }).load().picks, []);
  assert.deepEqual(createPickStore(storage, { parent: 'parent-1' }).load().picks, [{ claim: 'A.', taskId: 't-a' }]);
});

test('a pick record expires with the tasks it names', () => {
  const storage = fakeStorage();
  let now = 1_000_000;
  const store = createPickStore(storage, { parent: 'p', now: () => now });
  store.save({ picks: [{ claim: 'A.', taskId: 't-a' }] });
  now += TASK_RECORD_TTL_MS + 1;
  assert.deepEqual(store.load().picks, [], 'a task id older than a day can only read "not found"');
});

// The announcement ledger. A ui/message lands as the USER's turn, so
// a duplicate is visible and cannot be withdrawn.
test('the ledger is shared by every card in the conversation, not per row set', () => {
  const storage = fakeStorage();
  const first = createAnnouncedStore(storage);
  const second = createAnnouncedStore(storage);
  assert.deepEqual(first.reserve(['v1', 'v2']), ['v1', 'v2']);
  // A second card, showing other rows, must still see v1 as taken.
  assert.deepEqual(second.reserve(['v1']), []);
  assert.deepEqual(second.reserve(['v1', 'v3']), ['v3']);
  assert.deepEqual(second.announced().sort(), ['v1', 'v2', 'v3']);
});

test('the ledger is re-read at reserve time, not cached at construction', () => {
  const storage = fakeStorage();
  const held = createAnnouncedStore(storage);
  // Built BEFORE anything was announced, as a mounted card would be.
  const other = createAnnouncedStore(storage);
  held.reserve(['v1']);
  assert.deepEqual(other.reserve(['v1']), [], 'a card mounted earlier must still see it');
});

test('an entry expires with the verification window it describes', () => {
  const storage = fakeStorage();
  let now = 1_000_000;
  const ledger = createAnnouncedStore(storage, { now: () => now });
  ledger.reserve(['v1']);
  now += VERIFICATION_RECORD_TTL_MS + 1;
  assert.deepEqual(ledger.announced(), [], 'the same claim asked again next month is a new check');
  assert.deepEqual(ledger.reserve(['v1']), ['v1']);
});

test('without storage the ledger claims everything rather than nothing', () => {
  // No same-origin sandbox: there is no guard to be had, and the reader still
  // needs to be told what their check found.
  const ledger = createAnnouncedStore(null);
  assert.equal(ledger.available, false);
  assert.deepEqual(ledger.reserve(['v1', 'v2']), ['v1', 'v2']);
});

test('a corrupt ledger reads as empty, never as an error', () => {
  const storage = fakeStorage();
  storage.setItem('lenz-card:v1:announced', '{not json');
  const ledger = createAnnouncedStore(storage);
  assert.deepEqual(ledger.announced(), []);
  assert.deepEqual(ledger.reserve(['v1']), ['v1']);
});
