// A citation check that did not finish → the frame the card shows. Pure.
//
// What reaches the card (server.py):
// - a failed check (`_finished_citecheck`): `status: 'failed'`, `failure_reason`
//   (the API's `failure.code`), `failure_class`, `retryable`, `credits_charged`;
// - a start the API refused (`_citecheck_submit_error`, a 422):
//   `status: 'invalid_request'` and the API's `code`;
// - a read of the check (`_citecheck_result`): `status: 'not_found'` (a 404),
//   with `gone: true` (a 410), or `status: 'error'` (any other refusal).
// Older payloads named the code `failure.code`, `error_code` or `code`, and
// spelled no citations `no_checkable_citations`; both are still read.
//
// The server's `message` and `next_step` are written for the model and are
// never shown: every line here is a sentence of the card's own (copy.js).

import * as copy from '../copy.js';
import { text } from './format.js';

const NO_CITATIONS = new Set(['no_citations', 'no_checkable_citations']);

export function citecheckFailure(data) {
  const d = data && typeof data === 'object' ? data : {};
  const failure = d.failure && typeof d.failure === 'object' ? d.failure : {};
  const code = text(d.failure_reason) || text(failure.code) || text(d.error_code) || text(d.code);
  const failureClass = text(d.failure_class) || text(failure.failure_class);
  const retryable = typeof d.retryable === 'boolean' ? d.retryable : failure.retryable;

  if (d.status === 'not_found' && d.gone === true) {
    return { heading: copy.CITECHECK_GONE_HEADING, lines: [copy.CITECHECK_GONE_BODY, copy.CITECHECK_ASK_AGAIN] };
  }
  if (d.status === 'not_found' || d.status === 'error') {
    return { heading: copy.CITECHECK_UNREADABLE_HEADING, lines: [copy.CITECHECK_ASK_AGAIN] };
  }

  // Only a failed check carries its own charge; 0 is the ledger's net, read with it.
  const isCheck = d.status === 'failed';
  const charged = isCheck && d.credits_charged === 0 ? [copy.NOTHING_CHARGED] : [];
  // The same input replays the failed check (same idempotency key, kept a day).
  const replays = isCheck ? [copy.CITECHECK_SAME_RESULT] : [];

  if (NO_CITATIONS.has(code)) {
    const line = charged.length ? copy.CITECHECK_WHAT_COUNTS_UNCHARGED : copy.CITECHECK_WHAT_COUNTS;
    return { heading: copy.CITECHECK_NO_CITATIONS_HEADING, lines: [line] };
  }
  if (d.status === 'invalid_request' || failureClass === 'invalid_input' || code === 'invalid_input') {
    return { heading: copy.CITECHECK_INVALID_HEADING, lines: [copy.CITECHECK_SEND_FULL_TEXT, ...charged] };
  }
  // A failure on Lenz's side that can pass. Told to try again only because the
  // API stops replaying it to the same request after a short window (copy.js).
  if (isCheck && (retryable === true || code === 'upstream_unavailable' || failureClass === 'upstream_unavailable')) {
    const line = d.credits_charged === 0 ? copy.CITECHECK_RETRY_LATER_UNCHARGED : copy.CITECHECK_RETRY_LATER;
    return { heading: copy.CITECHECK_RETRY_HEADING, lines: [line] };
  }
  return { heading: copy.FAILED_HEADING, lines: [copy.CITECHECK_OUR_SIDE, ...charged, ...replays] };
}
