// What a refusal for lack of credits says. Pure.
//
// The facts come in two shapes that carry the same names: the tool result of a
// whole call the MODEL made (`cost`, `credits_remaining`, `resets_at`, beside
// fields the card never reads) and the reduced result of the deep check the
// CARD started. Both are untrusted, like every other payload: a count is shown
// only as a whole, non-negative number of a sane size, and a date only when
// `Date` parses it. A payload string is never rendered.
//
// `cost` is the total the refused call needed, the figure the connector tells
// the model ("this call costs N"). `cost_per_citation` is a price for one
// citation, not a total, so it is never read: without `cost` there is no total.

import * as copy from '../copy.js';

// Far above any balance a plan holds, far below the point where a count stops
// being a number a person can read.
const MAX_CREDITS = 1_000_000_000;
const count = (value) => (Number.isInteger(value) && value >= 0 && value <= MAX_CREDITS ? value : null);

// The connector sends an ISO 8601 moment. A string that does not begin with a
// calendar day is not one, whatever `Date` would make of it.
const ISO_DAY = /^\d{4}-\d{2}-\d{2}/;
function moment(value) {
  if (typeof value !== 'string' || value.length > 64 || !ISO_DAY.test(value)) return null;
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? null : ms;
}

// `null` for every fact that is absent or not trustworthy.
export function creditFacts(payload) {
  const p = payload && typeof payload === 'object' ? payload : {};
  return { needs: count(p.cost), has: count(p.credits_remaining), resetsAt: moment(p.resets_at) };
}

// The facts a card state keeps: only the ones that were there.
export function quotaFactsOf(facts) {
  const f = facts && typeof facts === 'object' ? facts : {};
  const out = {};
  for (const key of ['needs', 'has', 'resetsAt']) {
    if (f[key] !== null && f[key] !== undefined) out[key] = f[key];
  }
  return out;
}

// The lines of a refusal. `scope` is 'deep' for a deep check the card started
// (the connector says whether the balance is empty) and 'call' for a whole call
// the model made (an empty balance is known only when the balance is 0). With
// both numbers the sentence is the first line and nothing asks for them again;
// without, the old first line stands, the question follows unless the balance
// is known to be empty, and the date is added whenever there is one.
export function quotaWords(input, now = Date.now()) {
  const q = input && typeof input === 'object' ? input : {};
  const needs = count(q.needs);
  const has = count(q.has);
  const resetsAt = Number.isFinite(q.resetsAt) ? q.resetsAt : null;
  const deep = q.scope === 'deep';
  const numbers = needs !== null && has !== null;
  const empty = deep ? q.empty !== false : has === 0;
  const heading = numbers
    ? (deep ? copy.QUOTA_DEEP_NEEDS : copy.QUOTA_CALL_NEEDS)(needs, has)
    : deep && !empty
      ? copy.QUOTA_SHORT
      : copy.QUOTA_EMPTY;
  const lines = [];
  if (resetsAt !== null) lines.push(copy.quotaResets(resetsAt, now));
  if (!numbers && !empty) lines.push(copy.QUOTA_NEXT);
  return { heading, lines };
}
