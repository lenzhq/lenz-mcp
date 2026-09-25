import assert from 'node:assert/strict';
import { test } from 'node:test';

import * as copy from './copy.js';

// Every string the card can show, functions evaluated with sample arguments.
function everyString() {
  const out = [];
  const collect = (value) => {
    if (typeof value === 'string') out.push(value);
    else if (typeof value === 'function') {
      try {
        collect(value(2, 3, 4));
      } catch (_e) {
        // A builder that needs a richer argument is covered by its own test.
      }
      collect(value({ wrong: 1, mixed: 1, hold: 1, error: 1 }));
    } else if (value && typeof value === 'object') {
      for (const v of Object.values(value)) collect(v);
    }
  };
  for (const value of Object.values(copy)) collect(value);
  return out;
}

test('the copy names no assistant: the same card runs in every host', () => {
  const strings = everyString();
  assert.ok(strings.length > 50, 'the walk reached the copy');
  for (const s of strings) {
    assert.doesNotMatch(s, /claude|chatgpt|openai|anthropic/i, s);
  }
});

test('no brand label: the host already names the app above the card', () => {
  assert.equal(copy.BRAND, undefined);
});
