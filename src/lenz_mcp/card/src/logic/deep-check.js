// The deep check a card starts: one row's state machine. Pure: the host, the
// clock and the store are injected, so the whole lifecycle is unit-tested.
//
// Rules:
// - Nothing is submitted except by start(), which the card calls from a real
//   click. recover() and polling only ever READ.
// - start() is idempotent while a check is submitting, running or done; the
//   API also joins a same-key resubmit, so a double click cannot charge twice.
// - Polls never overlap, back off on errors, stop at the cap, and a poll error
//   never triggers a new submission.
// - Every answer is matched to the task it was asked about; a late one for
//   another task, or after dispose(), is dropped.
// - A reopened card recovers by the ids it stored, never by claim text.

import { stageLabel } from './format.js';

export const POLL_MS = 3000;
export const LONG_POLL_MS = 10000;
export const LONG_RUN_SECONDS = 180;
export const MAX_POLL_ERRORS = 5;
export const MAX_BACKOFF_MS = 30000;

const START_TOOL = 'start_verification_widget';
const POLL_TOOL = 'get_verification_widget';
// A failed submission that is worth pressing "Try again" for.
const RETRYABLE_START_STATUSES = new Set(['service_unavailable', 'rate_limited', 'error', 'in_progress']);

// `claim` is what a card-started check submits; a model-started one adopts a
// task_id instead and never submits. The depth is the server's one constant
// (src/lenz_mcp/config.py CARD_VERIFY_DEPTH), so changing it needs no card release.
export function createDeepCheck({ claim, host, store, clock, onChange = () => {} }) {
  let state = { kind: 'idle' };
  let disposed = false;
  let timer = null;
  let polling = false;
  let starting = false;

  const set = (next) => {
    state = next;
    if (!disposed) onChange(state);
  };

  // The store keeps one record per card row and REPLACES it on each save.
  const save = (value) => {
    try {
      if (store) store.save(value);
    } catch (_e) {
      /* storage is best effort */
    }
  };

  const schedule = (ms) => {
    if (disposed) return;
    if (timer !== null) clock.clearTimeout(timer);
    timer = clock.setTimeout(() => {
      timer = null;
      poll();
    }, ms);
  };

  const call = async (name, args) => {
    try {
      const out = await host.callTool(name, args);
      return out && typeof out === 'object' ? out : null;
    } catch (_e) {
      return null;
    }
  };

  const running = (taskId, extra = {}) => ({
    kind: 'running',
    taskId,
    stage: null,
    index: 0,
    total: 5,
    elapsed: 0,
    errors: 0,
    ...extra,
  });

  async function poll() {
    if (disposed || polling || state.kind !== 'running') return;
    const taskId = state.taskId;
    polling = true;
    const out = await call(POLL_TOOL, { task_id: taskId });
    polling = false;
    if (disposed || state.kind !== 'running' || state.taskId !== taskId) return;

    const status = out && out.status;
    if (status === 'processing') {
      const elapsed = Number.isInteger(out.elapsed_seconds) ? out.elapsed_seconds : state.elapsed;
      set({
        ...state,
        stage: stageLabel(out.step) || state.stage,
        index: Number.isInteger(out.index) && out.index > 0 ? out.index : state.index,
        total: Number.isInteger(out.total) && out.total > 0 ? out.total : state.total,
        elapsed,
        errors: 0,
      });
      schedule(elapsed >= LONG_RUN_SECONDS ? LONG_POLL_MS : POLL_MS);
      return;
    }
    if (status === 'completed') {
      const verificationId = typeof out.verification_id === 'string' ? out.verification_id : '';
      const storedTaskId = state.recovering ? state.storedTaskId : taskId;
      save(verificationId ? { taskId: storedTaskId, verificationId } : { taskId: storedTaskId });
      set({ kind: 'completed', taskId, result: out, recovered: !!state.recovering });
      return;
    }
    if (status === 'failed') {
      set({
        kind: 'failed',
        taskId,
        failureClass: typeof out.failure_class === 'string' ? out.failure_class : '',
        retryable: out.retryable === true,
      });
      return;
    }
    if (status === 'needs_input') {
      set({ kind: 'failed', taskId, failureClass: 'needs_input', retryable: false });
      return;
    }
    if (status === 'not_found') {
      // Gone for good: forget it, so the next mount offers the button again.
      try {
        if (store && store.clear) store.clear();
      } catch (_e) {
        /* best effort */
      }
      set({ kind: 'unrecoverable' });
      return;
    }
    // A thrown call or an error envelope: a blip until the cap says otherwise.
    const errors = state.errors + 1;
    if (errors >= MAX_POLL_ERRORS) {
      set({ kind: 'unavailable', taskId });
      return;
    }
    set({ ...state, errors });
    schedule(Math.min(POLL_MS * 2 ** errors, MAX_BACKOFF_MS));
  }

  async function start() {
    const retryable = state.kind === 'failed' && state.retryable;
    if (disposed || starting || !(state.kind === 'idle' || retryable)) return;
    starting = true;
    const args = { claim };
    // The failed run this press retries. Kept through a start whose answer is
    // lost, so the next press sends the same retry_of, reaches the same
    // idempotency key, and joins a retry that may already be running.
    const retryOf = retryable && state.taskId ? state.taskId : null;
    if (retryOf) args.retry_of = retryOf;
    set({ kind: 'submitting' });
    const out = await call(START_TOOL, args);
    starting = false;

    const status = out && out.status;
    const accepted = status === 'submitted' && typeof out.task_id === 'string' && out.task_id;
    // An accepted, paid run is remembered even if the card was torn down while
    // waiting: a reopened card recovers it instead of offering another start.
    if (accepted) save({ taskId: out.task_id });
    if (disposed) return;

    if (accepted) {
      set(running(out.task_id));
      schedule(POLL_MS);
      return;
    }
    if (status === 'quota_exhausted') {
      set({ kind: 'quota', empty: out.balance_empty !== false });
      return;
    }
    set({ kind: 'failed', taskId: retryOf, failureClass: '', retryable: !out || RETRYABLE_START_STATUSES.has(status) });
  }

  // `onAdopted` fires the moment this takes work on, BEFORE the first poll —
  // not after. The caller counts outstanding checks, and the poll can resolve
  // inside this call: a signal sent afterwards would arrive after the
  // completion and leave the card waiting for a check that had already landed.
  // A card with nothing to recover never fires it.
  async function recover({ onAdopted } = {}) {
    if (disposed || state.kind !== 'idle' || !store) return;
    let rec = null;
    try {
      rec = store.load();
    } catch (_e) {
      rec = null;
    }
    if (!rec) return;
    if (typeof rec.verificationId === 'string' && /^[0-9a-f]{8}$/.test(rec.verificationId)) {
      set(running(rec.verificationId, { recovering: true, storedTaskId: rec.taskId || null }));
    } else if (typeof rec.taskId === 'string' && rec.taskId) {
      set(running(rec.taskId));
    } else {
      return;
    }
    if (onAdopted) onAdopted();
    await poll();
  }

  // A run the MODEL started: the card is handed its task_id in the tool result
  // and adopts it. No store, no start call, and a late answer is dropped the
  // same way (the poll checks the task_id it began with).
  async function adopt(taskId, hint = {}) {
    if (disposed || state.kind !== 'idle' || typeof taskId !== 'string' || !taskId) return;
    set(running(taskId, hint));
    await poll();
  }

  return {
    state: () => state,
    start,
    adopt,
    recover,
    pollNow: () => poll(),
    dispose() {
      disposed = true;
      if (timer !== null) clock.clearTimeout(timer);
      timer = null;
    },
  };
}
