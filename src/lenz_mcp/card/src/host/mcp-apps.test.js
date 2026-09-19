import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createMcpAppsHost, HostError } from './mcp-apps.js';

const flush = () => new Promise((resolve) => setImmediate(resolve));

// A window + parent pair: the card posts to `parent`, the "host" answers by
// dispatching message events on `win` with `source: parent`.
function fakeFrame(answer = () => undefined) {
  const listeners = new Set();
  const sent = [];
  const parent = {
    postMessage(msg) {
      sent.push(msg);
      const reply = answer(msg);
      if (reply !== undefined) queueMicrotask(() => deliver(reply));
    },
  };
  const win = {
    parent,
    addEventListener: (type, fn) => type === 'message' && listeners.add(fn),
    removeEventListener: (type, fn) => listeners.delete(fn),
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
  };
  function deliver(data, source = parent) {
    for (const fn of [...listeners]) fn({ data, source });
  }
  return { win, parent, sent, deliver, listeners };
}

const initResult = {
  protocolVersion: '2026-01-26',
  hostInfo: { name: 'Claude', version: '1.0.0' },
  hostCapabilities: { message: { text: {} }, updateModelContext: { text: {} }, openLinks: {}, serverTools: {} },
  hostContext: { theme: 'dark', styles: { variables: { '--color-background-primary': 'light-dark(#fff, #333)' } } },
};

function standardHost(extra = {}) {
  return (msg) => {
    if (msg.method === 'ui/initialize') return { jsonrpc: '2.0', id: msg.id, result: initResult };
    if (msg.method in extra) return extra[msg.method](msg);
    if (msg.id !== undefined && msg.method) return { jsonrpc: '2.0', id: msg.id, result: {} };
    return undefined;
  };
}

test('connect: the handshake follows the spec types (appInfo, then initialized)', async () => {
  const frame = fakeFrame(standardHost());
  const host = createMcpAppsHost({ win: frame.win });
  const ready = await host.connect();
  assert.deepEqual(frame.sent[0].params, {
    appInfo: { name: 'lenz-card', version: '1' },
    appCapabilities: { availableDisplayModes: ['inline'] },
    protocolVersion: '2026-01-26',
  });
  assert.equal(frame.sent[0].method, 'ui/initialize');
  assert.equal(frame.sent[1].method, 'ui/notifications/initialized');
  assert.equal(ready.context.theme, 'dark');
  assert.deepEqual(host.capabilities, { message: true, updateModelContext: true, openLinks: true, serverTools: true });
});

test('capabilities are feature-detected: a missing one is false', async () => {
  const frame = fakeFrame((msg) =>
    msg.method === 'ui/initialize'
      ? { jsonrpc: '2.0', id: msg.id, result: { ...initResult, hostCapabilities: { serverTools: {} } } }
      : undefined,
  );
  const host = createMcpAppsHost({ win: frame.win });
  await host.connect();
  assert.deepEqual(host.capabilities, { message: false, updateModelContext: false, openLinks: false, serverTools: true });
  await assert.rejects(host.openLink('https://a.example/'), HostError);
  await assert.rejects(host.sendMessage('hi'), HostError);
});

test('callTool returns structuredContent, and throws on a protocol error', async () => {
  const frame = fakeFrame(
    standardHost({
      'tools/call': (msg) =>
        msg.params.name === 'boom'
          ? { jsonrpc: '2.0', id: msg.id, error: { code: -32000, message: 'denied' } }
          : { jsonrpc: '2.0', id: msg.id, result: { content: [{ type: 'text', text: '{}' }], structuredContent: { status: 'submitted', task_id: 't1' } } },
    }),
  );
  const host = createMcpAppsHost({ win: frame.win });
  await host.connect();
  assert.deepEqual(await host.callTool('start_verification_widget', { claim: 'X', depth: 'standard' }), {
    status: 'submitted',
    task_id: 't1',
  });
  await assert.rejects(host.callTool('boom', {}), HostError);
});

test('callTool falls back to JSON text content when there is no structuredContent', async () => {
  const frame = fakeFrame(
    standardHost({
      'tools/call': (msg) => ({ jsonrpc: '2.0', id: msg.id, result: { content: [{ type: 'text', text: '{"status":"processing"}' }] } }),
    }),
  );
  const host = createMcpAppsHost({ win: frame.win });
  await host.connect();
  assert.deepEqual(await host.callTool('get_verification_widget', { task_id: 't1' }), { status: 'processing' });
});

test('messages from anything but the parent frame are ignored', async () => {
  const frame = fakeFrame(standardHost());
  const host = createMcpAppsHost({ win: frame.win });
  await host.connect();
  const results = [];
  host.onToolResult((r) => results.push(r));
  frame.deliver(
    { jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: { status: 'ok' } } },
    { postMessage() {} },
  );
  await flush();
  assert.deepEqual(results, []);
  frame.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: { status: 'ok' } } });
  await flush();
  assert.deepEqual(results, [{ status: 'ok' }]);
});

test('a tool result that arrived before a subscriber is replayed to it', async () => {
  const frame = fakeFrame(standardHost());
  const host = createMcpAppsHost({ win: frame.win });
  await host.connect();
  frame.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: { status: 'ok' } } });
  await flush();
  const results = [];
  host.onToolResult((r) => results.push(r));
  assert.deepEqual(results, [{ status: 'ok' }]);
});

test('context updates, links and messages use the spec methods and shapes', async () => {
  const frame = fakeFrame(standardHost());
  const host = createMcpAppsHost({ win: frame.win });
  await host.connect();
  await host.updateModelContext({ text: 'Lenz card update (v1).', structured: { lenz_card: { version: 1 } } });
  await host.openLink('https://www.bls.gov/x');
  await host.sendMessage('Ask Lenz about this check (verification abcd1234): ');
  const byMethod = Object.fromEntries(frame.sent.map((m) => [m.method, m.params]));
  assert.deepEqual(byMethod['ui/update-model-context'], {
    content: [{ type: 'text', text: 'Lenz card update (v1).' }],
    structuredContent: { lenz_card: { version: 1 } },
  });
  assert.deepEqual(byMethod['ui/open-link'], { url: 'https://www.bls.gov/x' });
  assert.deepEqual(byMethod['ui/message'], {
    role: 'user',
    content: [{ type: 'text', text: 'Ask Lenz about this check (verification abcd1234): ' }],
  });
});

test('openLink refuses anything but http(s) before it reaches the host', async () => {
  const frame = fakeFrame(standardHost());
  const host = createMcpAppsHost({ win: frame.win });
  await host.connect();
  const before = frame.sent.length;
  await assert.rejects(host.openLink('javascript:alert(1)'), HostError);
  assert.equal(frame.sent.length, before);
});

test('size reports are deduplicated', async () => {
  const frame = fakeFrame(standardHost());
  const host = createMcpAppsHost({ win: frame.win });
  await host.connect();
  host.reportSize(735, 400);
  host.reportSize(735, 400);
  host.reportSize(735, 420);
  assert.equal(frame.sent.filter((m) => m.method === 'ui/notifications/size-changed').length, 2);
});

test('teardown runs the handler, then answers the host; dispose stops listening', async () => {
  const frame = fakeFrame(standardHost());
  const host = createMcpAppsHost({ win: frame.win });
  await host.connect();
  let tornDown = 0;
  host.onTeardown(() => {
    tornDown += 1;
  });
  frame.deliver({ jsonrpc: '2.0', id: 77, method: 'ui/resource-teardown', params: {} });
  await flush();
  assert.equal(tornDown, 1);
  assert.deepEqual(frame.sent.at(-1), { jsonrpc: '2.0', id: 77, result: {} });
  host.dispose();
  assert.equal(frame.listeners.size, 0);
});

test('theme changes reach the subscriber', async () => {
  const frame = fakeFrame(standardHost());
  const host = createMcpAppsHost({ win: frame.win });
  await host.connect();
  const seen = [];
  host.onContextChange((ctx) => seen.push(ctx.theme));
  frame.deliver({ jsonrpc: '2.0', method: 'ui/notifications/host-context-changed', params: { theme: 'light' } });
  await flush();
  assert.deepEqual(seen, ['light']);
});

test('a request with no answer times out as a HostError', async () => {
  const frame = fakeFrame((msg) =>
    msg.method === 'ui/initialize' ? { jsonrpc: '2.0', id: msg.id, result: initResult } : undefined,
  );
  const host = createMcpAppsHost({ win: frame.win, timeoutMs: 20 });
  await host.connect();
  await assert.rejects(host.callTool('get_verification_widget', { task_id: 't1' }), HostError);
});
