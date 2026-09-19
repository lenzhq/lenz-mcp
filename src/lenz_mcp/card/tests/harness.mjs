// A stub MCP Apps host for the built card: a local server that serves the
// committed bundle and a host page speaking the host side of the protocol, with
// scripted tool answers. Used by host.test.mjs and render.mjs.
import { createServer } from 'node:http';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';

const root = dirname(dirname(fileURLToPath(import.meta.url)));

export function cardHtml() {
  const versions = JSON.parse(readFileSync(join(root, 'dist', 'versions.json'), 'utf8'));
  const latest = Object.values(versions).at(-1);
  return readFileSync(join(root, 'dist', latest.file), 'utf8');
}

const HOST_PAGE = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>
body{margin:0;padding:16px;font-family:system-ui;background:#ffffff}
body.dark{background:rgb(48,48,46)}
iframe{display:block;border:0;width:var(--w,735px);margin:0 0 16px}
</style></head><body><script>
window.log = [];
window.cards = {};
function reply(frame, id, result){ frame.contentWindow.postMessage({jsonrpc:'2.0', id, result}, '*'); }
function fail(frame, id, message){ frame.contentWindow.postMessage({jsonrpc:'2.0', id, error:{code:-32000, message}}, '*'); }
window.addEventListener('message', (event) => {
  const entry = Object.entries(window.cards).find(([, c]) => c.frame.contentWindow === event.source);
  if (!entry) return;
  const [name, card] = entry;
  const msg = event.data;
  if (!msg || msg.jsonrpc !== '2.0') return;
  window.log.push({ card: name, method: msg.method, params: msg.params, id: msg.id, at: Date.now() });
  const cfg = card.config;
  if (msg.method === 'ui/initialize') {
    reply(card.frame, msg.id, { protocolVersion: '2026-01-26', hostInfo: { name: 'stub', version: '0' },
      hostCapabilities: cfg.capabilities, hostContext: { theme: cfg.theme, styles: { variables: cfg.variables || {} },
      displayMode: 'inline', containerDimensions: { width: 735, maxHeight: 5000 } } });
  } else if (msg.method === 'ui/notifications/initialized') {
    card.frame.contentWindow.postMessage({ jsonrpc: '2.0', method: 'ui/notifications/tool-input', params: { arguments: { claim: 'x' } } }, '*');
    if (!cfg.noResult) card.frame.contentWindow.postMessage({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { content: [{ type: 'text', text: '{}' }], structuredContent: cfg.toolResult } }, '*');
  } else if (msg.method === 'tools/call') {
    const stub = (cfg.tools || {})[msg.params.name];
    // A stub may be a QUEUE (answers in order) or a MAP keyed by the argument
    // that identifies the work — task_id. Several rows poll concurrently, and a
    // queue would hand row 1 row 3's result, which the real server never does.
    const byId = stub && !Array.isArray(stub) && typeof stub === 'object';
    const queue = byId ? [] : stub || [];
    const args = (msg.params && msg.params.arguments) || {};
    const answer = byId ? stub[args.task_id || args.verification_id] : queue.length > 1 ? queue.shift() : queue[0];
    if (answer === undefined) return; // never answer: a request that hangs
    if (answer && answer.__error) return fail(card.frame, msg.id, answer.__error);
    const delay = (answer && answer.__delay) || 0;
    setTimeout(() => reply(card.frame, msg.id, { content: [{ type: 'text', text: JSON.stringify(answer) }], structuredContent: answer }), delay);
  } else if (msg.id !== undefined && msg.method) {
    reply(card.frame, msg.id, {});
  }
});
window.startCard = (name, config) => {
  const frame = document.createElement('iframe');
  // Without allow-same-origin the frame has an opaque origin and no localStorage.
  frame.setAttribute('sandbox', config.noStorage ? 'allow-scripts' : 'allow-scripts allow-same-origin');
  frame.title = 'Lenz result';
  if (config.width) frame.style.setProperty('--w', config.width + 'px');
  // ChatGPT's iOS app gives the frame the whole screen width with no inset
  // (2026-09-18): the frame spans the viewport, which is the screen here.
  if (config.fullBleed) {
    document.body.style.padding = '0';
    frame.style.setProperty('--w', '100vw');
  }
  window.cards[name] = { frame, config };
  document.body.appendChild(frame);
  frame.src = config.dev ? '/card-dev.html' : '/card.html';
  return true;
};
window.teardown = (name) => {
  const card = window.cards[name];
  card.frame.contentWindow.postMessage({ jsonrpc: '2.0', id: 9001, method: 'ui/resource-teardown', params: { reason: 'test' } }, '*');
};
window.removeCard = (name) => { window.cards[name].frame.remove(); delete window.cards[name]; };
// A host may deliver another tool result into a card that is already mounted
// (a second needs_input, a later check in the same conversation).
window.pushResult = (name, result) => {
  const card = window.cards[name];
  card.config.toolResult = result;
  card.frame.contentWindow.postMessage(
    { jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { content: [{ type: 'text', text: '{}' }], structuredContent: result } },
    '*',
  );
};
window.setTheme = (name, theme) => {
  document.body.classList.toggle('dark', theme === 'dark');
  document.documentElement.style.colorScheme = theme;
  window.cards[name].frame.contentWindow.postMessage({ jsonrpc: '2.0', method: 'ui/notifications/host-context-changed', params: { theme } }, '*');
};
window.setWidth = (name, width) => { window.cards[name].frame.style.setProperty('--w', width + 'px'); };
// ChatGPT sends ~25 of these within a second of mount (probe, 2026-09-17).
window.contextStorm = (name, times, detail) => {
  const frame = window.cards[name].frame;
  for (let i = 0; i < times; i += 1) {
    frame.contentWindow.postMessage({ jsonrpc: '2.0', method: 'ui/notifications/host-context-changed', params: detail || {} }, '*');
  }
};
// Height reports resize the frame, as a host would.
window.addEventListener('message', (event) => {
  const msg = event.data;
  if (!msg || msg.method !== 'ui/notifications/size-changed') return;
  const entry = Object.values(window.cards).find((c) => c.frame.contentWindow === event.source);
  if (entry) entry.frame.style.height = msg.params.height + 'px';
});
</script></body></html>`;

export async function startHarness() {
  const card = cardHtml();
  const server = createServer((req, res) => {
    if (req.url === '/card.html') {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end(card);
    } else if (req.url === '/card-dev.html' && existsSync(join(root, 'dist-dev', 'card-dev.html'))) {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end(readFileSync(join(root, 'dist-dev', 'card-dev.html'), 'utf8'));
    } else if (req.url === '/host.html') {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end(HOST_PAGE);
    } else {
      res.writeHead(404);
      res.end();
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  return {
    base,
    browser,
    async page({ width = 800, height = 900, phone = false, screenHeight = 900, dark = false, scale = 1, bypassCSP = false } = {}) {
      const context = await browser.newContext({
        viewport: { width, height },
        // A phone: its screen is as wide as the viewport (Playwright's default
        // screen is 1280x720 whatever the viewport), and it is a touch screen,
        // which is what lets the card believe a full-width frame is the whole
        // screen (src/logic/bleed.js).
        ...(phone ? { screen: { width, height: screenHeight }, hasTouch: true, isMobile: true } : {}),
        colorScheme: dark ? 'dark' : 'light',
        deviceScaleFactor: scale,
        bypassCSP,
      });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(String(e)));
      page.on('console', (m) => {
        if (m.type() === 'error') errors.push(m.text());
      });
      await page.goto(`${base}/host.html`);
      if (dark) await page.evaluate(() => { document.body.classList.add('dark'); document.documentElement.style.colorScheme = 'dark'; });
      return { page, context, errors };
    },
    async close() {
      await browser.close();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

export const FIXTURES = JSON.parse(readFileSync(join(root, 'fixtures', 'fixtures.json'), 'utf8'));

export const CAPABILITIES = { message: { text: {} }, updateModelContext: { text: {} }, openLinks: {}, serverTools: {} };

export const QUICK_LOW = {
  status: 'ok',
  claims: [
    {
      claim: '90% of startups fail within their first year.',
      verdict: 'Mostly False',
      confidence: 'low',
      rationale:
        'Most startups survive their first year; official statistics put first-year failures at about one in five, and the 90% figure describes failure over a much longer span.',
      dissent: 'The figure depends on what counts as a startup; for venture-backed firms the early failure rate is higher.',
      recommend_verify: true,
      next_step: 'Recommend a deep check.',
    },
  ],
  confidence_note: 'x',
  source: 'Lenz fast fact-check (3-model panel)',
};

export const PROCESSING = (step, index, elapsed) => ({ status: 'processing', step, index, total: 5, elapsed_seconds: elapsed });

export const COMPLETED = {
  status: 'completed',
  verification_id: 'abcd1234',
  claim: '90% of startups fail within their first year.',
  verdict: 'False',
  lenz_score: 2,
  confidence: 'high',
  confidence_note: 'x',
  depth: 'standard',
  key_finding: 'About one in five new businesses closes within its first year, not nine in ten.',
  executive_summary:
    'Government business survival data show roughly 20% of new establishments close in their first year and about half within five years. The 90% figure is a long-run estimate for some startup categories, misapplied to the first year.',
  warnings: ['Survival rates differ by country and by sector.', 'Venture-backed startups fail at higher rates than the average new business.'],
  sources: [
    { title: 'Business Employment Dynamics: establishment survival', url: 'https://www.bls.gov/bdm/bdmage.htm', publisher: 'bls.gov', date: '2025-06-01', quote: 'Approximately 20 percent of new business establishments fail during the first year.' },
    { title: 'Small business facts', url: 'https://advocacy.sba.gov/facts', publisher: 'sba.gov', date: '2024-03-12', quote: 'About two-thirds of businesses with employees survive at least two years.' },
    { title: 'Startup failure rates explained', url: 'https://example.org/startups', publisher: 'example.org', quote: '' },
  ],
  sources_total: 14,
  presentation: 'x',
  supersedes: 'x',
  source: 'Lenz deep fact-check',
};
