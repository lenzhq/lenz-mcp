import assert from 'node:assert/strict';
import { test } from 'node:test';

import * as copy from '../copy.js';
import { creditFacts, quotaWords, quotaFactsOf } from './credits.js';

const NOW = new Date(2026, 10, 23, 12).getTime();
const local = (y, m, d) => new Date(y, m - 1, d).getTime();

test('a refusal that states what the call needs, the balance and the reset is read as it was sent', () => {
  const facts = creditFacts({ status: 'quota_exhausted', cost: 10, credits_remaining: 7, resets_at: '2026-12-01T00:00:00+00:00' });
  assert.equal(facts.needs, 10);
  assert.equal(facts.has, 7);
  assert.equal(facts.resetsAt, Date.parse('2026-12-01T00:00:00+00:00'));
});

test('a refusal with no facts reads as no facts', () => {
  for (const payload of [{ status: 'quota_exhausted' }, {}, null, undefined, 'x', 5, []]) {
    assert.deepEqual(creditFacts(payload), { needs: null, has: null, resetsAt: null });
  }
});

test('only a whole, non-negative, sane number is a credit count; anything else is absent', () => {
  const bad = ['10', '', -1, 1.5, 1e21, 1e10, Number.MAX_SAFE_INTEGER, NaN, Infinity, -Infinity, true, null, {}, [10], { valueOf: () => 10 }];
  for (const value of bad) {
    const facts = creditFacts({ cost: value, credits_remaining: value });
    assert.equal(facts.needs, null, String(value));
    assert.equal(facts.has, null, String(value));
  }
  for (const value of [0, 1, 7, 10, 1000]) {
    assert.equal(creditFacts({ cost: value }).needs, value);
    assert.equal(creditFacts({ credits_remaining: value }).has, value);
  }
});

test('the total a call needs is `cost`; a price per citation is not a total and is never read', () => {
  assert.equal(creditFacts({ cost_per_citation: 1, credits_remaining: 0 }).needs, null);
  assert.equal(creditFacts({ requested: 3, credits_remaining: 0 }).needs, null);
  assert.equal(creditFacts({ cost: 3, cost_per_citation: 1, requested: 3 }).needs, 3);
});

test('a reset date that does not parse is absent: not a string, not a date, an impossible day', () => {
  const bad = [12345, null, true, {}, [], '', 'tomorrow', '1', 'not-a-date', '2026-13-45T00:00:00Z', '2026-12-01T25:00:00Z', 'x'.repeat(500), '<b>2026-12-01</b>'];
  for (const value of bad) assert.equal(creditFacts({ resets_at: value }).resetsAt, null, String(value));
  assert.equal(creditFacts({ resets_at: '2026-12-01' }).resetsAt, Date.parse('2026-12-01'));
});

test('a bad number beside a good one costs only the bad one', () => {
  assert.deepEqual(creditFacts({ cost: 10, credits_remaining: -2, resets_at: '2026-12-01T00:00:00Z' }), {
    needs: 10,
    has: null,
    resetsAt: Date.parse('2026-12-01T00:00:00Z'),
  });
});

test('the facts a state keeps are only the ones that were present', () => {
  assert.deepEqual(quotaFactsOf({ needs: null, has: null, resetsAt: null }), {});
  assert.deepEqual(quotaFactsOf({ needs: 10, has: 0, resetsAt: null }), { needs: 10, has: 0 });
  assert.deepEqual(quotaFactsOf({ needs: 10, has: 7, resetsAt: 5 }), { needs: 10, has: 7, resetsAt: 5 });
  assert.deepEqual(quotaFactsOf(undefined), {});
});

test('a card-started deep check: the sentence, then the date; no "ask" line when the numbers are shown', () => {
  const words = quotaWords({ scope: 'deep', empty: false, needs: 10, has: 7, resetsAt: local(2026, 12, 1) }, NOW);
  assert.equal(words.heading, 'A deep check needs 10 credits and you have 7.');
  assert.deepEqual(words.lines, ['Your credits reset on 1 December.']);
});

test('a whole call the model made: the sentence is the heading', () => {
  const words = quotaWords({ scope: 'call', needs: 3, has: 0, resetsAt: null }, NOW);
  assert.equal(words.heading, 'This check needs 3 credits and you have 0.');
  assert.deepEqual(words.lines, []);
});

test('numbers absent, a deep check the balance did not cover: today\'s line, the date if any, then the question', () => {
  assert.deepEqual(quotaWords({ scope: 'deep', empty: false }, NOW), {
    heading: copy.QUOTA_SHORT,
    lines: [copy.QUOTA_NEXT],
  });
  assert.deepEqual(quotaWords({ scope: 'deep', empty: false, resetsAt: local(2026, 12, 1) }, NOW), {
    heading: copy.QUOTA_SHORT,
    lines: ['Your credits reset on 1 December.', copy.QUOTA_NEXT],
  });
});

test('numbers absent on an empty balance: the first line stands alone, or beside the date', () => {
  assert.deepEqual(quotaWords({ scope: 'deep', empty: true }, NOW), { heading: copy.QUOTA_EMPTY, lines: [] });
  assert.deepEqual(quotaWords({ scope: 'call', has: 0 }, NOW), { heading: copy.QUOTA_EMPTY, lines: [] });
  assert.deepEqual(quotaWords({ scope: 'call', has: 0, resetsAt: local(2026, 12, 1) }, NOW), {
    heading: copy.QUOTA_EMPTY,
    lines: ['Your credits reset on 1 December.'],
  });
});

test('a whole call with nothing known keeps today\'s two lines: an empty balance is not assumed', () => {
  assert.deepEqual(quotaWords({ scope: 'call' }, NOW), { heading: copy.QUOTA_EMPTY, lines: [copy.QUOTA_NEXT] });
  // A balance known to be short, but no price: still never says the numbers.
  assert.deepEqual(quotaWords({ scope: 'call', has: 7 }, NOW), { heading: copy.QUOTA_EMPTY, lines: [copy.QUOTA_NEXT] });
});

test('one number alone is not shown: the sentence needs both', () => {
  assert.deepEqual(quotaWords({ scope: 'deep', empty: false, needs: 10 }, NOW), { heading: copy.QUOTA_SHORT, lines: [copy.QUOTA_NEXT] });
  assert.deepEqual(quotaWords({ scope: 'deep', empty: true, has: 0 }, NOW), { heading: copy.QUOTA_EMPTY, lines: [] });
});

test('the words never carry a link', () => {
  const words = quotaWords({ scope: 'deep', empty: false, needs: 10, has: 7, resetsAt: local(2026, 12, 1) }, NOW);
  for (const line of [words.heading, ...words.lines]) assert.doesNotMatch(line, /https?:|\/|plan|upgrade|buy/i, line);
});
