// Per-row persistence across a card's teardown and reopen.
//
// MCP Apps has no view state, and a tools/call's JSON-RPC id repeats across
// sessions, so the only place a card can remember a started check is the
// sandbox origin's localStorage (per conversation, per the spec's `domain`
// note), when the sandbox allows it at all. The record holds ids only
// ({taskId, verificationId}, plus the row's own claim and quick verdict to tell
// a hash collision apart); recovery reads the ids. What the model has already
// been told is NOT per row — one card pushes one cumulative snapshot — so it
// lives in a card-level record (createCardStore). Keyed by the row's claim + quick verdict, so the same card re-mounted
// in the same conversation finds its own check.
//
// Records expire: a task id after a day (a run's progress is short-lived, so an
// older id is not worth polling), a verification id after a week (the same
// claim asked again later gets the button, not an old result).

const PREFIX = 'lenz-card:v1:';
const TASK_ID = /^[A-Za-z0-9_-]{1,128}$/;
const VERIFICATION_ID = /^[0-9a-f]{8}$/;
const DAY_MS = 24 * 3600 * 1000;
export const TASK_RECORD_TTL_MS = DAY_MS;
export const VERIFICATION_RECORD_TTL_MS = 7 * DAY_MS;

// FNV-1a over UTF-16 code units, hex: short, stable, not a secret.
function fnv1a(s, seed = 0x811c9dc5) {
  let h = seed;
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, '0');
}

// 64 bits, from two independent rounds. A 32-bit key was cheap to collide by
// hand, and a collision does not only mis-read: the second row's save REPLACES
// the first row's record, losing a paid check's ids. Reads still compare the
// claim itself, so a collision can never show another row's result.
export function rowKey(claim, quickVerdict) {
  const text = `${claim}\u0000${quickVerdict}`;
  return `${PREFIX}${fnv1a(text)}${fnv1a(text, 0x01000193)}`;
}

function usable(storage) {
  if (!storage) return false;
  try {
    const probe = `${PREFIX}probe`;
    storage.setItem(probe, '1');
    storage.removeItem(probe);
    return true;
  } catch (_e) {
    return false;
  }
}

function clean(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const rec = {};
  if (typeof raw.taskId === 'string' && TASK_ID.test(raw.taskId)) rec.taskId = raw.taskId;
  if (typeof raw.verificationId === 'string' && VERIFICATION_ID.test(raw.verificationId)) {
    rec.verificationId = raw.verificationId;
  }
  return rec.taskId || rec.verificationId ? rec : null;
}

export function createRowStore(storage, { claim, quickVerdict, now = () => Date.now() }) {
  const available = usable(storage);
  const key = rowKey(claim, quickVerdict);
  const clear = () => {
    try {
      storage.removeItem(key);
    } catch (_e) {
      /* best effort */
    }
  };
  return {
    available,
    load() {
      if (!available) return null;
      let raw;
      try {
        raw = JSON.parse(storage.getItem(key));
      } catch (_e) {
        return null;
      }
      const rec = clean(raw);
      if (!rec) return null;
      // The key is a 32-bit hash: the record also names the row it belongs to,
      // so a colliding claim never recovers another claim's check.
      if (!raw || raw.claim !== claim || raw.quickVerdict !== quickVerdict) return null;
      const savedAt = raw && Number.isFinite(raw.savedAt) ? raw.savedAt : 0;
      const ttl = rec.verificationId ? VERIFICATION_RECORD_TTL_MS : TASK_RECORD_TTL_MS;
      if (now() - savedAt > ttl) {
        clear();
        return null;
      }
      return rec;
    },
    save(value) {
      if (!available) return;
      const rec = clean(value);
      try {
        if (rec) storage.setItem(key, JSON.stringify({ ...rec, claim, quickVerdict, savedAt: now() }));
      } catch (_e) {
        /* quota or policy: persistence is best effort */
      }
    },
    clear() {
      if (available) clear();
    },
  };
}

// The card's own record: how many checks the model has already been told about.
// Keyed on the whole row set, so a card showing other claims has its own count,
// and validated against that set on read, like a row record is.
export function createCardStore(storage, { rows, now = () => Date.now() }) {
  const joined = (Array.isArray(rows) ? rows : []).join('\u0001');
  // `joined` first: asking whether storage works is itself a write, and a card
  // with no rows to remember must not make one.
  const available = !!joined && usable(storage);
  const key = `${PREFIX}card:${fnv1a(joined)}${fnv1a(joined, 0x01000193)}`;
  return {
    available,
    load() {
      if (!available) return 0;
      try {
        const raw = JSON.parse(storage.getItem(key));
        if (!raw || raw.rows !== joined) return 0;
        if (now() - (Number.isFinite(raw.savedAt) ? raw.savedAt : 0) > VERIFICATION_RECORD_TTL_MS) {
          storage.removeItem(key);
          return 0;
        }
        return Number.isInteger(raw.pushedVersion) && raw.pushedVersion > 0 ? raw.pushedVersion : 0;
      } catch (_e) {
        return 0;
      }
    },
    save(pushedVersion) {
      if (!available || !Number.isInteger(pushedVersion) || pushedVersion <= 0) return;
      try {
        // Keep whatever else the record holds: the announced ids live here too.
        storage.setItem(key, JSON.stringify({ ...read(), pushedVersion, rows: joined, savedAt: now() }));
      } catch (_e) {
        /* best effort */
      }
    },
  };

  // The record as it stands, or {} — never another row-set's record.
  function read() {
    try {
      const raw = JSON.parse(storage.getItem(key));
      return raw && raw.rows === joined ? raw : {};
    } catch (_e) {
      return {};
    }
  }
}

// store.js imports nothing on purpose. A non-string field is not coerced: it
// reads as absent, because String({toString: null}) throws.
function str(value) {
  return typeof value === 'string' ? value.trim() : '';
}

// The same shape the server enforces. A child id arrives from the API and goes
// straight back out to it, so it is validated on the way in AND on the way out
// of storage: a record written by an older bundle is not to be trusted either.
const SENDABLE_ID = /^[A-Za-z0-9_-]{1,128}$/;
const sendable = (value) => SENDABLE_ID.test(str(value));

function cleanPicks(picks) {
  const seen = new Set();
  return (Array.isArray(picks) ? picks : [])
    .map((pick) => ({ claim: str(pick && pick.claim), taskId: str(pick && pick.taskId) }))
    .filter((pick) => {
      if (!pick.claim || !sendable(pick.taskId) || seen.has(pick.taskId)) return false;
      seen.add(pick.taskId);
      return true;
    });
}

const EMPTY_PICKS = Object.freeze({ picks: [], requested: [] });

// What the reader picked, and what it started. The picker's payload
// never changes on a re-mount, so without this record a reopened card shows the
// picker again over checks that are already running and already paid for. Keyed
// on the PARENT task_id, which is what identifies this one selection.
export function createPickStore(storage, { parent, now = () => Date.now() }) {
  const id = str(parent);
  const available = !!id && usable(storage);
  const key = `${PREFIX}picks:${fnv1a(id)}${fnv1a(id, 0x01000193)}`;
  return {
    available,
    load() {
      if (!available) return EMPTY_PICKS;
      try {
        const raw = JSON.parse(storage.getItem(key));
        if (!raw || raw.parent !== id) return EMPTY_PICKS;
        // A started check is a task, so it expires with the task records.
        if (now() - (Number.isFinite(raw.savedAt) ? raw.savedAt : 0) > TASK_RECORD_TTL_MS) {
          storage.removeItem(key);
          return EMPTY_PICKS;
        }
        return { picks: cleanPicks(raw.picks), requested: (Array.isArray(raw.requested) ? raw.requested : []).map(str).filter(Boolean) };
      } catch (_e) {
        return EMPTY_PICKS;
      }
    },
    // `requested` is what the reader ticked, saved BEFORE anything is started:
    // a reply that never arrives must not let the selection change underneath
    // the idempotency key, or the same claim is paid for twice.
    save({ picks = [], requested = [] } = {}) {
      if (!available) return;
      const picked = cleanPicks(picks);
      const asked = (Array.isArray(requested) ? requested : []).map(str).filter(Boolean);
      if (!picked.length && !asked.length) return;
      try {
        storage.setItem(key, JSON.stringify({ parent: id, picks: picked, requested: asked, savedAt: now() }));
      } catch (_e) {
        /* a full or refusing storage is not worth an error to the reader */
      }
    },
  };
}

// What this CONVERSATION has already announced in the chat, by verification id.
//
// Not on the card record, and not keyed by row set: two
// cards showing the same check have different row sets and therefore different
// card records, so each would announce it; and a picker suppresses its card
// record entirely (its payload is a model-run shape), so a reload re-announced
// everything it had started. A ui/message is sent as the USER's turn, so a
// duplicate is visible and cannot be taken back.
//
// The ledger is re-read at RESERVE time, never cached at construction, because
// a second card may have written it since. Two frames could still interleave
// between the read and the write — localStorage offers no atomic test-and-set
// and the frames are separate JS contexts — so this closes the sequential case
// (which is every real one measured) and not a same-instant tie.
const ANNOUNCED_KEY = `${PREFIX}announced`;

export function createAnnouncedStore(storage, { now = () => Date.now() } = {}) {
  const available = usable(storage);
  const read = () => {
    if (!available) return {};
    try {
      const raw = JSON.parse(storage.getItem(ANNOUNCED_KEY));
      if (!raw || typeof raw !== 'object') return {};
      const fresh = {};
      for (const [id, at] of Object.entries(raw)) {
        // A verification id outlives a task: the same week's window as a row's.
        if (typeof id === 'string' && Number.isFinite(at) && now() - at <= VERIFICATION_RECORD_TTL_MS) {
          fresh[id] = at;
        }
      }
      return fresh;
    } catch (_e) {
      return {};
    }
  };
  return {
    available,
    announced: () => Object.keys(read()),
    // Claim these ids for THIS card. Returns the ones it may announce: any the
    // ledger already holds belong to whoever announced them first.
    reserve(ids) {
      const wanted = (Array.isArray(ids) ? ids : []).map(str).filter(Boolean);
      if (!wanted.length) return [];
      if (!available) return wanted;
      const held = read();
      const mine = wanted.filter((id) => !(id in held));
      if (!mine.length) return [];
      try {
        const at = now();
        for (const id of mine) held[id] = at;
        storage.setItem(ANNOUNCED_KEY, JSON.stringify(held));
      } catch (_e) {
        /* a refusing storage means no protection, not no message */
      }
      return mine;
    },
  };
}
