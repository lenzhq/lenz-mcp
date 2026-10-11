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

test('the pages disclosure reads exactly as agreed, and says pages, not sources', () => {
  assert.equal(copy.pagesSummary(1), 'Pages the reviewers looked at (1)');
  assert.equal(copy.pagesSummary(12), 'Pages the reviewers looked at (12)');
  assert.equal(copy.showAllPages(7), 'Show all 7');
  for (const s of [copy.pagesSummary(3), copy.showAllPages(3)]) {
    assert.doesNotMatch(s, /source|evidence|citation/i, s);
  }
});

test('a quick check no longer says it shows no sources', () => {
  assert.equal(copy.QUICK_FOOTER, undefined);
  for (const line of Object.values(copy.CONFIDENCE_LINE)) assert.doesNotMatch(line, /no sources/i, line);
  assert.equal(copy.CONFIDENCE_LINE.high, 'Confidence: high · a first read');
  assert.equal(copy.CONFIDENCE_LINE.medium, 'Confidence: medium · a first read');
});
