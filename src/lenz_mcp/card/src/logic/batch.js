// The list card's batch: the flagged rows one press of "Check all N flagged
// against sources" started, and how far they have got. Pure: the list card
// keeps the state, rows report into it, and this module decides what it means.

// The kinds a row's deep check can STOP in (logic/deep-check.js): a verdict, or
// one of the ways it ends without one.
const STOPPED = new Set(['completed', 'failed', 'quota', 'unavailable', 'unrecoverable']);
export const isStopped = (run) => !!run && STOPPED.has(run.kind);

// A row reports its check on every change, with `attempt`: how many times the
// reader pressed that row's own button (its "Try again" included). A stopped
// check stays stopped unless a NEWER attempt reports: a row mounted afresh starts
// again at attempt 0 and reports "idle", then "running" while it recovers the
// check it already finished, and that must never make the count go back; a
// press of "Try again" is a new attempt, and its check is running again.
export function recordRun(runs, index, run) {
  if (!run || typeof run.kind !== 'string') return runs;
  const prev = runs[index];
  const attempt = (r) => (r && Number.isInteger(r.attempt) ? r.attempt : 0);
  if (isStopped(prev) && !isStopped(run) && attempt(run) <= attempt(prev)) return runs;
  if (prev && prev.kind === run.kind && prev.elapsed === run.elapsed && attempt(prev) === attempt(run)) return runs;
  return { ...runs, [index]: run };
}

// How far the batch is: how many of its checks have stopped, whether any is
// still running, and the longest any has run (the server's own count, seconds).
export function batchProgress(batch, runs) {
  const rows = Array.isArray(batch) ? batch : [];
  const done = rows.filter((i) => isStopped(runs[i])).length;
  const elapsed = rows.reduce((most, i) => Math.max(most, (runs[i] && Number(runs[i].elapsed)) || 0), 0);
  return { total: rows.length, done, running: rows.length > 0 && done < rows.length, elapsed };
}

// What the batch found, as rows the list's tally can count: a check with a
// verdict counts by that verdict, one that stopped without one as could not be checked.
export function batchRows(batch, runs, verdictOf) {
  return (Array.isArray(batch) ? batch : []).map((i) => {
    const verdict = runs[i] && runs[i].kind === 'completed' ? verdictOf(i) : '';
    return verdict ? { verdict } : { error: 'stopped' };
  });
}

// A row of a reopened batch whose own record was gone by the time it looked
// (another tab cleared it): it has nothing to recover and would hold the batch
// open forever, so it leaves the batch. A pressed batch's rows have started
// their checks; only a restored one can lose a row this way.
export function withoutRow(batch, index) {
  if (!Array.isArray(batch) || !batch.includes(index)) return batch;
  const rest = batch.filter((i) => i !== index);
  return rest.length ? rest : null;
}

// What is said when the batch ends: the summary, joined to what the row that
// ended it has just said (its result). The status region holds one message, and
// a second one now would replace that result before it is read. The row is the
// batch row whose check stopped last (`stoppedAt`: row -> order of its last
// stop); another row's message, outside the batch, is never the one joined.
export function batchEndMessage(batch, said, stoppedAt, summary) {
  const rows = Array.isArray(batch) ? batch : [];
  let last = -1;
  for (const i of rows) if (Number.isInteger(stoppedAt[i]) && (last < 0 || stoppedAt[i] > stoppedAt[last])) last = i;
  const own = last >= 0 && typeof said[last] === 'string' ? said[last] : '';
  return own ? `${own}. ${summary}` : summary;
}

// A saved batch, read back on a reopened card: row positions only, each a row
// that exists and has a record of its own check (`hasCheck`). A row with no
// record left has nothing to recover and would hold the batch open forever.
export function restoreBatch(saved, rowCount, hasCheck) {
  if (!Array.isArray(saved)) return null;
  const seen = new Set();
  const rows = saved.filter((i) => {
    if (!Number.isInteger(i) || i < 0 || i >= rowCount || seen.has(i)) return false;
    seen.add(i);
    return hasCheck(i);
  });
  return rows.length ? rows : null;
}
