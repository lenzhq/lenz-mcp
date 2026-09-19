// Pure formatting: verdict inks, the stage a status reports, elapsed time, URLs.
// No DOM, no host: unit-tested with `node --test`.

import { STAGE_LABELS } from '../copy.js';

const VERDICT_KEYS = {
  true: 'true',
  'mostly true': 'mostly-true',
  mixed: 'mixed',
  'mostly false': 'mostly-false',
  false: 'false',
};

// The CSS modifier for a verdict word's ink; anything else reads as neutral.
export function verdictKey(verdict) {
  return VERDICT_KEYS[String(verdict || '').trim().toLowerCase()] || 'unknown';
}

// Every value here is attacker-controlled: an object whose `toString` is null
// THROWS on String(), so nothing is coerced — a non-string reads as absent.
export function confidenceBucket(confidence) {
  const c = text(confidence).toLowerCase();
  return c === 'high' || c === 'medium' || c === 'low' ? c : 'medium';
}

// The stage label for a status step, or null. An unknown or `starting` step
// never prints a raw pipeline name: the caller keeps the last known stage.
export function stageLabel(step) {
  const key = text(step).toLowerCase();
  return Object.prototype.hasOwnProperty.call(STAGE_LABELS, key) ? STAGE_LABELS[key] : null;
}

export function formatElapsed(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, '0')}`;
}

// Only http(s) links are ever opened, and only through the host.
export function isWebUrl(url) {
  if (typeof url !== 'string') return false;
  const trimmed = url.trim();
  if (!/^https?:\/\//i.test(trimmed)) return false;
  try {
    const parsed = new URL(trimmed);
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:') && Boolean(parsed.hostname);
  } catch (_e) {
    return false;
  }
}

export function domainOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch (_e) {
    return '';
  }
}

// A payload string as display text: a string, trimmed, or ''. Never markup:
// every value reaches the page as a text node.
export function text(value) {
  return typeof value === 'string' ? value.trim() : '';
}

export function score(value) {
  return Number.isInteger(value) && value >= 1 && value <= 10 ? value : null;
}
