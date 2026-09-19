import { test } from 'node:test';
import assert from 'node:assert/strict';

import { THEME_SLOTS, mcpAppsTheme } from './theme.js';

test('the MCP Apps adapter maps only the slots the host actually published', () => {
  assert.deepEqual(mcpAppsTheme(undefined), {});
  assert.deepEqual(mcpAppsTheme({ '--color-text-primary': 'light-dark(#000, #fff)' }), {
    '--lz-ink': 'var(--color-text-primary)',
  });
  const all = mcpAppsTheme({
    '--color-background-primary': 'x',
    '--color-text-primary': 'x',
    '--color-text-secondary': 'x',
    '--color-border-tertiary': 'x',
    '--color-unrelated': 'x',
  });
  assert.deepEqual(Object.keys(all).sort(), [...THEME_SLOTS].sort());
  assert.deepEqual(mcpAppsTheme({ '--color-text-primary': '' }), {}, 'an empty value is not a slot');
});
