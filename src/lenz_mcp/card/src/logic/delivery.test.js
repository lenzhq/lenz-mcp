import { test } from 'node:test';
import assert from 'node:assert/strict';

import { DELIVER_CONTEXT, DELIVER_MESSAGE, deliveryOf, readDelivery, watchDelivery } from './delivery.js';

const hostWith = (result) => ({ callTool: async () => result, other: 'kept' });

test('the card takes the mechanism from the server, and never guesses', async () => {
  const host = watchDelivery(hostWith({ status: 'submitted', _card: { deliver: 'message' } }));
  // Before any call it is the quiet mechanism: nothing has said otherwise.
  assert.equal(readDelivery(host), DELIVER_CONTEXT);
  await host.callTool('start_verification_widget', {});
  assert.equal(readDelivery(host), DELIVER_MESSAGE);
});

test('an old server, or a field we do not recognise, keeps the quiet mechanism', async () => {
  for (const result of [
    {},
    null,
    { _card: {} },
    { _card: { deliver: '' } },
    { _card: { deliver: 'both' } },
    { _card: { deliver: 'MESSAGE' } },
    { _card: 'message' },
    { deliver: 'message' },
    { _card: { deliver: { toString: null } } },
  ]) {
    const host = watchDelivery(hostWith(result));
    await host.callTool('t', {});
    assert.equal(readDelivery(host), DELIVER_CONTEXT, JSON.stringify(result));
  }
});

test('the MOST RECENT card-tool response decides', async () => {
  const answers = [{ _card: { deliver: 'message' } }, { _card: { deliver: 'context' } }];
  let i = 0;
  const host = watchDelivery({ callTool: async () => answers[i++] });
  await host.callTool('t', {});
  assert.equal(readDelivery(host), DELIVER_MESSAGE);
  await host.callTool('t', {});
  assert.equal(readDelivery(host), DELIVER_CONTEXT);
});

test('two cards do not share a mechanism', async () => {
  const chatgpt = watchDelivery(hostWith({ _card: { deliver: 'message' } }));
  const claude = watchDelivery(hostWith({ _card: { deliver: 'context' } }));
  await chatgpt.callTool('t', {});
  await claude.callTool('t', {});
  assert.equal(readDelivery(chatgpt), DELIVER_MESSAGE);
  assert.equal(readDelivery(claude), DELIVER_CONTEXT);
});

test('a live getter on the host stays live through the wrapper', async () => {
  // The adapter exposes `capabilities` as a getter filled at connect. A spread
  // would freeze it to its pre-connect value — every capability false, every
  // button gone, and every flow timing out rather than failing loudly.
  let declared = { message: false };
  const host = {
    get capabilities() {
      return declared;
    },
    callTool: async () => ({}),
  };
  const watched = watchDelivery(host);
  assert.deepEqual(watched.capabilities, { message: false });
  declared = { message: true };
  assert.deepEqual(watched.capabilities, { message: true }, 'the wrapper must not snapshot a getter');
});

test('the wrapper passes the result through untouched and keeps the host whole', async () => {
  const result = { status: 'submitted', task_id: 'a', _card: { deliver: 'message' } };
  const host = watchDelivery(hostWith(result));
  assert.equal(await host.callTool('t', {}), result, 'the card reads the same object');
  assert.equal(host.other, 'kept');
});

test('deliveryOf reads only the card namespace', () => {
  assert.equal(deliveryOf({ _card: { deliver: 'message' } }), 'message');
  assert.equal(deliveryOf({ _card: { deliver: 'context' } }), 'context');
  assert.equal(deliveryOf({ ui: { deliver: 'message' } }), '');
});
