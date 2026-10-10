import { test } from 'node:test';
import assert from 'node:assert/strict';

import { mountIdentity } from './identity.js';

test('the identity is what the server stamped on the mounting result', () => {
  assert.deepEqual(mountIdentity({ status: 'ok', _card: { conversation: '0123456789abcdef', call_id: 'fedcba9876543210' } }), {
    conversation: '0123456789abcdef',
    callId: 'fedcba9876543210',
    key: '0123456789abcdef|fedcba9876543210',
  });
});

test('an unstamped result has no identity, and a missing half is simply absent', () => {
  const none = { conversation: '', callId: '', key: '' };
  assert.deepEqual(mountIdentity(undefined), none);
  assert.deepEqual(mountIdentity({ status: 'ok' }), none);
  assert.deepEqual(mountIdentity({ _card: 'x' }), none);
  assert.deepEqual(mountIdentity({ _card: { deliver: 'message' } }), none);
  assert.deepEqual(mountIdentity({ _card: { call_id: 'abc123' } }), { conversation: '', callId: 'abc123', key: '|abc123' });
});

test('a stamp that is not a short token is ignored, never coerced', () => {
  const none = { conversation: '', callId: '', key: '' };
  for (const bad of [42, null, {}, [], 'has space', 'a'.repeat(65), '<b>x</b>', '']) {
    assert.deepEqual(mountIdentity({ _card: { conversation: bad, call_id: bad } }), none, String(bad));
  }
});
