// WHEN the card tells the chat, for a host that has no silent channel.
//
// Claude takes the result through `ui/update-model-context` and the user sees
// nothing, so a push per completion is free. ChatGPT has only `ui/message`,
// which lands as a user turn the reader watches appear — so five picked checks
// must not become five turns.
//
// The rule: a card sends when it has NO running checks left, covering
// everything that completed since its last message. One check is one message,
// as before. Five picked checks are one message when the fifth settles. A short
// debounce was considered and rejected: picked checks start together but can
// finish more than half a minute apart, so a few seconds coalesces nothing.
//
// The ceiling stops a stuck check from holding the others: if anything is still
// running CEILING_MS after the first completion, send what has completed and
// send the remainder once at the end. At most two messages per batch.
export const CEILING_MS = 150_000;

// A failed check is never announced, but it DOES stop the card waiting for it:
// otherwise one outage would hold four good verdicts back for the full ceiling.
export function createMessenger({ send, ledger, timer = setTimeout, cancel = clearTimeout }) {
  const running = new Set();
  const done = new Map();
  const sent = new Set();
  let ceiling = null;
  let ceilingFired = false;
  let sending = Promise.resolve();

  const clearCeiling = () => {
    if (ceiling !== null) {
      cancel(ceiling);
      ceiling = null;
    }
  };

  // Everything that has landed and has not been announced, in the order the
  // checks were STARTED — never the order they finished, or a picker's rows
  // would be renumbered by luck.
  const pending = () =>
    [...done.values()].filter((check) => !sent.has(check.verificationId)).sort((a, b) => a.order - b.order);

  function flush() {
    const checks = pending();
    if (!checks.length) return;
    // RESERVE first, against a ledger shared by every card in this
    // conversation and re-read now rather than cached: another card may have
    // announced one of these since this one mounted. What comes back is what
    // nobody else has claimed.
    const mine = ledger ? ledger.reserve(checks.map((check) => check.verificationId)) : checks.map((c) => c.verificationId);
    const allowed = new Set(mine);
    const announce = checks.filter((check) => allowed.has(check.verificationId));
    // Marked either way: a check somebody else announced must not be retried
    // here, and a send that fails is not retried at all — a user turn is not
    // something to push into someone's chat twice.
    for (const check of checks) sent.add(check.verificationId);
    if (!announce.length) return;
    sending = sending.then(() => send(announce)).catch(() => {});
  }

  return {
    started(key) {
      if (!running.size) ceilingFired = false;
      running.add(key);
    },
    // A check that produced a verdict. `order` is its position in the card.
    completed(key, check) {
      running.delete(key);
      if (check && check.verificationId) done.set(check.verificationId, check);
      if (!running.size) {
        clearCeiling();
        flush();
        return;
      }
      // Still waiting on others: hold, but not forever. Once this batch's
      // ceiling has fired, it is NOT re-armed — re-arming let a third
      // completion produce a third message. Everything
      // left waits for the batch to settle.
      if (ceiling === null && !ceilingFired && pending().length) {
        ceiling = timer(() => {
          ceiling = null;
          ceilingFired = true;
          flush();
        }, CEILING_MS);
      }
    },
    // A check that ended without a verdict. Nothing is announced for it.
    failed(key) {
      running.delete(key);
      if (!running.size) {
        clearCeiling();
        flush();
      }
    },
    dispose: clearCeiling,
    // For tests and for the card's own guards.
    state: () => ({ running: running.size, pending: pending().length, sent: [...sent], ceilingFired }),
  };
}
