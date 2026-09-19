// The card's views. Rendering only:
// no host protocol here (that is host/mcp-apps.js) and no polling logic (that is
// logic/deep-check.js). Every payload value reaches the page as a text node:
// JSX children, never innerHTML.

import { Component } from 'preact';
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';

import * as copy from './copy.js';
import { createDeepCheck } from './logic/deep-check.js';
import { domainOf, formatElapsed, isWebUrl, text, verdictKey } from './logic/format.js';
import { buttonStrength, changedFrom, deepResult, isSendableId, routePayload, tally } from './logic/route.js';
import { DELIVER_MESSAGE, readDelivery } from './logic/delivery.js';
import { buildSnapshot } from './logic/snapshot.js';
import { createPickStore, createRowStore } from './logic/store.js';

const LIST_ROWS_SHOWN = 8;
const CAVEATS_SHOWN = 3;
// One press of the picker starts a paid deep check per claim, so it is capped:
// it bounds a mis-tap, and it matches what we tell the
// model — name the ones that matter, never deep-check every row. One constant,
// so the number can move in one place.
const PICKER_MAX_SELECTED = 5;
const PICKER_CLAIMS_SHOWN = 8;

function safeStorage() {
  try {
    return globalThis.localStorage;
  } catch (_e) {
    return undefined;
  }
}

function Footer({ right }) {
  return (
    <div class="lz-footer">
      <span>{copy.BRAND}</span>
      <span>{right}</span>
    </div>
  );
}

function Frame({ heading, lines = [], footer, eyebrow }) {
  return (
    <div class="lz">
      {eyebrow ? <p class="lz-eyebrow">{eyebrow}</p> : null}
      <h1 class="lz-claim" tabIndex={-1}>
        {heading}
      </h1>
      {lines.filter(Boolean).map((l, i) => (
        <p key={i} class="lz-body" style={{ marginTop: '8px' }}>
          {l}
        </p>
      ))}
      {footer ? <Footer right={footer} /> : null}
    </div>
  );
}

// At most ~6 lines, then "Show more"; the rationale and the dissent each clamp
// on their own. Whether the text overflows is measured, not guessed from its length:
// the same text is four lines at 735 px and nine at 320.
function Clamped({ id, children }) {
  const [open, setOpen] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || open) return undefined;
    const measure = () => setOverflows(el.scrollHeight > el.clientHeight + 1);
    measure();
    if (!globalThis.ResizeObserver) return undefined;
    const observer = new globalThis.ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [open]);
  return (
    <>
      <p class={`lz-body ${open ? '' : 'lz-clamp'}`} id={id} ref={ref}>
        {children}
      </p>
      {open || overflows ? (
        <button type="button" class="lz-link" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
          {open ? copy.SHOW_LESS : copy.SHOW_MORE}
        </button>
      ) : null}
    </>
  );
}

function Reasoning({ row }) {
  if (!row.rationale && !row.dissent) return null;
  return (
    <div class="lz-region first">
      {row.rationale ? (
        <div>
          <Clamped id="lz-rationale">
            {row.rationale}
          </Clamped>
          <p class="lz-label">{copy.REASONING_LABEL}</p>
        </div>
      ) : null}
      {row.dissent ? (
        <div class={row.rationale ? 'lz-region' : ''}>
          <Clamped id="lz-dissent">
            {copy.DISSENT_PREFIX}
            {row.dissent}
          </Clamped>
        </div>
      ) : null}
    </div>
  );
}

function Running({ state, headingRef, first = false, note = true }) {
  const long = state.elapsed >= 180;
  return (
    <div class={`lz-region lz-running ${first ? 'first' : ''}`}>
      <h2 ref={headingRef} tabIndex={-1}>
        {copy.RUNNING_HEADING}
      </h2>
      {state.kind === 'running' && state.stage && state.index > 0 ? (
        <p class="lz-meta">{copy.stepLine(state.stage, state.index, state.total)}</p>
      ) : null}
      {state.kind === 'running' && state.elapsed > 0 ? <p class="lz-meta">{formatElapsed(state.elapsed)}</p> : null}
      {note ? <p class="lz-note">{long ? copy.LONG_RUN : copy.USUALLY}</p> : null}
    </div>
  );
}

function CheckButton({ row, onPress, buttonRef }) {
  const strength = buttonStrength(row);
  const cls = strength === 'quiet' ? 'lz-link' : `lz-button ${strength}`;
  return (
    <div class="lz-actions">
      <button type="button" class={cls} ref={buttonRef} onClick={onPress}>
        {copy.CHECK_BUTTON}
      </button>
    </div>
  );
}

// What a check that did not finish says, as one sentence for the status region.
export function notFinishedText(state) {
  if (state.kind === 'quota') return `${state.empty ? copy.QUOTA_EMPTY : copy.QUOTA_SHORT} ${copy.QUOTA_NEXT}`;
  if (state.kind === 'unavailable') return copy.UNAVAILABLE;
  if (state.kind === 'unrecoverable') return copy.UNRECOVERABLE;
  if (state.retryable) return copy.OUTAGE;
  return `${copy.FAILED_HEADING} ${copy.FAILED_BY_CLASS[state.failureClass] || copy.FAILED_DEFAULT}`;
}

function NotFinished({ state, onRetry, focusRef }) {
  if (state.kind === 'quota') {
    return (
      <div class="lz-region">
        <p class="lz-body" tabIndex={-1} ref={focusRef}>{state.empty ? copy.QUOTA_EMPTY : copy.QUOTA_SHORT}</p>
        <p class="lz-body">{copy.QUOTA_NEXT}</p>
      </div>
    );
  }
  if (state.kind === 'unavailable') {
    return (
      <div class="lz-region">
        <p class="lz-body" tabIndex={-1} ref={focusRef}>{copy.UNAVAILABLE}</p>
      </div>
    );
  }
  if (state.kind === 'unrecoverable') {
    return (
      <div class="lz-region">
        <p class="lz-body" tabIndex={-1} ref={focusRef}>{copy.UNRECOVERABLE}</p>
      </div>
    );
  }
  // failed. A card that did not start the run cannot retry it: no claim to send.
  if (state.retryable && onRetry) {
    return (
      <div class="lz-region">
        <p class="lz-body" tabIndex={-1} ref={focusRef}>{copy.OUTAGE}</p>
        <div class="lz-actions">
          <button type="button" class="lz-link" onClick={onRetry}>
            {copy.TRY_AGAIN}
          </button>
        </div>
      </div>
    );
  }
  return (
    <div class="lz-region" role="alert">
      <p class="lz-body" tabIndex={-1} ref={focusRef}>{copy.FAILED_HEADING}</p>
      <p class="lz-body">{copy.FAILED_BY_CLASS[state.failureClass] || copy.FAILED_DEFAULT}</p>
    </div>
  );
}

function Stamp({ result }) {
  const key = verdictKey(result.verdict);
  const twoWords = result.verdict.trim().includes(' ');
  return (
    <div class={`lz-stamp v-${key}`}>
      <span class={`lz-stamp-word ${twoWords ? 'two-words' : ''}`}>{result.verdict}</span>
      {result.score != null ? (
        <>
          <span class="lz-stamp-score">
            {result.score}
            <span class="denom">/10</span>
          </span>
          <span class="lz-bar" role="img" aria-label={copy.scoreLabel(result.score)}>
            {Array.from({ length: 10 }, (_, i) => (
              <i key={i} class={i < result.score ? 'on' : ''} />
            ))}
          </span>
        </>
      ) : null}
    </div>
  );
}

function Caveats({ warnings }) {
  const [open, setOpen] = useState(false);
  if (!warnings.length) return null;
  const shown = open ? warnings : warnings.slice(0, CAVEATS_SHOWN);
  const hidden = warnings.length - CAVEATS_SHOWN;
  return (
    <div class="lz-region">
      <h2 class="lz-label">{copy.CAVEATS}</h2>
      <ul class="lz-list lz-caveats" role="list">
        {shown.map((w, i) => (
          <li key={i}>{w}</li>
        ))}
      </ul>
      {hidden > 0 ? (
        <button type="button" class="lz-link" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? copy.SHOW_LESS : copy.showMoreCaveats(hidden)}
        </button>
      ) : null}
    </div>
  );
}

// Three sources, then the rest behind one full-row disclosure (card-local). The
// snapshot pushed to the model carries all of them.
const SOURCES_SHOWN = 3;

function Sources({ result, host }) {
  const [open, setOpen] = useState(false);
  const all = result.sources;
  const list = open ? all : all.slice(0, SOURCES_SHOWN);
  const hidden = all.length - SOURCES_SHOWN;
  if (!all.length) {
    return (
      <div class="lz-region">
        <p class="lz-body">{copy.NO_SOURCES}</p>
      </div>
    );
  }
  const canOpen = host.capabilities.openLinks;
  return (
    <div class="lz-region">
      <h2 class="lz-label">{copy.sourcesLabel(result.sourcesTotal, list.length)}</h2>
      <ul class="lz-list lz-sources" id="lz-sources">
        {list.map((s, i) => {
          const title = s.title || domainOf(s.url);
          const meta = [s.publisher || domainOf(s.url), s.date].filter(Boolean).join(' · ');
          return (
            <li key={i}>
              {canOpen && isWebUrl(s.url) ? (
                <a
                  class="lz-link"
                  href={s.url}
                  rel="noopener noreferrer"
                  onClick={(event) => {
                    event.preventDefault();
                    host.openLink(s.url).catch(() => {});
                  }}
                >
                  {title}
                </a>
              ) : (
                <p class="lz-body">{title}</p>
              )}
              {meta ? <p class="lz-meta lz-source-meta">{meta}</p> : null}
              {s.quote ? <p class="lz-quote">{`“${s.quote}”`}</p> : null}
            </li>
          );
        })}
      </ul>
      {hidden > 0 ? (
        <button type="button" class="lz-link lz-row-toggle" aria-expanded={open} aria-controls="lz-sources" onClick={() => setOpen(!open)}>
          {open ? copy.SHOW_LESS : copy.showMoreSources(hidden)}
        </button>
      ) : null}
    </div>
  );
}

// `compact` is the same block inside a list row, which carries its own claim,
// verdict line and footer: one component, two frames.
function DeepResult({ result, quickVerdict, host, headingRef, compact = false }) {
  const was = changedFrom(quickVerdict, result.verdict);
  return (
    <div class={compact ? '' : 'lz'}>
      {compact ? null : <p class="lz-eyebrow">{copy.DEEP_EYEBROW}</p>}
      {compact ? null : (
        <h1 class="lz-claim" ref={headingRef} tabIndex={-1}>
          {result.claim}
        </h1>
      )}
      {was && !compact ? <p class="lz-meta lz-changed">{copy.changedFrom(was)}</p> : null}
      {compact ? null : <Stamp result={result} />}
      {compact ? null : <p class="lz-meta lz-conf">{copy.deepConfidence(result.confidence)}</p>}
      {result.keyFinding ? <p class="lz-finding">{result.keyFinding}</p> : null}
      {result.summary ? (
        <div class="lz-region first">
          <p class="lz-body">{result.summary}</p>
        </div>
      ) : null}
      <Caveats warnings={result.warnings} />
      <Sources result={result} host={host} />
      {/* "Ask a follow-up" PREFILLS the composer and the user finishes the
          sentence. A host that SENDS a message instead would post the
          fragment as a user turn, so the button is not offered there. */}
      {host.capabilities.message && result.verificationId && readDelivery(host) !== DELIVER_MESSAGE ? (
        <div class="lz-actions">
          <button
            type="button"
            class="lz-link"
            onClick={() => host.sendMessage(copy.followUpPrefill(result.verificationId)).catch(() => {})}
          >
            {copy.FOLLOW_UP}
          </button>
        </div>
      ) : null}
      {compact ? null : <Footer right={copy.deepFooter(result.sourcesTotal)} />}
    </div>
  );
}

const TERMINAL = new Set(['failed', 'quota', 'unavailable', 'unrecoverable']);

// One row's deep check: its own stored ids, its own state machine, its own
// announcements. The single card and every list row use this same hook.
function useDeepCheck({ row, host, announce, registerCompleted, label = '', store: given, checkKey = '', order = 0, reportState }) {
  const own = useMemo(
    () => (given === null ? null : createRowStore(safeStorage(), { claim: row.claim, quickVerdict: row.verdict })),
    [row.claim, row.verdict, given === null],
  );
  const store = given === null ? null : own;
  const [state, setState] = useState({ kind: 'idle' });
  const checkRef = useRef(null);

  useEffect(() => {
    const check = createDeepCheck({
      claim: row.claim,
      host,
      store,
      clock: { setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: (id) => clearTimeout(id) },
      onChange: setState,
    });
    checkRef.current = check;
    // A recovered check is OUTSTANDING work and the card must wait for it like
    // any other: otherwise a reopened card with several unfinished checks would
    // announce each result on its own as it lands. Only when recovery actually
    // takes something on: a card with nothing to recover would otherwise be
    // counted as waiting forever, and no message would ever be sent.
    check.recover({
      // Only when recovery actually adopts a check, and before its first poll:
      // a card with nothing to recover must not be counted as waiting, and a
      // signal sent after the poll could arrive after the result.
      onAdopted: () => reportState && checkKey && reportState(checkKey, 'running'),
    });
    return () => check.dispose();
  }, [row.claim, row.verdict]);

  // Announce stage changes and the result, once each, through the one status region.
  const lastStage = useRef('');
  useEffect(() => {
    if (state.kind === 'running' && state.stage && state.stage !== lastStage.current) {
      lastStage.current = state.stage;
      announce(copy.announceStage(state.stage, label));
    }
    if (TERMINAL.has(state.kind)) announce(copy.announceRow(notFinishedText(state), label));
    if (state.kind === 'completed') {
      const r = deepResult(state.result);
      announce(copy.announceResult(r.verdict, r.score, r.confidence, label));
      // A model-started card tells the model nothing: it ran the tool itself.
      // `checkKey` is the SAME identity `reportState` uses: the card counts what
      // is still running by it, so a completion under a different key would
      // never close the wait (and every check would post its own message).
      if (registerCompleted) registerCompleted(row, r, store, { recovered: !!state.recovered, checkKey, order });
    }
    // A check that ended WITHOUT a verdict still closes the card's wait: a
    // completion reports itself through registerCompleted.
    if (reportState && checkKey && TERMINAL.has(state.kind)) reportState(checkKey, 'failed');
  }, [state]);

  // Starting is reported HERE, synchronously, not from a rendered state: when a
  // poll answers at once React can coalesce 'running' away, and a card that
  // never heard "started" thinks nothing is outstanding and tells the chat
  // about each check separately.
  const began = () => {
    if (reportState && checkKey) reportState(checkKey, 'running');
  };
  return {
    state,
    start: () => {
      began();
      return checkRef.current && checkRef.current.start();
    },
    adopt: (taskId, hint) => {
      began();
      return checkRef.current && checkRef.current.adopt(taskId, hint);
    },
  };
}

function SingleCard({ row, host, announce, registerCompleted, reportState }) {
  const { state, start } = useDeepCheck({ row, host, announce, registerCompleted, checkKey: 'single', order: 0, reportState });
  const buttonRef = useRef(null);
  const runningHeading = useRef(null);
  const resultHeading = useRef(null);
  const terminalRef = useRef(null);
  const focusFollows = useRef(false);

  // Focus: a press moves focus to the running heading (the button is gone); when
  // the result replaces the running block, focus follows only if it was there.
  useEffect(() => {
    const doc = globalThis.document;
    const active = doc && doc.activeElement;
    // Only when focus is still in the card: someone typing in the host's composer
    // while the check runs keeps their cursor.
    const focusIsLost = !!doc && doc.hasFocus() && (!active || active === doc.body);
    if ((state.kind === 'submitting' || state.kind === 'running') && focusFollows.current && runningHeading.current && focusIsLost) {
      runningHeading.current.focus();
    }
    // The running heading that held focus is gone: focus goes to what replaced it.
    const focusOnRunning = !!doc && doc.hasFocus() && active === runningHeading.current;
    if (TERMINAL.has(state.kind) && focusFollows.current && terminalRef.current && (focusIsLost || focusOnRunning)) {
      terminalRef.current.focus();
    }
    if (state.kind === 'completed' && focusFollows.current && resultHeading.current) {
      if (focusIsLost || focusOnRunning) {
        resultHeading.current.focus();
      }
      focusFollows.current = false;
    }
  }, [state.kind]);

  const press = () => {
    focusFollows.current = true;
    start();
  };

  if (state.kind === 'completed') {
    return <DeepResult result={deepResult(state.result)} quickVerdict={row.verdict} host={host} headingRef={resultHeading} />;
  }

  const busy = state.kind === 'submitting' || state.kind === 'running';
  return (
    <div class="lz">
      <p class="lz-eyebrow">{copy.QUICK_EYEBROW}</p>
      <h1 class="lz-claim" tabIndex={-1}>
        {row.claim}
      </h1>
      <p class={`lz-verdict ${busy ? 'dimmed' : `v-${verdictKey(row.verdict)}`}`}>
        {row.verdict}
        {busy ? <span class="lz-verdict-label">{copy.QUICK_VERDICT_LABEL}</span> : null}
      </p>
      <p class="lz-meta lz-conf">{copy.CONFIDENCE_LINE[row.confidence]}</p>
      {busy ? null : <Reasoning row={row} />}
      {state.kind === 'idle' && host.capabilities.serverTools ? (
        <CheckButton row={row} onPress={press} buttonRef={buttonRef} />
      ) : null}
      {busy ? <Running state={state} headingRef={runningHeading} /> : null}
      {['failed', 'quota', 'unavailable', 'unrecoverable'].includes(state.kind) ? (
        <NotFinished state={state} onRetry={press} focusRef={terminalRef} />
      ) : null}
      <Footer right={copy.QUICK_FOOTER} />
    </div>
  );
}

// One list row. Collapsed: the claim, the
// row's CURRENT verdict, and only what is unusual about it in words. Open: the
// confidence line and the reasoning the single card shows, plus the action — and
// once its deep check has run, the deep verdict in place, with the full deep
// check block behind one disclosure. One row is open at a time; the others stay usable.
function Row({ index, row, host, open, hidden, onToggle, announce, registerCompleted, reportState, onVerdict }) {
  const label = copy.rowLabel(index + 1);
  const { state, start } = useDeepCheck({ row, host, announce, registerCompleted, label, checkKey: rowId(row), order: index, reportState });
  const [full, setFull] = useState(false);
  const panelId = `lz-row-${index}`;
  const headRef = useRef(null);
  const runningHeading = useRef(null);
  const terminalRef = useRef(null);
  const focusFollows = useRef(false);
  const result = state.kind === 'completed' ? deepResult(state.result) : null;
  const busy = state.kind === 'submitting' || state.kind === 'running';

  useEffect(() => {
    if (result) onVerdict(row, result.verdict);
  }, [result && result.verdict]);
  useEffect(() => {
    if (!open) setFull(false);
  }, [open]);

  // Focus follows the press, as it does on the single card: to the running line,
  // then to the row's own header, which by then carries the deep verdict.
  useEffect(() => {
    const doc = globalThis.document;
    const active = doc && doc.activeElement;
    const inCard = !!doc && doc.hasFocus();
    const focusIsLost = inCard && (!active || active === doc.body);
    if (!focusFollows.current) return;
    if (busy && runningHeading.current && focusIsLost) runningHeading.current.focus();
    const onRunning = inCard && active === runningHeading.current;
    if (TERMINAL.has(state.kind) && terminalRef.current && (focusIsLost || onRunning)) terminalRef.current.focus();
    if (result && headRef.current && (focusIsLost || onRunning)) {
      headRef.current.focus();
      focusFollows.current = false;
    }
  }, [state.kind]);

  const press = () => {
    focusFollows.current = true;
    start();
  };

  const verdict = result ? result.verdict : row.verdict;
  const markers = [
    row.confidence === 'low' ? copy.ROW_NOT_SURE : '',
    row.dissent ? copy.ROW_SPLIT : '',
  ].filter(Boolean);

  return (
    <li hidden={hidden || undefined}>
      <span class="num">{index + 1}</span>
      <div>
        <button type="button" class="lz-row-head" ref={headRef} aria-expanded={open} aria-controls={panelId} onClick={onToggle}>
          <span class="row-claim">{row.claim}</span>
          {row.error ? (
            <span class="lz-meta row-meta">{copy.ROW_ERROR[row.error] || copy.ROW_ERROR_DEFAULT}</span>
          ) : (
            <>
              <span class="row-line">
                <span class={`row-verdict ${busy ? 'dimmed' : `v-${verdictKey(verdict)}`}`}>{verdict}</span>
                {result && result.score != null ? <span class="lz-meta row-score">{`${result.score}/10`}</span> : null}
              </span>
              {/* The meta has its own line, but read aloud the row still runs "True 9/10 ·
                  Checked against 14 sources": the separator is there for a screen reader. */}
              {result ? (
                <span class="lz-meta row-meta">
                  <span class="lz-sr"> · </span>
                  {copy.rowSources(result.sourcesTotal)}
                </span>
              ) : null}
              {!result && markers.length ? (
                <span class="lz-meta row-meta">
                  <span class="lz-sr"> · </span>
                  {markers.join(' · ')}
                </span>
              ) : null}
            </>
          )}
        </button>
        {open ? (
          <div class="lz-row-panel" id={panelId}>
            {result && changedFrom(row.verdict, result.verdict) ? (
              <p class="lz-meta lz-row-changed">{copy.changedFrom(changedFrom(row.verdict, result.verdict))}</p>
            ) : null}
            {row.error ? <p class="lz-body">{row.hint}</p> : null}
            {/* The confidence belongs to the verdict the row is showing: the deep
                check's once it has one, the quick read's until then. */}
            {result ? <p class="lz-meta lz-conf">{copy.deepConfidence(result.confidence)}</p> : null}
            {result || row.error ? null : <p class="lz-meta lz-conf">{copy.CONFIDENCE_LINE[row.confidence]}</p>}
            {result ? (
              <>
                <div class="lz-actions">
                  <button type="button" class="lz-link" aria-expanded={full} onClick={() => setFull(!full)}>
                    {full ? copy.HIDE_FULL_CHECK : copy.SHOW_FULL_CHECK}
                  </button>
                </div>
                {full ? <DeepResult result={result} quickVerdict={row.verdict} host={host} compact /> : null}
              </>
            ) : (
              <>
                {busy ? null : <Reasoning row={row} />}
                {state.kind === 'idle' && !row.error && host.capabilities.serverTools ? (
                  <CheckButton row={row} onPress={press} />
                ) : null}
                {busy ? <Running state={state} headingRef={runningHeading} /> : null}
                {TERMINAL.has(state.kind) ? <NotFinished state={state} onRetry={press} focusRef={terminalRef} /> : null}
              </>
            )}
          </div>
        ) : null}
      </div>
    </li>
  );
}

const rowId = (row) => `${row.claim}\u0000${row.verdict}`;

function ListCard({ rows, host, announce, registerCompleted, reportState }) {
  const [allRows, setAllRows] = useState(false);
  const [openId, setOpenId] = useState('');
  // A row's deep verdict replaces its quick one in the tally as well as in the
  // row, remembered by the row's own identity: a position would follow the wrong
  // row when the payload changes under the card.
  const [deepVerdicts, setDeepVerdicts] = useState({});
  const hidden = rows.length - LIST_ROWS_SHOWN;
  const counted = rows.map((row) => {
    const deep = deepVerdicts[rowId(row)];
    return deep ? { ...row, verdict: deep, error: '' } : row;
  });
  const onVerdict = (row, verdict) =>
    setDeepVerdicts((prev) => (prev[rowId(row)] === verdict ? prev : { ...prev, [rowId(row)]: verdict }));

  return (
    <div class="lz">
      <div class="lz-head">
        <h1 class="lz-claim" tabIndex={-1}>
          {copy.listHeading(rows.length)}
        </h1>
        <p class="lz-body lz-tally">{copy.tallyLine(tally(counted))}</p>
      </div>
      <div class="lz-region lz-rows-region">
        <ol class="lz-list lz-rows">
          {rows.map((row, i) => (
            <Row
              key={rowId(row)}
              index={i}
              row={row}
              host={host}
              hidden={!allRows && i >= LIST_ROWS_SHOWN}
              open={openId === rowId(row)}
              onToggle={() => setOpenId(openId === rowId(row) ? '' : rowId(row))}
              announce={announce}
              registerCompleted={registerCompleted}
              reportState={reportState}
              onVerdict={onVerdict}
            />
          ))}
        </ol>
        {hidden > 0 ? (
          <button type="button" class="lz-link lz-row-toggle" aria-expanded={allRows} onClick={() => setAllRows(!allRows)}>
            {allRows ? copy.SHOW_LESS : copy.showMoreRows(hidden)}
          </button>
        ) : null}
      </div>
      <Footer right={copy.QUICK_FOOTER} />
    </div>
  );
}

export class Boundary extends Component {
  constructor() {
    super();
    this.state = { failed: false };
  }
  componentDidCatch() {
    this.setState({ failed: true });
  }
  render() {
    if (this.state.failed) return <Frame heading={copy.FAILED_HEADING} lines={[copy.ASK_AGAIN]} />;
    return this.props.children;
  }
}

// A deep check the MODEL ran: its own card. It adopts the run's task_id from the
// tool result and polls it to the end, then shows the result. Nothing is pushed
// to the model (it called the tool, so it has the result) and nothing is stored
// (a re-mount adopts the same id again), and there is no "changed from the quick
// verdict" line, because no quick verdict is on screen.
function DeepCard({ route, host, announce }) {
  const { state, adopt } = useDeepCheck({ row: { claim: '', verdict: '' }, host, announce, registerCompleted: null, store: null });
  const heading = useRef(null);
  const started = useRef(false);

  useEffect(() => {
    if (started.current || route.view !== 'deep-running') return;
    started.current = true;
    adopt(route.taskId, { stage: route.stage, index: route.index, total: route.total, elapsed: route.elapsed });
  }, [route.view, route.taskId]);

  if (route.view === 'deep') return <DeepResult result={route.result} host={host} headingRef={heading} />;
  if (state.kind === 'completed') {
    return <DeepResult result={deepResult(state.result)} host={host} headingRef={heading} />;
  }
  // Running or ended: the claim is the heading (no eyebrow — "Claim checked" is
  // not true yet), and no footer, which speaks for a result that is not there.
  const claim = route.claim || '';
  if (TERMINAL.has(state.kind)) {
    return (
      <div class="lz">
        {claim ? <h1 class="lz-claim">{claim}</h1> : null}
        <NotFinished state={state} onRetry={null} focusRef={heading} />
      </div>
    );
  }
  return (
    <div class="lz">
      {claim ? <h1 class="lz-claim">{claim}</h1> : null}
      {/* With no claim above it, the running block opens the card: a rule there
          would separate nothing. */}
      <Running state={state.kind === 'idle' ? { kind: 'running', ...route } : state} headingRef={heading} first={!claim} />
    </div>
  );
}

// The picker. The reader chooses, the CARD starts the checks, so
// unlike a model-run card every completed child IS pushed to the model.
//
// Two things make a re-mount safe. The payload never changes, so the picker
// would otherwise reappear over checks that are already running and already
// paid for: the pick record (parent task_id -> children) is what sends a
// reopened card straight to the running rows. And `select_claims_widget` is
// retry-safe by idempotency key, so a lost answer costs nothing.
// What a select response actually started. Every child id is validated before
// it is stored or sent back, a duplicate id is dropped, and a label is trusted
// only when it is one of the texts this card submitted — a response the card
// cannot attribute is treated as not started rather than guessed at.
function startedFrom(result, texts) {
  const entries = result && Array.isArray(result.claims) ? result.claims : [];
  const sameLength = entries.length === texts.length;
  const seen = new Set();
  const started = [];
  for (const [i, entry] of entries.entries()) {
    const taskId = text(entry && entry.task_id);
    if (!isSendableId(taskId) || seen.has(taskId)) continue;
    const echoed = text(entry && entry.claim);
    const claim = texts.includes(echoed) ? echoed : sameLength ? texts[i] : '';
    if (!claim) continue;
    seen.add(taskId);
    started.push({ claim, taskId });
  }
  return started;
}

function Picker({ route, host, announce, registerCompleted, reportState }) {
  const store = useMemo(() => createPickStore(safeStorage(), { parent: route.taskId }), [route.taskId]);
  const saved = useMemo(() => store.load(), [store]);
  const [picks, setPicks] = useState(saved.picks);
  // What was SENT and never confirmed. The selection then locks: changing it
  // would change the idempotency key, and a set the API did start would be
  // started — and charged — a second time.
  const [pending, setPending] = useState(saved.picks.length ? [] : saved.requested);
  const [chosen, setChosen] = useState(saved.picks.length ? [] : saved.requested);
  // What was asked for, for the partial line. Held in state as well as in the
  // record: the record was read at mount, before this card submitted anything.
  const [requested, setRequested] = useState(saved.requested);
  const [all, setAll] = useState(false);
  const [starting, setStarting] = useState(false);
  const [failed, setFailed] = useState(false);
  const heading = useRef(null);

  if (picks.length) {
    return (
      <PickedRows
        picks={picks}
        requested={requested}
        host={host}
        announce={announce}
        registerCompleted={registerCompleted}
        reportState={reportState}
      />
    );
  }

  const locked = pending.length > 0;
  const atCap = chosen.length >= PICKER_MAX_SELECTED;
  const hidden = route.claims.length - PICKER_CLAIMS_SHOWN;
  const toggle = (claim) => {
    if (locked) return;
    setChosen((prev) => (prev.includes(claim) ? prev.filter((c) => c !== claim) : atCap ? prev : [...prev, claim]));
  };

  const submit = async () => {
    if (!chosen.length || starting) return;
    setStarting(true);
    setFailed(false);
    // The claims go back in the payload's own order, not the tap order: the
    // server matches them against the texts it offered.
    const texts = route.claims.filter((claim) => chosen.includes(claim));
    // Written BEFORE the call: if the reply is lost, the next attempt must send
    // exactly these texts, so the key replays instead of starting a second set.
    store.save({ requested: texts });
    setPending(texts);
    setRequested(texts);
    try {
      const result = await host.callTool('select_claims_widget', { task_id: route.taskId, claims: texts });
      const started = startedFrom(result, texts);
      if (!started.length) throw new Error('nothing started');
      store.save({ picks: started, requested: texts });
      setPicks(started);
    } catch (_e) {
      // The picks stay on screen and stay LOCKED: pressing again re-sends the
      // same claims, which the API replays at no new charge.
      setFailed(true);
      setStarting(false);
    }
  };

  return (
    <div class="lz">
      <h1 class="lz-claim" ref={heading} tabIndex={-1}>
        {copy.PICKER_HEADING}
      </h1>
      <p class="lz-body">{copy.PICKER_LEAD}</p>
      <div class="lz-region">
        <ol class="lz-list lz-picks">
          {route.claims.map((claim, i) => {
            const on = chosen.includes(claim);
            return (
              <li key={claim} hidden={!all && i >= PICKER_CLAIMS_SHOWN}>
                <label class="lz-pick">
                  <input
                    type="checkbox"
                    checked={on}
                    disabled={starting || locked || (atCap && !on)}
                    onChange={() => toggle(claim)}
                  />
                  <span class="lz-pick-n">{i + 1}</span>
                  <span>{claim}</span>
                </label>
              </li>
            );
          })}
        </ol>
        {hidden > 0 ? (
          <button type="button" class="lz-link lz-row-toggle" aria-expanded={all} onClick={() => setAll(!all)}>
            {all ? copy.SHOW_LESS : copy.showMoreClaims(hidden)}
          </button>
        ) : null}
      </div>
      <div class="lz-region">
        {atCap && !locked ? <p class="lz-note">{copy.pickerCap(PICKER_MAX_SELECTED)}</p> : null}
        {failed ? (
          <p class="lz-body lz-alert" role="alert">
            {copy.PICKER_FAILED}
          </p>
        ) : null}
        {/* True whatever happened to the first attempt: the same claims derive
            the same idempotency key, so the API replays instead of charging. */}
        {locked ? <p class="lz-note">{copy.PICKER_UNCONFIRMED}</p> : null}
        <button type="button" class="lz-button filled" disabled={!chosen.length || starting} onClick={submit}>
          {starting
            ? copy.PICKER_SUBMITTING
            : locked
              ? copy.PICKER_RETRY
              : chosen.length
                ? copy.pickerButton(chosen.length)
                : copy.PICKER_EMPTY_BUTTON}
        </button>
      </div>
    </div>
  );
}

// The checks the picker started: one row each, adopted by task_id and polled to
// the end. The card started these, so each completed one is pushed to the model
// by the same cumulative rules the list card uses.
function PickedRows({ picks, requested = [], host, announce, registerCompleted, reportState }) {
  // Two different counts, because they say different things: how many have
  // STOPPED (so the card knows it is done waiting) and how many produced a
  // VERDICT (so the heading cannot claim a failure as a check).
  const [settled, setSettled] = useState({});
  const [checked, setChecked] = useState({});
  const stopped = picks.filter((pick) => settled[pick.taskId]).length;
  const verdicts = picks.filter((pick) => checked[pick.taskId]).length;
  const running = stopped < picks.length;
  // Some picks the reader made never started: say so rather than drop them.
  const missing = Math.max(0, requested.length - picks.length);

  // One row open at a time, as on the list card: five full deep results in one
  // card is 8,000 px of scroll, so a settled row collapses to its verdict.
  const [openId, setOpenId] = useState('');

  return (
    <div class="lz">
      <h1 class="lz-claim" tabIndex={-1}>
        {running ? copy.pickerRunningHeading(picks.length) : copy.pickerDoneHeading(verdicts, picks.length)}
      </h1>
      <div class="lz-region lz-rows-region">
        <ol class="lz-list lz-rows">
          {picks.map((pick, i) => (
            <PickedRow
              key={pick.taskId}
              index={i}
              pick={pick}
              host={host}
              announce={announce}
              registerCompleted={registerCompleted}
              reportState={reportState}
              open={openId === pick.taskId}
              onToggle={() => setOpenId(openId === pick.taskId ? '' : pick.taskId)}
              onSettled={(gotVerdict) => {
                setSettled((prev) => (prev[pick.taskId] ? prev : { ...prev, [pick.taskId]: true }));
                if (gotVerdict) setChecked((prev) => (prev[pick.taskId] ? prev : { ...prev, [pick.taskId]: true }));
              }}
            />
          ))}
        </ol>
      </div>
      {missing > 0 ? <p class="lz-note">{copy.pickerPartial(picks.length, requested.length)}</p> : null}
      {running ? <p class="lz-note">{copy.USUALLY}</p> : null}
      {/* No footer while they run: it would describe a result that is not there.
          Every row already says what it is. */}
      {running ? null : <Footer right={copy.DEEP_ROWS_FOOTER} />}
    </div>
  );
}

function PickedRow({ index, pick, host, announce, registerCompleted, reportState, open, onToggle, onSettled }) {
  const row = useMemo(() => ({ claim: pick.claim, verdict: '' }), [pick.claim]);
  const label = copy.rowLabel(index + 1);
  // store: null deliberately. The row store is keyed by claim text, so two
  // pickers offering the same claim would recover each other's check and one
  // paid child would vanish behind the other's result. The
  // pick record holds this child's task_id, which is all recovery needs.
  const { state, adopt } = useDeepCheck({ row, host, announce, registerCompleted, label, store: null, checkKey: pick.taskId, order: index, reportState });
  const [full, setFull] = useState(false);
  const started = useRef(false);
  const heading = useRef(null);
  const panelId = `lz-pick-${index}`;
  const result = state.kind === 'completed' ? deepResult(state.result) : null;
  const ended = TERMINAL.has(state.kind);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    adopt(pick.taskId);
  }, [pick.taskId]);

  useEffect(() => {
    if (result || ended) onSettled(!!result);
  }, [state.kind]);
  useEffect(() => {
    if (!open) setFull(false);
  }, [open]);

  return (
    <li>
      <span class="num">{index + 1}</span>
      <div>
        <button type="button" class="lz-row-head" aria-expanded={open} aria-controls={panelId} onClick={onToggle}>
          <span class="row-claim">{pick.claim}</span>
          {result ? (
            <>
              <span class="row-line">
                <span class={`row-verdict v-${verdictKey(result.verdict)}`}>{result.verdict}</span>
                {result.score != null ? <span class="lz-meta row-score">{`${result.score}/10`}</span> : null}
              </span>
              <span class="lz-meta row-meta">
                <span class="lz-sr"> · </span>
                {copy.rowSources(result.sourcesTotal)}
              </span>
            </>
          ) : ended ? (
            <span class="lz-meta row-meta">{copy.ROW_DID_NOT_FINISH}</span>
          ) : (
            // Running: the stage, so a collapsed row still shows progress.
            <span class="lz-meta row-meta">{state.stage ? copy.stepLine(state.stage, state.index, state.total) : copy.RUNNING_HEADING}</span>
          )}
        </button>
        {open ? (
          <div class="lz-row-panel" id={panelId}>
            {result ? (
              <>
                <p class="lz-meta lz-conf">{copy.deepConfidence(result.confidence)}</p>
                <div class="lz-actions">
                  <button type="button" class="lz-link" aria-expanded={full} onClick={() => setFull(!full)}>
                    {full ? copy.HIDE_FULL_CHECK : copy.SHOW_FULL_CHECK}
                  </button>
                </div>
                {full ? <DeepResult result={result} host={host} compact /> : null}
              </>
            ) : ended ? (
              <NotFinished state={state} onRetry={null} focusRef={heading} />
            ) : (
              <Running state={state} headingRef={heading} first note={false} />
            )}
          </div>
        ) : null}
      </div>
    </li>
  );
}

export function App({ host, payload, announce, registerCompleted, reportState }) {
  const route = routePayload(payload);
  switch (route.view) {
    case 'waiting':
      return <Frame heading={copy.WAITING} />;
    case 'single':
      return <SingleCard key={`${route.row.claim}|${route.row.verdict}`} row={route.row} host={host} announce={announce} registerCompleted={registerCompleted} reportState={reportState} />;
    case 'list':
      return <ListCard rows={route.rows} host={host} announce={announce} registerCompleted={registerCompleted} reportState={reportState} />;
    case 'row-error':
      return (
        <Frame
          eyebrow={copy.QUICK_EYEBROW}
          heading={route.row.claim}
          lines={[copy.ROW_ERROR[route.row.error] || copy.ROW_ERROR_DEFAULT, route.row.hint]}
          footer={copy.QUICK_FOOTER}
        />
      );
    case 'deep':
    case 'deep-running':
      return <DeepCard key={route.taskId || (route.result && route.result.verificationId)} route={route} host={host} announce={announce} />;
    case 'picker':
      return (
        <Picker
          key={route.taskId}
          route={route}
          host={host}
          announce={announce}
          registerCompleted={registerCompleted}
          reportState={reportState}
        />
      );
    case 'nothing':
      return <Frame heading={copy.NOTHING_TO_CHECK} lines={[route.message]} />;
    case 'reconnect':
      return <Frame heading={copy.FAILED_HEADING} lines={[copy.RECONNECT]} />;
    // Each failure below says what happened, whether it was the reader's fault,
    // and what to do next. No footer: nothing was checked, so there is no check
    // to describe.
    case 'outage':
      return <Frame heading={copy.OUTAGE_HEADING} lines={[copy.OUTAGE_BODY, copy.retryIn(route.retryAfter)]} />;
    case 'quota':
      return <Frame heading={copy.QUOTA_EMPTY} lines={[copy.QUOTA_NEXT]} />;
    case 'in-progress':
      return <Frame heading={copy.IN_PROGRESS_HEADING} lines={[copy.IN_PROGRESS_BODY]} />;
    case 'already-resolved':
      return <Frame heading={copy.ALREADY_RESOLVED_HEADING} lines={[copy.ALREADY_RESOLVED_BODY]} />;
    default:
      return <Frame heading={copy.FAILED_HEADING} lines={[copy.ASK_AGAIN]} />;
  }
}
