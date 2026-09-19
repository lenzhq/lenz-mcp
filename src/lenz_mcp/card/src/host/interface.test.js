import { test } from 'node:test';
import assert from 'node:assert/strict';

import { HOST_CAPABILITIES, HOST_OPERATIONS } from './interface.js';
import { createMcpAppsHost } from './mcp-apps.js';

// The contract every host adapter meets. A ChatGPT adapter later runs this same
// test against its own factory.
function contract(name, factory) {
  test(`${name} implements the whole host interface`, () => {
    const win = {
      parent: { postMessage() {} },
      addEventListener() {},
      removeEventListener() {},
      setTimeout,
      clearTimeout,
    };
    const host = factory(win);
    for (const op of HOST_OPERATIONS) assert.equal(typeof host[op], 'function', op);
    assert.deepEqual(Object.keys(host.capabilities).sort(), [...HOST_CAPABILITIES].sort());
    for (const cap of HOST_CAPABILITIES) assert.equal(host.capabilities[cap], false, `${cap} is off until connect()`);
    host.dispose();
  });
}

contract('MCP Apps adapter', (win) => createMcpAppsHost({ win }));
