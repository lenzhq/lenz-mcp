// Tool result → what the card shows. Pure.
//
// The card is attached to `assess_claim`, so the payload is the quick
// check's structuredContent: `{status: 'ok', claims: [...]}`, `no_claim`, or an
// error envelope (`auth_required`, `quota_exhausted`, `rate_limited`,
// `service_unavailable`, `invalid_request`, `error`, …).

import { confidenceBucket, stageLabel, text, verdictKey } from './format.js';

// A task id we can send back: the card never invents one, and an unsendable one
// is not a run (the same guard the server applies).
const SENDABLE_ID = /^[A-Za-z0-9_-]{1,128}$/;
// Ids the card RECEIVES are checked too, not just the ones it reads off a
// payload: a child task id from a select response is sent straight back out.
export const isSendableId = (value) => typeof value === 'string' && SENDABLE_ID.test(value);

// A count of seconds from the server, or 0. Never coerced: the value is
// attacker-controlled like every other field.
function wholeSeconds(value) {
  return Number.isInteger(value) && value > 0 ? value : 0;
}

function quickRow(entry) {
  const e = entry && typeof entry === 'object' ? entry : {};
  const error = text(e.error) || (text(e.verdict) === 'Error' ? 'error' : '');
  return {
    claim: text(e.claim),
    verdict: text(e.verdict),
    confidence: confidenceBucket(e.confidence),
    rationale: text(e.rationale),
    dissent: text(e.dissent),
    recommend: e.recommend_verify === true,
    error,
    hint: text(e.hint),
  };
}

// One entry point: a quick check's payload (assess) or a deep check's (verify /
// get_verification, which the MODEL ran). The card never guesses from the tool
// name, only from the shape it was handed.
// A deep check that is still going says `submitted` from verify_claim and
// `processing` from get_verification (server.py: two words, one situation), and
// the card must adopt the run on either. Reading only one shows a paid, running
// check as "did not finish".
const DEEP_RUNNING = new Set(['submitted', 'processing']);
const DEEP_STATUSES = new Set(['completed', 'needs_input', ...DEEP_RUNNING]);

// True when the MODEL ran this check, not the card. Such a card pushes nothing
// and stores nothing, so it must not even open the context store: probing
// whether storage works is itself a write.
export function modelRanIt(payload) {
  return !!payload && typeof payload === 'object' && DEEP_STATUSES.has(payload.status);
}

export function routePayload(payload) {
  if (!payload || typeof payload !== 'object') return { view: 'waiting' };
  if (modelRanIt(payload)) return routeDeep(payload);
  return routeQuick(payload);
}

// A deep check's own card. No "changed from the quick verdict"
// line here: the model started this one, so there is no quick verdict on screen,
// and nothing is pushed to the model, which already has the result.
export function routeDeep(payload) {
  if (payload.status === 'completed') return { view: 'deep', result: deepResult(payload) };
  if (DEEP_RUNNING.has(payload.status)) {
    const taskId = text(payload.task_id);
    if (!SENDABLE_ID.test(taskId)) return { view: 'did-not-finish' };
    const whole = (value) => (Number.isInteger(value) && value > 0 ? value : 0);
    return {
      view: 'deep-running',
      taskId,
      claim: text(payload.claim),
      stage: stageLabel(payload.step),
      index: whole(payload.index),
      total: whole(payload.total) || 5,
      elapsed: Number.isInteger(payload.elapsed_seconds) && payload.elapsed_seconds > 0 ? payload.elapsed_seconds : 0,
    };
  }
  // needs_input is the picker. `message` and `resolve_with` are
  // written for the model and are never shown. Without a task id we can send
  // back there is nothing to resolve, so the picker would be a dead end.
  const parent = text(payload.task_id);
  const claims = (Array.isArray(payload.claims) ? payload.claims : [])
    .map((c) => text(c && typeof c === 'object' ? c.text || c.claim : c))
    .filter(Boolean);
  if (!SENDABLE_ID.test(parent) || !claims.length) return { view: 'did-not-finish' };
  return { view: 'picker', taskId: parent, claims };
}

export function routeQuick(payload) {
  if (!payload || typeof payload !== 'object') return { view: 'waiting' };
  switch (payload.status) {
    case 'ok': {
      const rows = (Array.isArray(payload.claims) ? payload.claims : []).map(quickRow).filter((r) => r.claim);
      if (rows.length === 0) return { view: 'nothing', message: '' };
      if (rows.length === 1) {
        const row = rows[0];
        if (row.error) return { view: 'row-error', row };
        return { view: 'single', row };
      }
      return { view: 'list', rows };
    }
    case 'no_claim':
      return { view: 'nothing', message: text(payload.message) };
    case 'auth_required':
      return { view: 'reconnect' };
    // Retryable, and they share an answer: nothing was wrong with the claim,
    // nothing was charged, come back after the wait the server stated.
    case 'service_unavailable':
    case 'rate_limited':
      return { view: 'outage', retryAfter: wholeSeconds(payload.retry_after_seconds) };
    case 'quota_exhausted':
      return { view: 'quota' };
    case 'in_progress':
      return { view: 'in-progress' };
    // The picker's parent was resolved by someone else (the model, another card).
    case 'already_resolved':
      return { view: 'already-resolved' };
    default:
      return { view: 'did-not-finish' };
  }
}

// How strong the "Check against sources" button is: filled when Lenz
// recommends it (low confidence), outlined on medium or a dissent, a quiet text
// button on high.
export function buttonStrength(row) {
  if (row.recommend || row.confidence === 'low') return 'filled';
  if (row.confidence === 'medium' || row.dissent) return 'outlined';
  return 'quiet';
}

// A completed deep result as the card renders it. Every field is text or a
// number from a closed range; sources keep only http(s) links.
export function deepResult(payload) {
  const p = payload && typeof payload === 'object' ? payload : {};
  const sources = (Array.isArray(p.sources) ? p.sources : [])
    .filter((s) => s && typeof s === 'object')
    .map((s) => ({
      title: text(s.title),
      url: text(s.url),
      publisher: text(s.publisher),
      date: text(s.date),
      quote: text(s.quote),
    }))
    .filter((s) => s.title || s.url);
  const total = Number.isInteger(p.sources_total) && p.sources_total >= sources.length ? p.sources_total : sources.length;
  return {
    verificationId: /^[0-9a-f]{8}$/.test(text(p.verification_id)) ? text(p.verification_id) : '',
    claim: text(p.claim),
    verdict: text(p.verdict),
    score: Number.isInteger(p.lenz_score) && p.lenz_score >= 1 && p.lenz_score <= 10 ? p.lenz_score : null,
    confidence: confidenceBucket(p.confidence),
    keyFinding: text(p.key_finding),
    summary: text(p.executive_summary),
    warnings: (Array.isArray(p.warnings) ? p.warnings : []).map(text).filter(Boolean),
    sources,
    sourcesTotal: total,
  };
}

// One quiet line above the stamp, only when the deep verdict differs.
export function changedFrom(quickVerdict, deepVerdict) {
  const q = text(quickVerdict);
  const d = text(deepVerdict);
  return q && d && q.toLowerCase() !== d.toLowerCase() ? q : '';
}

// The list's tally: how many rows read wrong, mixed, fine, or could not be checked.
// Counted on each row's CURRENT verdict, so a deep check that changes one moves it.
export function tally(rows) {
  const counts = { wrong: 0, mixed: 0, hold: 0, error: 0 };
  for (const row of rows) {
    if (row.error || !row.verdict) {
      counts.error += 1;
      continue;
    }
    const key = verdictKey(row.verdict);
    if (key === 'false' || key === 'mostly-false') counts.wrong += 1;
    else if (key === 'true' || key === 'mostly-true') counts.hold += 1;
    else if (key === 'mixed') counts.mixed += 1;
    else counts.error += 1;
  }
  return counts;
}
