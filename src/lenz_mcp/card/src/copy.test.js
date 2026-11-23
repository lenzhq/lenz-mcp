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

test('a check that found too few sources says so, in one sentence', () => {
  const text = copy.FAILED_BY_CLASS.insufficient_evidence;
  assert.match(text, /too few public sources/);
  assert.match(text, /Nothing was charged/);
  assert.notEqual(text, copy.FAILED_DEFAULT);
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

// The heading is the one line a reader takes away from a citation check, so it
// may never say more than the rows under it: "all check out" needs every
// citation checked and supported.
test('the citation heading says "all check out" only when every citation was checked and supported', () => {
  assert.equal(copy.citecheckSummary({ total: 2, checked: 2 }), '2 citations checked · all check out');
  assert.equal(copy.citecheckSummary({ total: 1, checked: 1 }), '1 citation checked · it checks out');
});

test('a citation check that read nothing says so, and never that the citations check out', () => {
  assert.equal(copy.citecheckSummary({ total: 2, checked: 0 }), 'No citations could be checked');
  assert.equal(copy.citecheckSummary({ total: 1, checked: 0 }), 'The citation could not be checked');
});

test('the citation heading names what is left: unchecked, a closer look, or issues, most serious first', () => {
  assert.equal(copy.citecheckSummary({ total: 2, checked: 1 }), '1 of 2 citations checked · 1 could not be checked');
  assert.equal(copy.citecheckSummary({ total: 5, checked: 2 }), '2 of 5 citations checked · 3 could not be checked');
  assert.equal(copy.citecheckSummary({ total: 2, checked: 2, look: 1 }), '2 citations checked · 1 needs a closer look');
  assert.equal(copy.citecheckSummary({ total: 4, checked: 4, look: 2 }), '4 citations checked · 2 need a closer look');
  assert.equal(copy.citecheckSummary({ total: 7, checked: 5, issues: 2, look: 1 }), '5 of 7 citations checked · 2 need attention');
  assert.equal(copy.citecheckSummary({ total: 3, checked: 3, issues: 1 }), '3 citations checked · 1 needs attention');
  // An issue found without reading the page (a DOI that is not registered) is still an issue.
  assert.equal(copy.citecheckSummary({ total: 1, checked: 0, issues: 1 }), '1 citation checked · 1 needs attention');
});

test('no citation heading says "hold up": the group under it is called Checks out', () => {
  for (const args of [{ total: 2, checked: 2 }, { total: 2, checked: 0 }, { total: 2, checked: 1 }, { total: 2, checked: 2, look: 1 }, { total: 2, checked: 2, issues: 1 }]) {
    assert.doesNotMatch(copy.citecheckSummary(args), /hold/i);
  }
});
