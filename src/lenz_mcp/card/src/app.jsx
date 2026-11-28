// The card's views. Rendering only:
// no host protocol here (that is host/mcp-apps.js) and no polling logic (that is
// logic/deep-check.js). Every payload value reaches the page as a text node:
// JSX children, never innerHTML.

import { Component } from 'preact';
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';

import * as copy from './copy.js';
import { quotaWords } from './logic/credits.js';
import { createDeepCheck } from './logic/deep-check.js';
import { domainOf, formatElapsed, isWebUrl, languageName, text, verdictKey } from './logic/format.js';
import { bucketOf, buttonStrength, changedFrom, deepResult, isSendableId, routePayload, tally } from './logic/route.js';
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

// The reviewers' note: the reasoning and the dissent as ONE block under one
// label, with one "Show more" that opens both. Closed, a note with both parts
// shows four lines of the reasoning and two of the dissent, so the two are named
// and both fit a phone screen; a part on its own keeps six. Whether anything is
// cut is measured, not guessed from its length: the same text is four lines at
// 735 px and nine at 320.
// The classes that cut each part while closed (src/styles.js).
const NOTE_CLAMP = { alone: 'lz-clamp', rationale: 'lz-clamp lz-clamp-4', dissent: 'lz-clamp lz-clamp-2' };

function ReviewersNote({ rationale, dissent, id }) {
  const [open, setOpen] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || open) return undefined;
    const measure = () => setOverflows([...el.children].some((p) => p.scrollHeight > p.clientHeight + 1));
    measure();
    if (!globalThis.ResizeObserver) return undefined;
    const observer = new globalThis.ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [open]);
  const both = Boolean(rationale && dissent);
  const clamp = (part) => (open ? '' : NOTE_CLAMP[both ? part : 'alone']);
  return (
    <div class="lz-reasoning">
      <h2 class="lz-label">{copy.REASONING_LABEL}</h2>
      <div id={id} ref={ref}>
        {rationale ? <p class={`lz-body ${clamp('rationale')}`}>{rationale}</p> : null}
        {dissent ? (
          <p class={`lz-body ${clamp('dissent')}`}>
            <span class="lz-lead">{copy.DISSENT_PREFIX}</span>
            {dissent}
          </p>
        ) : null}
      </div>
      {open || overflows ? (
        <button type="button" class="lz-link" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
          {open ? copy.SHOW_LESS : copy.SHOW_MORE}
        </button>
      ) : null}
    </div>
  );
}

// The pages the reviewers looked at: a disclosure, closed until the reader opens
// it, under the reviewers' note. Five titles, then "Show all N". A row is a
// reference, not an action: the title and, under it, the site, the whole row one
// link that opens through the host, or plain text where the host cannot open
// links. Titles are text nodes. Opening it, or a link, calls no tool and sends
// no message.
const PAGES_SHOWN = 5;

function Pages({ pages, host, id }) {
  const [all, setAll] = useState(false);
  const hidden = pages.length > PAGES_SHOWN + 1 ? pages.length - PAGES_SHOWN : 0;
  const list = all || hidden === 0 ? pages : pages.slice(0, PAGES_SHOWN);
  const canOpen = host.capabilities.openLinks;
  return (
    <details class="lz-pages">
      <summary class="lz-pages-summary">{copy.pagesSummary(pages.length)}</summary>
      <ul class="lz-list lz-pages-list" id={id}>
        {list.map((page, i) => {
          const site = isWebUrl(page.url) ? domainOf(page.url) : '';
          const row = (
            <>
              <span class="lz-page-title">{page.title}</span>
              {site ? <span class="lz-page-site">{site}</span> : null}
            </>
          );
          return (
            <li key={i}>
              {canOpen && isWebUrl(page.url) ? (
                <a
                  class="lz-page"
                  href={page.url}
                  rel="noopener noreferrer nofollow ugc"
                  onClick={(event) => {
                    event.preventDefault();
                    host.openLink(page.url).catch(() => {});
                  }}
                >
                  {row}
                </a>
              ) : (
                <p class="lz-page">{row}</p>
              )}
            </li>
          );
        })}
      </ul>
      {hidden > 0 && !all ? (
        <button type="button" class="lz-link" aria-controls={id} onClick={() => setAll(true)}>
          {copy.showAllPages(pages.length)}
        </button>
      ) : null}
    </details>
  );
}

// What a quick No Public Record verdict means, in one plain sentence under it.
function NoRecordLine({ row }) {
  return verdictKey(row.verdict) === 'no-record' ? <p class="lz-body lz-no-record">{copy.NO_RECORD_QUICK}</p> : null;
}

function Reasoning({ row, host, idSuffix = '' }) {
  const pages = row.pages || [];
  if (!row.rationale && !row.dissent && !pages.length) return null;
  const hasNote = Boolean(row.rationale || row.dissent);
  return (
    <div class="lz-region first lz-reviewers">
      {hasNote ? <ReviewersNote rationale={row.rationale} dissent={row.dissent} id={`lz-reasoning${idSuffix}`} /> : null}
      {pages.length ? (
        <div class={hasNote ? 'lz-pages-block' : ''}>
          <Pages pages={pages} host={host} id={`lz-pages${idSuffix}`} />
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
    <div class="lz-actions lz-actions-row">
      <button type="button" class={cls} ref={buttonRef} onClick={onPress}>
        {copy.CHECK_BUTTON}
      </button>
      <span class="lz-meta">{copy.CHECK_DURATION_HINT}</span>
    </div>
  );
}

// What a check that did not finish says, as one sentence for the status region.
export function notFinishedText(state) {
  if (state.kind === 'quota') {
    const words = quotaWords({ ...state, scope: 'deep' });
    return [words.heading, ...words.lines].join(' ');
  }
  if (state.kind === 'unavailable') return copy.UNAVAILABLE;
  if (state.kind === 'unrecoverable') return copy.UNRECOVERABLE;
  if (state.retryable) return copy.OUTAGE;
  return `${copy.FAILED_HEADING} ${copy.FAILED_BY_CLASS[state.failureClass] || copy.FAILED_DEFAULT}`;
}

function NotFinished({ state, onRetry, focusRef }) {
  if (state.kind === 'quota') {
    const words = quotaWords({ ...state, scope: 'deep' });
    return (
      <div class="lz-region">
        <p class="lz-body" tabIndex={-1} ref={focusRef}>{words.heading}</p>
        {words.lines.map((line) => (
          <p key={line} class="lz-body">{line}</p>
        ))}
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
  const hidden = warnings.length > CAVEATS_SHOWN + 1 ? warnings.length - CAVEATS_SHOWN : 0;
  const shown = open || hidden === 0 ? warnings : warnings.slice(0, CAVEATS_SHOWN);
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
  const hidden = all.length > SOURCES_SHOWN + 1 ? all.length - SOURCES_SHOWN : 0;
  const list = open || hidden === 0 ? all : all.slice(0, SOURCES_SHOWN);
  // No Public Record already says nothing public records the claim: no "could not be read" line.
  if (!all.length && verdictKey(result.verdict) === 'no-record') return null;
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
              {s.quote ? <Quote quote={s.quote} language={s.quoteLanguage} /> : null}
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

// A source's quote. A non-English one is marked with its language (the web's
// pattern: a small tag before it, `lang` on the quote so it is read and
// hyphenated as that language). No translation. Both values are text nodes.
function Quote({ quote, language }) {
  if (!language) return <p class="lz-quote">{`“${quote}”`}</p>;
  return (
    <p class="lz-quote lz-quote-foreign" lang={language}>
      <span class="lz-quote-lang" lang="en">
        {languageName(language)}
      </span>
      <span dir="auto">{`“${quote}”`}</span>
    </p>
  );
}

function AuditTrace({ audit }) {
  if (!audit || typeof audit !== 'object') return null;
  const sources = audit.sources_researched || 0;
  const evidence = audit.evidence_count || 0;
  const panel = Array.isArray(audit.panel) ? audit.panel : [];

  const countsLine = copy.auditCounts(sources, evidence);

  if (!countsLine && !panel.length) return null;

  return (
    <details class="audit-trace">
      <summary class="audit-trace-summary">{copy.AUDIT_TRACE_SUMMARY}</summary>
      <div class="audit-trace-body">
        {countsLine ? <p class="audit-counts">{countsLine}</p> : null}
        {panel.length > 0 ? (
          <div class="audit-panel">
            {panel.map((seat, i) => {
              const raw = String(seat.name || 'Reviewer').split(' · ')[0].trim();
              const rawName = /^Reviewer [A-Z]$/.test(raw)
                ? raw
                : 'Reviewer ' + (i < 26 ? String.fromCharCode(65 + i) : i + 1);
              const scoreText =
                typeof seat.score === 'number' && !Number.isNaN(seat.score) ? `${seat.score}/10` : '';
              const detail = [seat.verdict, scoreText ? `(${scoreText})` : ''].filter(Boolean).join(' ');
              return (
                <span key={i} class="audit-pill">
                  {detail ? `${rawName}: ${detail}` : rawName}
                </span>
              );
            })}
          </div>
        ) : null}
      </div>
    </details>
  );
}

function CopyButton({ textToCopy }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    if (!textToCopy) return;
    let ok = false;
    try {
      if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(textToCopy);
        ok = true;
      }
    } catch {}
    if (!ok) {
      try {
        const ta = document.createElement('textarea');
        ta.value = textToCopy;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        ok = Boolean(document.execCommand && document.execCommand('copy'));
        document.body.removeChild(ta);
      } catch {
        ok = false;
      }
    }
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };
  return (
    <button type="button" class="lz-link lz-copy-btn" onClick={onCopy}>
      {copied ? copy.COPIED_REWRITE : copy.COPY_REWRITE}
    </button>
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
      {was && !compact ? <p class="lz-meta lz-changed">{copy.changedFrom(was, result.verdict)}</p> : null}
      {compact ? null : <Stamp result={result} />}
      {compact ? null : <p class="lz-meta lz-conf">{copy.deepConfidence(result.confidence)}</p>}
      {result.keyFinding ? <p class="lz-finding">{result.keyFinding}</p> : null}
      {result.summary ? (
        <div class="lz-region first">
          <p class="lz-body">{result.summary}</p>
        </div>
      ) : null}
      {/* Between the summary and the caveats, where the claim page puts it. */}
      {result.suggestedRewrite ? (
        <div class="lz-region">
          <div class="lz-rewrite-head">
            <h2 class="lz-label">{copy.SUGGESTED_REWRITE}</h2>
            <CopyButton textToCopy={result.suggestedRewrite} />
          </div>
          <p class="lz-body">{result.suggestedRewrite}</p>
        </div>
      ) : null}
      <Caveats warnings={result.warnings} />
      <AuditTrace audit={result.card_audit || result.cardAudit} />
      <Sources result={result} host={host} />
      {result.url && host.capabilities.openLinks && isWebUrl(result.url) ? (
        <div class="lz-actions">
          <a
            class="lz-link"
            href={result.url}
            rel="noopener noreferrer"
            onClick={(e) => {
              e.preventDefault();
              host.openLink(result.url).catch(() => {});
            }}
          >
            {copy.SEE_SOURCES_IN_LENZ}
          </a>
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
    () => (given === null ? null : createRowStore(safeStorage(), { claim: row.claim, quickVerdict: row.verdict, language: row.language })),
    [row.claim, row.verdict, row.language, given === null],
  );
  const store = given === null ? null : own;
  const [state, setState] = useState({ kind: 'idle' });
  const checkRef = useRef(null);

  useEffect(() => {
    const check = createDeepCheck({
      claim: row.claim,
      language: row.language,
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
  }, [row.claim, row.verdict, row.language]);

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
      {busy ? null : <NoRecordLine row={row} />}
      <p class="lz-meta lz-conf">{copy.CONFIDENCE_LINE[row.confidence]}</p>
      {busy ? null : <Reasoning row={row} host={host} />}
      {state.kind === 'idle' && host.capabilities.serverTools ? (
        <CheckButton row={row} onPress={press} buttonRef={buttonRef} />
      ) : null}
      {busy ? <Running state={state} headingRef={runningHeading} /> : null}
      {['failed', 'quota', 'unavailable', 'unrecoverable'].includes(state.kind) ? (
        <NotFinished state={state} onRetry={press} focusRef={terminalRef} />
      ) : null}
    </div>
  );
}

// One list row. Collapsed: the claim, the
// row's CURRENT verdict, and only what is unusual about it in words. Open: the
// confidence line and the reasoning the single card shows, plus the action — and
// once its deep check has run, the deep verdict in place, with the full deep
// check block behind one disclosure. One row is open at a time; the others stay usable.
function Row({ index, displayNumber, row, host, open, hidden, onToggle, announce, registerCompleted, reportState, onVerdict, autoStart, completedResult }) {
  const num = displayNumber != null ? displayNumber : index + 1;
  const label = copy.rowLabel(num);
  const { state, start } = useDeepCheck({ row, host, announce, registerCompleted, label, checkKey: rowId(row), order: index, reportState });
  const [full, setFull] = useState(false);
  const panelId = `lz-row-${index}`;
  const headRef = useRef(null);
  const runningHeading = useRef(null);
  const terminalRef = useRef(null);
  const focusFollows = useRef(false);
  const result =
    state.kind === 'completed'
      ? deepResult(state.result)
      : completedResult
        ? deepResult(completedResult)
        : null;
  const busy = state.kind === 'submitting' || state.kind === 'running';

  useEffect(() => {
    if (state.kind === 'completed' && state.result) {
      const r = deepResult(state.result);
      onVerdict(index, r.verdict, r.confidence, state.result);
    }
  }, [state.kind === 'completed' ? state.result : null]);
  useEffect(() => {
    if (!open) setFull(false);
  }, [open]);
  useEffect(() => {
    if (autoStart && state.kind === 'idle' && !result && !completedResult && !row.error) start();
  }, [autoStart]);

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
    <li id={`mcp-row-${index}`} hidden={hidden || undefined}>
      <span class="num">{num}</span>
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
              <p class="lz-meta lz-row-changed">{copy.changedFrom(changedFrom(row.verdict, result.verdict), result.verdict)}</p>
            ) : null}
            {row.error ? <p class="lz-body">{row.hint}</p> : null}
            {/* The confidence belongs to the verdict the row is showing: the deep
                check's once it has one, the quick read's until then. */}
            {result ? <p class="lz-meta lz-conf">{copy.deepConfidence(result.confidence)}</p> : null}
            {result || row.error ? null : <NoRecordLine row={row} />}
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
                {busy ? null : <Reasoning row={row} host={host} idSuffix={`-${index}`} />}
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

const rowId = (row) => `${row.claim}\u0000${row.verdict}\u0000${row.language || ''}`;
const openKey = (i) => `row-${i}`;
const rowKey = (row, i) => `${i}\u0000${rowId(row)}`;
const SEV = { bad: 0, look: 1, ok: 2, none: 3 };

// The order the 8-row cap counts in: To fix, Closer look, Checks out only while
// that group is open, then Unchecked. A folded Checks out group has no cap
// position; the fold alone hides it.
const capOrder = (b, okOpen) => [...b.bad, ...b.look, ...(okOpen ? b.ok : []), ...b.none];

// Asymmetric bucket promotion: severity upgrades move immediately,
// severity downgrades stay pinned while the card is open so an open card
// never vanishes into collapsed Holds up.
function bucketRows(rows, verdicts, confidences, openId) {
  const b = { bad: [], look: [], ok: [], none: [] };
  rows.forEach((row, i) => {
    const v = verdicts[rowKey(row, i)] || verdicts[rowId(row)] || row.verdict;
    const c = confidences[rowKey(row, i)] || confidences[rowId(row)] || row.confidence;
    const initialBucket = bucketOf(row.verdict, row.confidence, row.error);
    const liveBucket = bucketOf(v, c, row.error);
    if (SEV[liveBucket] < SEV[initialBucket]) {
      b[liveBucket].push(i);
    } else if (SEV[liveBucket] > SEV[initialBucket]) {
      if (openId === openKey(i)) {
        b[initialBucket].push(i);
      } else {
        b[liveBucket].push(i);
      }
    } else {
      b[liveBucket].push(i);
    }
  });
  return b;
}

function ListCard({ rows, citations = [], host, announce, registerCompleted, reportState }) {
  const [allRows, setAllRows] = useState(false);
  const [openId, setOpenId] = useState('');
  // A row's deep verdict replaces its quick one in the tally as well as in the
  // row, remembered by rowId(row) so a replaced or reordered list never applies
  // a previous claim's verdict to a new claim at the same index.
  const [verdicts, setVerdicts] = useState({});
  const [confidences, setConfidences] = useState({});
  const [deepResults, setDeepResults] = useState({});
  const [batchRun, setBatchRun] = useState({});

  const normCitations = useMemo(() => {
    return (Array.isArray(citations) ? citations : []).map(normalizeCitation);
  }, [citations]);

  const citeBuckets = useMemo(() => {
    const b = { bad: [], look: [], ok: [], none: [] };
    normCitations.forEach((c) => {
      b[c.bucket].push(c);
    });
    return b;
  }, [normCitations]);

  const onVerdict = (keyOrIndex, v, conf, rawResult) => {
    if (typeof keyOrIndex === 'number') {
      const row = rows[keyOrIndex];
      if (!row) return;
      const rk = rowKey(row, keyOrIndex);
      const id = rowId(row);
      setVerdicts((prev) => ({ ...prev, [rk]: v, [id]: v }));
      if (conf) setConfidences((prev) => ({ ...prev, [rk]: conf, [id]: conf }));
      if (rawResult) setDeepResults((prev) => ({ ...prev, [rk]: rawResult, [id]: rawResult }));
      setBatchRun((prev) => (prev[keyOrIndex] ? { ...prev, [keyOrIndex]: false } : prev));
    } else if (typeof keyOrIndex === 'string') {
      setVerdicts((prev) => ({ ...prev, [keyOrIndex]: v }));
      if (conf) setConfidences((prev) => ({ ...prev, [keyOrIndex]: conf }));
      if (rawResult) setDeepResults((prev) => ({ ...prev, [keyOrIndex]: rawResult }));
    }
  };

  const counted = rows.map((row, i) => {
    const deep = verdicts[rowKey(row, i)] || verdicts[rowId(row)];
    return deep ? { ...row, verdict: deep, error: '' } : row;
  });

  const tallyCounts = useMemo(() => {
    const t = tally(counted);
    if (normCitations.length > 0) {
      normCitations.forEach((c) => {
        if (c.bucket === 'bad') t.wrong += 1;
        else if (c.bucket === 'look') t.mixed += 1;
        else if (c.bucket === 'ok') t.hold += 1;
        else if (c.bucket === 'none') t.error += 1;
      });
    }
    return t;
  }, [counted, normCitations]);

  const headingText =
    normCitations.length > 0
      ? copy.itemsHeading(rows.length + normCitations.length)
      : copy.listHeading(rows.length);

  const buckets = useMemo(
    () => bucketRows(rows, verdicts, confidences, openId),
    [rows, verdicts, confidences, openId]
  );

  const hasAttention =
    buckets.bad.length > 0 ||
    buckets.look.length > 0 ||
    citeBuckets.bad.length > 0 ||
    citeBuckets.look.length > 0;
  // Checks out folds behind anything to fix or look at, however short the list
  // (the GDocs add-on and the Chrome extension fold the same way); a clean
  // result shows what was checked.
  const [showOk, setShowOk] = useState(() => !hasAttention);

  const displayRank = useMemo(() => {
    const map = new Map();
    capOrder(buckets, showOk).forEach((idx, rank) => {
      map.set(idx, rank);
    });
    return map;
  }, [buckets, showOk]);
  // "Show N more" counts the rows a press reveals: those in the cap order past
  // the cap. A row with no cap position (folded Checks out) is never counted.
  const capCount = capOrder(buckets, showOk).length;
  const hidden = capCount > LIST_ROWS_SHOWN + 1 ? capCount - LIST_ROWS_SHOWN : 0;
  const capped = !allRows && hidden > 0;
  const pastCap = (i) => capped && displayRank.get(i) >= LIST_ROWS_SHOWN;

  const itemNumbers = useMemo(() => {
    const claimNums = new Map();
    const citeNums = new Map();
    if (normCitations.length === 0) return { claimNums, citeNums };
    let seq = 0;
    for (const key of ['bad', 'look', 'ok', 'none']) {
      for (const i of buckets[key]) {
        seq += 1;
        claimNums.set(i, seq);
      }
      for (const c of citeBuckets[key]) {
        seq += 1;
        citeNums.set(c, seq);
      }
    }
    return { claimNums, citeNums };
  }, [buckets, citeBuckets, normCitations.length]);

  const uncheckedBad = buckets.bad.filter(
    (i) => !verdicts[rowKey(rows[i], i)] && !verdicts[rowId(rows[i])] && !rows[i].error && !batchRun[i]
  );
  const showBatchBad = host?.capabilities?.serverTools && uncheckedBad.length >= 2 && uncheckedBad.length <= 5;

  return (
    <div class="lz">
      <p class="lz-eyebrow">{copy.QUICK_EYEBROW}</p>
      <div class="lz-head">
        <h1 class="lz-claim" tabIndex={-1}>
          {headingText}
        </h1>
        <p class="lz-body lz-tally">{copy.tallyLine(tallyCounts)}</p>
      </div>

      {rows.length + normCitations.length > 1 ? (
        <div class="claim-strip" role="navigation" aria-label="Claims overview">
          {rows.map((r, i) => {
            const v = verdicts[rowKey(r, i)] || verdicts[rowId(r)] || r.verdict;
            const c = confidences[rowKey(r, i)] || confidences[rowId(r)] || r.confidence;
            const b = bucketOf(v, c, r.error);
            const isOpen = openId === openKey(i);
            const vLabel = v || copy.ROW_ERROR[r.error] || copy.ROW_ERROR_DEFAULT;
            const cellClass = `strip-cell ${verdictKey(v) === 'no-record' ? 'strip-npr' : `strip-${b}`}${isOpen ? ' is-open' : ''}`;
            return (
              <button
                key={`${rowId(r)}#${i}`}
                type="button"
                class={cellClass}
                aria-expanded={isOpen}
                title={`Claim ${i + 1} (${vLabel}): ${r.claim}`}
                aria-label={`Claim ${i + 1}: ${vLabel}`}
                onClick={() => {
                  // Judge against the order as it will be after this click: the
                  // row open (it may leave a pinned bucket) and its group open.
                  const after = bucketRows(rows, verdicts, confidences, openKey(i));
                  const okOpen = showOk || after.ok.includes(i);
                  if (!showOk && okOpen) setShowOk(true);
                  if (!allRows && capOrder(after, okOpen).length > LIST_ROWS_SHOWN + 1 && capOrder(after, okOpen).indexOf(i) >= LIST_ROWS_SHOWN) setAllRows(true);
                  setOpenId(openKey(i));
                  setTimeout(() => {
                    const el = globalThis.document?.getElementById(`mcp-row-${i}`);
                    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                  }, 0);
                }}
              />
            );
          })}
          {normCitations.map((c, i) => (
            <span
              key={`cite-strip-${c.index ?? i}`}
              class={`strip-cell strip-${c.bucket}`}
              title={`Citation ${i + 1}: ${c.label}`}
              aria-label={`Citation ${i + 1}: ${c.label}`}
            />
          ))}
        </div>
      ) : null}

      <div class="lz-region lz-rows-region">
        <ol class="lz-list lz-rows">
          {(buckets.bad.length > 0 && buckets.bad.some((i) => !pastCap(i))) || citeBuckets.bad.length > 0 ? (
            <li key="header-bad" class="lz-bucket-head">
              {copy.BUCKET_BAD}
            </li>
          ) : null}
          {showBatchBad ? (
            <li key="batch-bad" class="lz-batch-actions">
              <button
                type="button"
                class="lz-button outlined"
                onClick={() => {
                  const next = { ...batchRun };
                  uncheckedBad.forEach((idx) => {
                    next[idx] = true;
                  });
                  setBatchRun(next);
                }}
              >
                {copy.checkAllFlagged(uncheckedBad.length)}
              </button>
            </li>
          ) : null}
          {buckets.bad.map((i) => (
            <Row
              key={`${rowId(rows[i])}#${i}`}
              index={i}
              displayNumber={itemNumbers.claimNums.get(i)}
              row={rows[i]}
              host={host}
              hidden={pastCap(i)}
              open={openId === openKey(i)}
              onToggle={() => setOpenId(openId === openKey(i) ? '' : openKey(i))}
              announce={announce}
              registerCompleted={registerCompleted}
              reportState={reportState}
              onVerdict={onVerdict}
              autoStart={!!batchRun[i]}
              completedResult={deepResults[rowKey(rows[i], i)] || deepResults[rowId(rows[i])]}
            />
          ))}
          {citeBuckets.bad.map((c, idx) => (
            <CitationRow
              key={`cite-bad-${c.index ?? idx}`}
              citation={c}
              displayNumber={itemNumbers.citeNums.get(c)}
              fallbackIndex={rows.length + idx}
              host={host}
            />
          ))}

          {(buckets.look.length > 0 && buckets.look.some((i) => !pastCap(i))) || citeBuckets.look.length > 0 ? (
            <li key="header-look" class="lz-bucket-head">
              {copy.BUCKET_LOOK}
            </li>
          ) : null}
          {buckets.look.map((i) => (
            <Row
              key={`${rowId(rows[i])}#${i}`}
              index={i}
              displayNumber={itemNumbers.claimNums.get(i)}
              row={rows[i]}
              host={host}
              hidden={pastCap(i)}
              open={openId === openKey(i)}
              onToggle={() => setOpenId(openId === openKey(i) ? '' : openKey(i))}
              announce={announce}
              registerCompleted={registerCompleted}
              reportState={reportState}
              onVerdict={onVerdict}
              autoStart={!!batchRun[i]}
              completedResult={deepResults[rowKey(rows[i], i)] || deepResults[rowId(rows[i])]}
            />
          ))}
          {citeBuckets.look.map((c, idx) => (
            <CitationRow
              key={`cite-look-${c.index ?? idx}`}
              citation={c}
              displayNumber={itemNumbers.citeNums.get(c)}
              fallbackIndex={rows.length + idx}
              host={host}
            />
          ))}

          {buckets.ok.length > 0 || citeBuckets.ok.length > 0 ? (
            <li key="header-ok" class="lz-bucket-head">
              <button
                type="button"
                class="lz-bucket-toggle"
                aria-expanded={showOk}
                onClick={() => {
                  if (showOk && buckets.ok.some((idx) => openKey(idx) === openId)) {
                    setOpenId('');
                  }
                  setShowOk(!showOk);
                }}
              >
                <span>{copy.bucketOkToggle(buckets.ok.length + citeBuckets.ok.length)}</span>
                <span aria-hidden="true">{showOk ? '▴' : '▾'}</span>
              </button>
            </li>
          ) : null}
          {buckets.ok.map((i) => (
            <Row
              key={`${rowId(rows[i])}#${i}`}
              index={i}
              displayNumber={itemNumbers.claimNums.get(i)}
              row={rows[i]}
              host={host}
              hidden={!showOk || pastCap(i)}
              open={openId === openKey(i)}
              onToggle={() => setOpenId(openId === openKey(i) ? '' : openKey(i))}
              announce={announce}
              registerCompleted={registerCompleted}
              reportState={reportState}
              onVerdict={onVerdict}
              autoStart={!!batchRun[i]}
              completedResult={deepResults[rowKey(rows[i], i)] || deepResults[rowId(rows[i])]}
            />
          ))}
          {showOk
            ? citeBuckets.ok.map((c, idx) => (
                <CitationRow
                  key={`cite-ok-${c.index ?? idx}`}
                  citation={c}
                  displayNumber={itemNumbers.citeNums.get(c)}
                  fallbackIndex={rows.length + idx}
                  host={host}
                />
              ))
            : null}

          {(buckets.none.length > 0 && buckets.none.some((i) => !pastCap(i))) || citeBuckets.none.length > 0 ? (
            <li key="header-none" class="lz-bucket-head">
              {copy.BUCKET_NONE}
            </li>
          ) : null}
          {buckets.none.map((i) => (
            <Row
              key={`${rowId(rows[i])}#${i}`}
              index={i}
              displayNumber={itemNumbers.claimNums.get(i)}
              row={rows[i]}
              host={host}
              hidden={pastCap(i)}
              open={openId === openKey(i)}
              onToggle={() => setOpenId(openId === openKey(i) ? '' : openKey(i))}
              announce={announce}
              registerCompleted={registerCompleted}
              reportState={reportState}
              onVerdict={onVerdict}
              autoStart={!!batchRun[i]}
              completedResult={deepResults[rowKey(rows[i], i)] || deepResults[rowId(rows[i])]}
            />
          ))}
          {citeBuckets.none.map((c, idx) => (
            <CitationRow
              key={`cite-none-${c.index ?? idx}`}
              citation={c}
              displayNumber={itemNumbers.citeNums.get(c)}
              fallbackIndex={rows.length + idx}
              host={host}
            />
          ))}
        </ol>
        {hidden > 0 ? (
          <button type="button" class="lz-link lz-row-toggle" aria-expanded={allRows} onClick={() => setAllRows(!allRows)}>
            {allRows ? copy.SHOW_LESS : copy.showMoreRows(hidden)}
          </button>
        ) : null}
      </div>
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
  const hidden = route.claims.length > PICKER_CLAIMS_SHOWN + 1 ? route.claims.length - PICKER_CLAIMS_SHOWN : 0;
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
              <li key={claim} hidden={!all && hidden > 0 && i >= PICKER_CLAIMS_SHOWN}>
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

function DeepBatchCard({ items, unstarted = [], host, announce, registerCompleted, reportState }) {
  const [settled, setSettled] = useState(() => {
    const init = {};
    items.forEach((it, i) => {
      if (it.result || (it.status !== 'submitted' && it.status !== 'processing') || !it.taskId) {
        init[i] = true;
      }
    });
    return init;
  });
  const [checked, setChecked] = useState(() => {
    const init = {};
    items.forEach((it, i) => {
      if (it.result) init[i] = true;
    });
    return init;
  });
  const stopped = items.filter((_, i) => settled[i]).length;
  const verdicts = items.filter((_, i) => checked[i]).length;
  const running = stopped < items.length;
  const [openIdx, setOpenIdx] = useState(-1);

  return (
    <div class="lz">
      <p class="lz-eyebrow">{copy.DEEP_EYEBROW}</p>
      <h1 class="lz-claim" tabIndex={-1}>
        {running ? copy.pickerRunningHeading(items.length) : copy.pickerDoneHeading(verdicts, items.length)}
      </h1>
      <div class="lz-region lz-rows-region">
        <ol class="lz-list lz-rows">
          {items.map((item, i) => (
            <DeepBatchRow
              key={item.taskId || `${i}-${item.claim}`}
              index={i}
              item={item}
              host={host}
              announce={announce}
              registerCompleted={registerCompleted}
              reportState={reportState}
              open={openIdx === i}
              onToggle={() => setOpenIdx(openIdx === i ? -1 : i)}
              onSettled={(gotVerdict) => {
                setSettled((prev) => (prev[i] ? prev : { ...prev, [i]: true }));
                if (gotVerdict) setChecked((prev) => (prev[i] ? prev : { ...prev, [i]: true }));
              }}
            />
          ))}
          {unstarted.map((claim, j) => (
            <li key={`unstarted-${j}`}>
              <span class="num">{items.length + j + 1}</span>
              <div>
                <div class="lz-row-head">
                  <span class="row-claim">{claim}</span>
                  <span class="lz-meta row-meta">{copy.QUOTA_SHORT}</span>
                </div>
              </div>
            </li>
          ))}
        </ol>
      </div>
      {unstarted.length > 0 ? <p class="lz-note">{copy.pickerPartial(items.length, items.length + unstarted.length)}</p> : null}
      {running ? <p class="lz-note">{copy.USUALLY}</p> : null}
      {running ? null : <Footer right={copy.DEEP_ROWS_FOOTER} />}
    </div>
  );
}

function DeepBatchRow({ index, item, host, announce, registerCompleted, reportState, open, onToggle, onSettled }) {
  const isRunningInitial = !item.result && (item.status === 'submitted' || item.status === 'processing') && !!item.taskId;
  const row = useMemo(() => ({ claim: item.claim, verdict: '' }), [item.claim]);
  const label = copy.rowLabel(index + 1);
  // Model-initiated batch check: registerCompleted and store are null so the card
  // neither writes to storage nor pushes duplicate context to the model.
  const { state, adopt } = useDeepCheck({
    row,
    host,
    announce,
    registerCompleted: null,
    label,
    store: null,
    checkKey: item.taskId || `batch-${index}`,
    order: index,
  });
  const [full, setFull] = useState(false);
  const started = useRef(false);
  const heading = useRef(null);
  const panelId = `lz-batch-${index}`;

  useEffect(() => {
    if (!isRunningInitial || started.current) return;
    started.current = true;
    adopt(item.taskId, { stage: item.stage, index: item.index, total: item.total, elapsed: item.elapsed });
  }, [isRunningInitial, item.taskId]);

  const polledResult = state.kind === 'completed' ? deepResult(state.result) : null;
  const result = item.result || polledResult;
  const polledPicker =
    state.kind === 'failed' && state.failureClass === 'needs_input' && item.taskId && Array.isArray(state.claims) && state.claims.length
      ? { taskId: item.taskId, claims: state.claims }
      : null;
  const picker = item.picker || polledPicker;
  const ended = !result && (!isRunningInitial || TERMINAL.has(state.kind));

  useEffect(() => {
    if (result || ended) onSettled(!!result);
  }, [!!result, ended]);
  useEffect(() => {
    if (!open) setFull(false);
  }, [open]);

  const claimLabel = item.claim || (result && result.claim) || copy.rowLabel(index + 1);

  return (
    <li>
      <span class="num">{index + 1}</span>
      <div>
        <button type="button" class="lz-row-head" aria-expanded={open} aria-controls={panelId} onClick={onToggle}>
          <span class="row-claim">{claimLabel}</span>
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
          ) : picker ? (
            <span class="lz-meta row-meta">{copy.PICKER_EMPTY_BUTTON}</span>
          ) : ended ? (
            <span class="lz-meta row-meta">{copy.ROW_DID_NOT_FINISH}</span>
          ) : (
            <span class="lz-meta row-meta">
              {state.stage ? copy.stepLine(state.stage, state.index, state.total) : copy.RUNNING_HEADING}
            </span>
          )}
        </button>
        {open || picker ? (
          <div class="lz-row-panel" id={panelId} hidden={!open}>
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
            ) : picker ? (
              <Picker
                key={picker.taskId}
                route={picker}
                host={host}
                announce={announce}
                registerCompleted={registerCompleted}
                reportState={reportState}
              />
            ) : ended ? (
              <NotFinished
                state={isRunningInitial ? state : { kind: 'failed', failureClass: item.failureClass }}
                onRetry={null}
                focusRef={heading}
              />
            ) : (
              <Running state={state} headingRef={heading} first note={false} />
            )}
          </div>
        ) : null}
      </div>
    </li>
  );
}

const ISSUE_FINDINGS = new Set([
  'doi_not_found',
  'page_not_found',
  'not_found',
  'metadata_mismatch',
  'contradicted',
  'contradicts',
  'quote_not_in_source',
  'not_in_source',
  'partly_supported',
  'partial_support',
]);

const CITECHECK_FINDING_LABELS = {
  doi_not_found: 'DOI not registered',
  page_not_found: 'Page not found',
  not_found: 'Not found',
  contradicted: 'Contradicted',
  contradicts: 'Contradicted',
  quote_not_in_source: 'Quote not in the source',
  not_in_source: 'Not in the source',
  metadata_mismatch: 'Reference details differ',
  partly_supported: 'Closer look',
  partial_support: 'Closer look',
  supported: 'Supported',
  supports: 'Supported',
  unchecked: 'Unchecked',
  failed: 'Unchecked',
  pending: 'Not checked yet',
};

function normalizeCitation(c) {
  const row = c || {};
  const res = row.result && typeof row.result === 'object' ? row.result : {};
  const chk = row.check && typeof row.check === 'object' ? row.check : {};
  const finding =
    row.finding ||
    res.finding ||
    res.verdict ||
    (chk.status === 'failed' ? 'failed' : chk.status === 'pending' ? 'pending' : '');
  const isSupport = finding === 'supported' || finding === 'supports';
  const isIssue =
    typeof row.is_issue === 'boolean'
      ? row.is_issue
      : typeof res.is_issue === 'boolean'
        ? res.is_issue
        : ISSUE_FINDINGS.has(finding);
  const isLook = finding === 'partly_supported' || finding === 'partial_support';
  const isBad = isIssue && !isLook;
  const isUnchecked = !isIssue && !isSupport;
  const bucket = isBad ? 'bad' : isLook ? 'look' : isSupport ? 'ok' : 'none';
  const label =
    row.finding_label ||
    CITECHECK_FINDING_LABELS[finding] ||
    (finding ? finding.replace(/_/g, ' ') : 'Unchecked');
  const snippet = text(row.snippet || chk.snippet);
  const rationale = text(
    row.rationale ||
      chk.rationale ||
      row.reason ||
      row.hint ||
      chk.hint ||
      (!snippet ? row.snippet || chk.snippet : ''),
  );
  const rawUrl = text(row.cited_url || row.url || chk.source_url);
  const doi = text(row.doi);
  const url = rawUrl || (doi ? `https://doi.org/${doi.replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, '')}` : '');
  const reference = text(row.reference);
  const ref = reference || domainOf(url) || doi || url;
  return {
    index: row.index ?? row.citation_index,
    finding,
    label,
    isSupport,
    isIssue,
    isLook,
    isBad,
    isUnchecked,
    bucket,
    url,
    doi,
    reference,
    ref,
    statement: text(row.statement),
    snippet,
    rationale,
  };
}

function CitecheckCard({ payload: initialPayload, host }) {
  const [data, setData] = useState(initialPayload || {});
  const [showHoldsUp, setShowHoldsUp] = useState(false);

  useEffect(() => {
    if (initialPayload && typeof initialPayload === 'object') {
      setData(initialPayload);
    }
  }, [initialPayload]);

  const citationCheckId =
    data.citation_check_id || data.citecheck_id || initialPayload?.citation_check_id || initialPayload?.citecheck_id || '';

  const isFailed =
    data.status === 'failed' ||
    data.status === 'not_found' ||
    data.status === 'error' ||
    data.status === 'invalid_request';

  const isRunning =
    !isFailed &&
    (data.status === 'queued' || data.status === 'running' || data.status === 'reading' || (!data.status && !Array.isArray(data.citations)));

  useEffect(() => {
    if (!isRunning || !citationCheckId || !host?.capabilities?.serverTools) return undefined;
    let timer = null;
    let active = true;

    async function poll() {
      try {
        const res = await host.callTool('get_citation_check_widget', {
          citation_check_id: citationCheckId,
          citecheck_id: citationCheckId,
        });
        if (!active) return;
        if (res && typeof res === 'object') {
          setData(res);
          const stillGoing = res.status === 'queued' || res.status === 'running' || res.status === 'reading';
          if (stillGoing) {
            timer = setTimeout(poll, 2500);
          }
        }
      } catch (_err) {
        if (active) {
          timer = setTimeout(poll, 2500);
        }
      }
    }

    timer = setTimeout(poll, 2500);
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [isRunning, citationCheckId, host]);

  if (isFailed) {
    const code = text(data.failure?.code || data.error_code || data.code);
    const line =
      code === 'no_checkable_citations' || code === 'no_citations'
        ? copy.CITECHECK_NO_CITATIONS
        : copy.FAILED_DEFAULT;
    return (
      <Frame
        eyebrow={copy.CITECHECK_EYEBROW}
        heading={copy.FAILED_HEADING}
        lines={[line, copy.ASK_AGAIN]}
      />
    );
  }

  const citations = (Array.isArray(data.citations) ? data.citations : []).map(normalizeCitation);
  const rawIssues = (Array.isArray(data.citation_issues) ? data.citation_issues : []).map((r) => {
    const norm = normalizeCitation(r);
    const isLook = norm.finding === 'partly_supported' || norm.finding === 'partial_support';
    const isBad = !isLook;
    return {
      ...norm,
      isIssue: true,
      isLook,
      isBad,
      bucket: isBad ? 'bad' : 'look',
    };
  });
  const allItems = citations.length > 0 ? citations : rawIssues;
  const bad = allItems.filter((c) => c.bucket === 'bad');
  const look = allItems.filter((c) => c.bucket === 'look');
  const unchecked = allItems.filter((c) => c.bucket === 'none');
  const supports = allItems.filter((c) => c.bucket === 'ok');

  // The heading counts the rows the reader sees, so it can never disagree with
  // the groups under it. The summary's own numbers are the fallback for a
  // payload that lists only its issues.
  const listed = citations.length > 0;
  const summaryChecked = data.summary?.citation_checks?.checked ?? data.summary?.checked;
  const summaryCounts = {
    total: data.summary?.citations_selected ?? summaryChecked,
    checked: summaryChecked,
    issues: data.summary?.issues ?? data.summary?.citation_issues ?? bad.length,
    look: look.length,
  };
  const rowCounts = {
    total: allItems.length,
    checked: allItems.length - unchecked.length,
    issues: bad.length,
    look: look.length,
  };

  const heading = isRunning ? copy.CITECHECK_CHECKING : copy.citecheckSummary(listed ? rowCounts : summaryCounts);

  const progDone = data.progress?.citation_checks?.done ?? data.progress?.done;
  const progTotal = data.progress?.citations ?? data.progress?.total;
  const progressLine =
    isRunning && Number.isInteger(progDone) && Number.isInteger(progTotal) && progTotal > 0
      ? copy.citecheckProgress(progDone, progTotal)
      : '';

  const allClean = !isRunning && bad.length === 0 && look.length === 0 && unchecked.length === 0 && supports.length > 0;
  const effectiveShowHoldsUp = showHoldsUp || allClean;

  return (
    <div class="lz citecheck-card">
      <p class="lz-eyebrow">{copy.CITECHECK_EYEBROW}</p>
      <div class="lz-head">
        <h1 class="lz-claim" tabIndex={-1}>
          {heading}
        </h1>
      </div>
      {isRunning ? (
        <div class="lz-running" style={{ marginTop: '6px' }}>
          {progressLine ? <p class="lz-meta">{progressLine}</p> : null}
          <p class="lz-meta">{copy.USUALLY}</p>
        </div>
      ) : null}

      {citations.length > 1 ? (
        <div class="claim-strip" role="navigation" aria-label="Citations overview">
          {citations.map((c, i) => (
            <span
              key={c.index ?? i}
              class={`strip-cell strip-${c.bucket}`}
              title={`Citation ${i + 1}: ${c.label}`}
            />
          ))}
        </div>
      ) : null}

      {bad.length > 0 ? (
        <div class="citecheck-section">
          <h2 class="citecheck-section-title">{copy.BUCKET_BAD}</h2>
          <ol class="citecheck-list">
            {bad.map((c, i) => (
              <CitationRow key={c.index ?? i} citation={c} fallbackIndex={i} host={host} />
            ))}
          </ol>
        </div>
      ) : null}

      { look.length > 0 ? (
        <div class="citecheck-section">
          <h2 class="citecheck-section-title">{copy.BUCKET_LOOK}</h2>
          <ol class="citecheck-list">
            {look.map((c, i) => (
              <CitationRow key={c.index ?? i} citation={c} fallbackIndex={bad.length + i} host={host} />
            ))}
          </ol>
        </div>
      ) : null}

      {unchecked.length > 0 ? (
        <div class="citecheck-section">
          <h2 class="citecheck-section-title">{copy.BUCKET_NONE}</h2>
          <ol class="citecheck-list">
            {unchecked.map((c, i) => (
              <CitationRow key={c.index ?? i} citation={c} fallbackIndex={bad.length + look.length + i} host={host} />
            ))}
          </ol>
        </div>
      ) : null}

      {supports.length > 0 ? (
        <div class="citecheck-section">
          {allClean ? (
            <h2 class="citecheck-section-title">{copy.citecheckHoldsUpToggle(supports.length)}</h2>
          ) : (
            <button
              type="button"
              class="lz-bucket-toggle citecheck-toggle"
              aria-expanded={effectiveShowHoldsUp}
              onClick={() => setShowHoldsUp(!showHoldsUp)}
            >
              <span>{copy.citecheckHoldsUpToggle(supports.length)}</span>
              <span aria-hidden="true">{effectiveShowHoldsUp ? '▴' : '▾'}</span>
            </button>
          )}
          {effectiveShowHoldsUp ? (
            <ol class="citecheck-list" style={{ marginTop: '8px' }}>
              {supports.map((c, i) => (
                <CitationRow key={c.index ?? i} citation={c} fallbackIndex={bad.length + look.length + unchecked.length + i} host={host} />
              ))}
            </ol>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function CitationRow({ citation, displayNumber, fallbackIndex, host }) {
  const c = citation && typeof citation.isSupport === 'boolean' ? citation : normalizeCitation(citation);
  const isPartly = c.finding === 'partly_supported' || c.finding === 'partial_support';
  const badgeClass = c.isSupport
    ? 'badge-support'
    : c.isUnchecked
      ? 'badge-neutral'
      : isPartly
        ? 'badge-look'
        : 'badge-issue';
  const ref = c.ref;
  const href = isWebUrl(c.url) ? c.url : isWebUrl(ref) ? ref : '';
  const canOpen = host?.capabilities?.openLinks;
  const rowNum =
    displayNumber != null
      ? displayNumber
      : Number.isInteger(c.index)
        ? c.index + 1
        : Number.isInteger(fallbackIndex)
          ? fallbackIndex + 1
          : null;

  return (
    <li class="citecheck-row">
      <span class="num">{rowNum != null ? rowNum : ''}</span>
      <div class="citecheck-row-body">
        <div class="citecheck-row-head">
          <span class={`cite-badge ${badgeClass}`}>{c.label}</span>
          {ref ? (
            href && canOpen ? (
              <button
                type="button"
                class="lz-link cite-ref"
                onClick={() => host.openLink(href).catch(() => {})}
              >
                {ref}
              </button>
            ) : (
              <span class="cite-ref">{ref}</span>
            )
          ) : null}
        </div>
        {c.statement ? <p class="cite-statement">{c.statement}</p> : null}
        {c.snippet ? <Quote quote={c.snippet} /> : null}
        {c.rationale && c.rationale !== c.snippet ? <p class="cite-rationale">{c.rationale}</p> : null}
      </div>
    </li>
  );
}

export function App({ host, payload, announce, registerCompleted, reportState }) {
  const route = routePayload(payload);
  switch (route.view) {
    case 'citecheck':
      return (
        <CitecheckCard
          key={route.payload?.citation_check_id || route.payload?.citecheck_id || 'citecheck'}
          payload={route.payload}
          host={host}
        />
      );
    case 'waiting':
      return <Frame heading={copy.WAITING} />;
    case 'single':
      return <SingleCard key={`${route.row.claim}|${route.row.verdict}|${route.row.language}`} row={route.row} host={host} announce={announce} registerCompleted={registerCompleted} reportState={reportState} />;
    case 'list':
      return <ListCard rows={route.rows} citations={route.citations} host={host} announce={announce} registerCompleted={registerCompleted} reportState={reportState} />;
    case 'row-error':
      return (
        <Frame
          eyebrow={copy.QUICK_EYEBROW}
          heading={route.row.claim}
          lines={[copy.ROW_ERROR[route.row.error] || copy.ROW_ERROR_DEFAULT, route.row.hint]}
        />
      );
    case 'deep':
    case 'deep-running':
      return <DeepCard key={route.taskId || (route.result && route.result.verificationId)} route={route} host={host} announce={announce} />;
    case 'deep-batch':
      return (
        <DeepBatchCard
          items={route.items}
          unstarted={route.unstarted}
          host={host}
          announce={announce}
          registerCompleted={registerCompleted}
          reportState={reportState}
        />
      );
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
    case 'quota': {
      const words = quotaWords({ ...route, scope: 'call' });
      return <Frame heading={words.heading} lines={words.lines} />;
    }
    case 'in-progress':
      return <Frame heading={copy.IN_PROGRESS_HEADING} lines={[copy.IN_PROGRESS_BODY]} />;
    case 'already-resolved':
      return <Frame heading={copy.ALREADY_RESOLVED_HEADING} lines={[copy.ALREADY_RESOLVED_BODY]} />;
    default:
      return <Frame heading={copy.FAILED_HEADING} lines={[copy.ASK_AGAIN]} />;
  }
}
