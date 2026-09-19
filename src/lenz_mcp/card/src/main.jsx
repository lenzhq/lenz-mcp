// Mount: connect to the host, apply its theme, render the card for the tool
// result, report size, push completed deep checks to the model, and stop
// everything on teardown.

import { render } from 'preact';
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';

import { DevSwitcher, createDevHost } from 'lenz-card-dev';

import { App, Boundary } from './app.jsx';
import { createMcpAppsHost } from './host/mcp-apps.js';
import { THEME_SLOTS } from './host/theme.js';
import { changedFrom, modelRanIt } from './logic/route.js';
import { buildSnapshot } from './logic/snapshot.js';
import { buildChatMessage } from './logic/chat-message.js';
import { createMessenger } from './logic/messenger.js';
import { DELIVER_MESSAGE, readDelivery, watchDelivery } from './logic/delivery.js';
import { createAnnouncedStore, createCardStore } from './logic/store.js';
import { isFullBleed } from './logic/bleed.js';

const VARIABLE_NAME = /^--[a-z0-9-]+$/;

function safeStorage() {
  try {
    return globalThis.localStorage;
  } catch (_e) {
    return undefined;
  }
}

// The rows a payload shows, as the card record's identity.
function payloadRows(payload) {
  const claims = payload && Array.isArray(payload.claims) ? payload.claims : [];
  return claims.map((c) => `${(c && c.claim) || ''}\u0000${(c && c.verdict) || ''}`);
}

// Theme from the host: Claude's style variables are
// light-dark() values, so color-scheme must follow the theme or the card stays light.
export function applyContext(ctx, doc = globalThis.document, slots = {}) {
  if (!ctx || !doc) return;
  const root = doc.documentElement;
  if (ctx.theme === 'light' || ctx.theme === 'dark') {
    root.dataset.theme = ctx.theme;
    root.style.colorScheme = ctx.theme;
  }
  const vars = ctx.styles && ctx.styles.variables;
  if (vars && typeof vars === 'object') {
    for (const [name, value] of Object.entries(vars)) {
      if (VARIABLE_NAME.test(name) && typeof value === 'string' && value.length < 512) {
        root.style.setProperty(name, value);
      }
    }
  }
  // The four slots the card paints with, filled by the adapter from this host's
  // own variables (host/theme.js). A slot the host does not publish keeps the
  // Ledger value from src/styles.js.
  for (const [slot, value] of Object.entries(slots)) {
    if (THEME_SLOTS.includes(slot) && VARIABLE_NAME.test(slot)) root.style.setProperty(slot, value);
  }
}

function Root({ host, dev }) {
  const [payload, setPayload] = useState(undefined);
  const [devKey, setDevKey] = useState('');
  const live = useRef(undefined);
  const [, setConnected] = useState(false);
  const [message, setMessage] = useState('');
  const completed = useRef(new Map());

  useEffect(() => {
    const theme = (ctx) => applyContext(ctx, globalThis.document, host.themeVariables ? host.themeVariables(ctx) : {});
    const offs = [
      host.onToolResult((result) => {
        live.current = result;
        setPayload(result);
      }),
      host.onContextChange(theme),
    ];
    host
      .connect()
      .then(({ context }) => {
        theme(context);
        // Capabilities are read at render: show the buttons they allow.
        setConnected(true);
      })
      .catch(() => {});
    return () => offs.forEach((off) => off());
  }, []);

  // What the model has been told is a fact about the CARD, not about one row: a
  // row that recovers late must never replace a bigger snapshot with a smaller one.
  // A model-run card gets no storage at all, not even the probe write that asks
  // whether storage works.
  const cardStore = useMemo(
    () => createCardStore(modelRanIt(payload) ? null : safeStorage(), { rows: payloadRows(payload) }),
    [payloadRows(payload).join('\u0001'), modelRanIt(payload)],
  );
  const pushed = useRef(0);
  const pushing = useRef(Promise.resolve());
  useEffect(() => {
    pushed.current = cardStore.load();
  }, [cardStore]);

  // Telling the model is ONE decision with two mechanisms, and the server says
  // which (mcp_card.card_delivery): Claude takes a silent push, ChatGPT only
  // a chat message. Never both on any host.
  // The announcement ledger is per CONVERSATION, not per card: two cards
  // showing one check, and a picker whose card record is deliberately absent,
  // must not each announce it.
  const ledger = useMemo(() => createAnnouncedStore(safeStorage()), []);
  const messenger = useMemo(
    () =>
      createMessenger({
        ledger,
        send: (checks) => {
          const text = buildChatMessage(checks);
          return text ? host.sendMessage(text) : Promise.resolve();
        },
      }),
    [ledger],
  );
  useEffect(() => () => messenger.dispose(), [messenger]);

  // One cumulative snapshot per card of every deep check it has shown.
  // Pushes are serialized: the host replaces the whole context with what it is
  // handed, so two in flight could land out of order and lose a verdict.
  const schedulePush = () => {
    pushing.current = pushing.current
      .then(async () => {
        const checks = [...completed.current.values()];
        if (!checks.length || checks.length <= pushed.current) return;
        if (!host.capabilities.updateModelContext) return;
        const version = checks.length;
        const snap = buildSnapshot({ checks });
        await host.updateModelContext({ text: snap.text, structured: snap.structured });
        if (version > pushed.current) {
          pushed.current = version;
          cardStore.save(version);
        }
      })
      .catch(() => {});
  };

  // "Completed" and "pushed" are tracked apart: a push that never landed
  // (teardown, host refusal) is tried again on the next mount.
  const registerCompleted = (row, result, store, { recovered = false, checkKey = '', order = 0 } = {}) => {
    if (!result.verificationId) return;
    completed.current.set(result.verificationId, {
      verificationId: result.verificationId,
      claim: result.claim || row.claim,
      // Only a check that finished in front of us has a known time; a recovered
      // one may be days old, and saying "now" would misstate the evidence's age.
      checkedAt: recovered ? '' : new Date().toISOString(),
      verdict: result.verdict,
      score: result.score,
      confidence: result.confidence,
      keyFinding: result.keyFinding,
      summary: result.summary,
      warnings: result.warnings,
      sources: result.sources,
      replacesQuick: changedFrom(row.verdict, result.verdict),
    });
    if (readDelivery(host) === DELIVER_MESSAGE) {
      // The order the checks were STARTED, so a picker's rows are never
      // renumbered by which one happened to finish first.
      // `order` is the check's POSITION IN THE CARD, passed by the row itself.
      // It was briefly derived from which verification completed first, which is
      // precisely the order the message must not use.
      messenger.completed(checkKey, { ...completed.current.get(result.verificationId), order });
      return;
    }
    schedulePush();
  };


  // Rows report whether their check is still going; a card that speaks in the
  // chat waits for all of them before it posts (logic/messenger.js).
  // Tracked unconditionally. The delivery mode is only known once a card tool
  // has answered, so gating this on it dropped every start made before the
  // first response — and each later completion then looked like the only
  // outstanding check and posted its own message. Only the
  // outbound send is gated, in registerCompleted.
  const reportState = (key, kind) => {
    if (kind === 'running') messenger.started(key);
    else if (kind === 'failed') messenger.failed(key);
  };

  return (
    <>
      {dev ? (
        <DevSwitcher
          dev={dev}
          onSelect={(state) => {
            setPayload(state ? state.toolResult : live.current);
            setDevKey(state ? state.name : '');
          }}
        />
      ) : null}
      <Boundary key={devKey}>
        <App
          host={host}
          payload={payload}
          announce={setMessage}
          registerCompleted={registerCompleted}
          reportState={reportState}
        />
      </Boundary>
      <p class="lz-sr" role="status" aria-live="polite">
        {message}
      </p>
    </>
  );
}

// Content height, measured as the ext-apps SDK does (html at max-content), so
// a frame taller than the card can still shrink; width from the viewport.
function watchSize(host, doc = globalThis.document) {
  const measure = () => {
    const html = doc.documentElement;
    const original = html.style.height;
    html.style.height = 'max-content';
    const height = Math.ceil(html.getBoundingClientRect().height);
    html.style.height = original;
    host.reportSize(Math.ceil(globalThis.innerWidth), height);
  };
  let scheduled = false;
  const schedule = () => {
    if (scheduled) return;
    scheduled = true;
    globalThis.requestAnimationFrame(() => {
      scheduled = false;
      measure();
    });
  };
  const observer = new globalThis.ResizeObserver(schedule);
  observer.observe(doc.documentElement);
  observer.observe(doc.body);
  schedule();
  return () => observer.disconnect();
}

// A full-bleed frame (logic/bleed.js) keeps only the card's top and bottom
// hairlines. Re-read on resize: rotating a phone can change the answer.
function watchBleed(doc = globalThis.document, win = globalThis.window) {
  const apply = () => {
    const root = doc.documentElement;
    if (isFullBleed(win)) root.dataset.bleed = 'full';
    else delete root.dataset.bleed;
  };
  apply();
  win.addEventListener('resize', apply);
  return () => win.removeEventListener('resize', apply);
}

export function mount(doc = globalThis.document) {
  const base = createMcpAppsHost({ win: globalThis.window });
  const dev = createDevHost ? createDevHost(base) : null;
  // Wrapped once, here: every card-tool response passes through one place that
  // reads how this host wants the result delivered (logic/delivery.js).
  const host = watchDelivery(dev ? dev.host : base);
  const root = doc.getElementById('lenz-card');
  render(<Root host={host} dev={dev} />, root);
  const stopSize = watchSize(host, doc);
  const stopBleed = watchBleed(doc);
  // Teardown: unmount (every deep check stops polling), stop watching, let go.
  host.onTeardown(() => {
    render(null, root);
    stopSize();
    stopBleed();
    host.dispose();
  });
}

if (globalThis.document && globalThis.document.getElementById('lenz-card')) mount();
