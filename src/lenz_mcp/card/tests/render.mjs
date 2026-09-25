// The state gallery: every card state (tests/states.mjs) through the stub host,
// light and dark, at 735, 360 and 320 px, at 200% zoom (a 735 px frame at
// device scale 2, so the card lays out in 368 CSS px), and full-bleed on a 390 px
// phone (`bleed390`: the frame spans the screen with no inset, as ChatGPT's iOS
// app renders it, so the card drops its side rules). Also, per state:
//   - an axe-core pass (WCAG 2.x A/AA rules) in both themes,
//   - a reflow check: no horizontal scroll at any width,
//   - no console errors,
//   - screenshot hashes compared with tests/baselines.json.
//
//   npm run render -- [out-dir] [--update-baselines] [--only name,name]
//
// Writes <out>/<state>-<theme>-<size>.png, <out>/gallery.html and gallery.png
// (gallery-2.png, … when one sheet would be too tall). Baselines are hashes of
// the PNGs as Chromium renders them on the machine that recorded them (macOS
// system font): compare on the same machine, re-record after an intended change.
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { CAPABILITIES, startHarness } from './harness.mjs';
import { buildStates } from './states.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const flag = (name) => args.includes(name);
const option = (name) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : '';
};
const positional = args.filter((a, i) => !a.startsWith('--') && !(i > 0 && args[i - 1] === '--only'));
const out = resolve(positional[0] || 'render-out');
const only = option('--only') ? new Set(option('--only').split(',')) : null;
mkdirSync(out, { recursive: true });

const AXE = readFileSync(createRequire(import.meta.url).resolve('axe-core/axe.min.js'), 'utf8');
const BASELINES = join(here, 'baselines.json');
const SIZES = [
  { key: '735', width: 735, scale: 1 },
  { key: '360', width: 360, scale: 1 },
  { key: '320', width: 320, scale: 1 },
  { key: 'zoom200', width: 368, scale: 2 },
];
const THEMES = ['light', 'dark'];
// The ends of the ground band each theme's inks hold AA on (src/styles.js).
const GROUND_BANDS = { light: ['#FFFFFF', '#E8E8E8'], dark: ['#171717', '#3A3A3A'] };
const BLEED_WIDTH = 390;

const states = buildStates().filter((s) => !only || only.has(s.name));
const harness = await startHarness();
const hashes = {};
const problems = [];

async function settle(page, state) {
  const frame = await cardFrame(page);
  // The picker: tick N options, and press when the state asks for it. The
  // started rows adopt their own runs, so the same poll wait covers them.
  if (state.pick != null) {
    const boxes = frame.locator('.lz-pick input');
    for (let i = 0; i < state.pick; i += 1) await boxes.nth(i).check();
    await page.waitForTimeout(60);
  }
  if (state.pickSubmit) {
    await frame.getByRole('button', { name: /^Check \d+ claims?$/ }).click();
    for (let t = 0; t < 4000; t += 200) {
      await page.clock.runFor(200);
      await page.waitForTimeout(15);
    }
  }
  if (state.openRow != null) {
    await frame.locator('.lz-row-head').nth(state.openRow).click();
    await page.waitForTimeout(100);
  }
  // A model-started card adopts its run on mount: give its first poll time.
  if (state.toolResult && state.toolResult.status === 'submitted') {
    for (let t = 0; t < 4000; t += 200) {
      await page.clock.runFor(200);
      await page.waitForTimeout(15);
    }
  }
  if (!state.press) return;
  const scope = state.openRow != null ? frame.locator('.lz-row-panel') : frame;
  await scope.getByRole('button', { name: /Check against sources/ }).click();
  const total = state.fastForwardMs || 4000;
  for (let t = 0; t < total; t += total / 40) {
    await page.clock.runFor(total / 40);
    await page.waitForTimeout(15);
  }
}

async function cardFrame(page) {
  return (await page.locator('iframe').elementHandle()).contentFrame();
}

async function openState(state, { scale, bypassCSP, phone = 0 }) {
  // A phone page taller than any card: capturing an element taller than a
  // mobile-emulated viewport rescales the page mid-capture, and the card would
  // be photographed in the moment it stopped seeing a full-width frame.
  const { page, context, errors } = await harness.page({ width: phone || 800, height: phone ? 6000 : 900, phone: Boolean(phone), scale, bypassCSP });
  await context.clock.install();
  await page.evaluate(
    (c) => window.startCard('card', c),
    {
      theme: 'light',
      width: 735,
      fullBleed: Boolean(phone),
      capabilities: state.capabilities || CAPABILITIES,
      toolResult: state.toolResult,
      noResult: state.noResult,
      tools: state.tools || {},
    },
  );
  const frame = await cardFrame(page);
  await frame.waitForSelector('#lenz-card > *', { timeout: 5000 });
  await settle(page, state);
  if (state.openFullCheck) {
    await frame.getByRole('button', { name: 'Show the full check' }).click();
    await page.waitForTimeout(150);
  }
  await page.waitForTimeout(200);
  return { page, context, errors, frame };
}

try {
  for (const state of states) {
    for (const scale of [1, 2]) {
      const { page, context, errors, frame } = await openState(state, { scale, bypassCSP: scale === 1 });
      for (const theme of THEMES) {
        await page.evaluate((t) => window.setTheme('card', t), theme);
        if (scale === 1) {
          await page.evaluate((w) => window.setWidth('card', w), 735);
          await page.waitForTimeout(150);
          await frame.addScriptTag({ content: AXE }).catch(() => {});
          // A card with no ground of its own shows the host's through the frame,
          // and axe inside the frame cannot see past it. The card cannot know the
          // exact shade, so it is measured on the host page's ground and on both
          // ends of the band its inks are chosen for (src/styles.js).
          const hostGround = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
          const grounds = [hostGround, ...GROUND_BANDS[theme]];
          const violations = await frame.evaluate(async (list) => {
            const root = document.documentElement;
            const run = async () => {
              const r = await window.axe.run(document, { runOnly: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'] });
              return r.violations.map((v) => `${v.id} (${v.nodes.length}): ${v.nodes[0].target.join(' ')}`);
            };
            if (getComputedStyle(root).getPropertyValue('--lz-bg').trim() !== 'transparent') return run();
            const out = [];
            try {
              for (const g of list) {
                root.style.background = g;
                for (const v of await run()) out.push(`on ${g}: ${v}`);
              }
            } finally {
              root.style.background = '';
            }
            return out;
          }, grounds);
          for (const v of violations) problems.push(`AXE ${state.name} ${theme}: ${v}`);
        }
        for (const size of SIZES.filter((s) => s.scale === scale)) {
          await page.evaluate((w) => window.setWidth('card', w), size.width);
          await page.waitForTimeout(200);
          const fit = await frame.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
          if (fit[0] > fit[1]) problems.push(`OVERFLOW ${state.name} ${theme} ${size.key}: ${fit[0]} > ${fit[1]}`);
          const file = `${state.name}-${theme}-${size.key}.png`;
          const png = await page.locator('iframe').screenshot({ path: join(out, file) });
          hashes[file] = createHash('sha256').update(png).digest('hex');
        }
      }
      if (errors.length) problems.push(`CONSOLE ${state.name} scale ${scale}: ${errors.join(' | ')}`);
      await context.close();
    }
    // One fresh phone page per theme: an element screenshot taller than a
    // mobile-emulated viewport rescales the page for a moment, and a card read
    // after it no longer sees a frame the width of the screen.
    for (const theme of THEMES) {
      const { page, context, errors, frame } = await openState(state, { scale: 1, bypassCSP: true, phone: BLEED_WIDTH });
      {
        await page.evaluate((t) => window.setTheme('card', t), theme);
        await page.waitForTimeout(150);
        const fit = await frame.evaluate(() => [
          document.documentElement.scrollWidth,
          document.documentElement.clientWidth,
          document.documentElement.dataset.bleed || '',
        ]);
        if (fit[0] > fit[1]) problems.push(`OVERFLOW ${state.name} ${theme} bleed${BLEED_WIDTH}: ${fit[0]} > ${fit[1]}`);
        if (fit[2] !== 'full') problems.push(`BLEED ${state.name} ${theme}: the card did not see a full-bleed frame`);
        const file = `${state.name}-${theme}-bleed${BLEED_WIDTH}.png`;
        const png = await page.locator('iframe').screenshot({ path: join(out, file) });
        hashes[file] = createHash('sha256').update(png).digest('hex');
      }
      if (errors.length) problems.push(`CONSOLE ${state.name} bleed ${theme}: ${errors.join(' | ')}`);
      await context.close();
    }
    process.stdout.write(`${state.name} `);
  }
} finally {
  await harness.close();
}

// Baselines.
let baselineReport = '';
if (!only && flag('--update-baselines')) {
  writeFileSync(BASELINES, `${JSON.stringify(hashes, Object.keys(hashes).sort(), 1)}\n`);
  baselineReport = `Recorded ${Object.keys(hashes).length} baselines.`;
} else if (existsSync(BASELINES)) {
  const base = JSON.parse(readFileSync(BASELINES, 'utf8'));
  const changed = Object.keys(hashes).filter((f) => base[f] !== hashes[f]);
  const missing = only ? [] : Object.keys(base).filter((f) => !(f in hashes));
  baselineReport = changed.length || missing.length
    ? `BASELINE changed: ${changed.join(', ') || '-'}; gone: ${missing.join(', ') || '-'}`
    : 'Screenshots match the baselines.';
}

// Gallery.
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const groups = [...new Set(states.map((s) => s.group))];
const figure = (name, theme, size) =>
  `<figure><img src="${name}-${theme}-${size}.png" loading="lazy"><figcaption>${theme} · ${size === 'zoom200' ? '200% zoom' : `${size} px`}</figcaption></figure>`;
const section = (s) =>
  `<section id="${esc(s.name)}"><h3>${esc(s.name)}</h3><p class="prov">${esc(s.provenance)}</p>` +
  THEMES.map((t) => `<div class="row">${SIZES.map((z) => figure(s.name, t, z.key)).join('')}</div>`).join('') +
  '</section>';
writeFileSync(
  join(out, 'gallery.html'),
  `<!doctype html><meta charset="utf-8"><title>Lenz card for Claude: every state</title>
<style>body{font:14px system-ui;margin:24px;background:#e9e8e4;color:#222}nav a{margin-right:10px}.row{display:flex;gap:14px;align-items:flex-start;margin:8px 0}
figure{margin:0}figure img{display:block;border:1px solid #bbb}figure img[src*="zoom200"]{width:368px}figcaption{font:12px ui-monospace,monospace;color:#555;margin-top:4px}
h2{margin:40px 0 4px;font-size:18px}h3{margin:28px 0 2px;font-size:15px}.prov{margin:0 0 6px;color:#555;font-size:12px}</style>
<h1>Lenz card for Claude: every state</h1><p>${states.length} states × 2 themes × 4 sizes. Built by <code>npm run render</code> from fixtures/fixtures.json.</p>
<nav>${groups.map((g) => `<a href="#g-${esc(g)}">${esc(g)}</a>`).join('')}</nav>
${groups.map((g) => `<h2 id="g-${esc(g)}">${esc(g)}</h2>${states.filter((s) => s.group === g).map(section).join('\n')}`).join('\n')}`,
);

// One sheet to scroll: 735 and 360 in both themes, at half size, in columns.
const sheetHtml = `<!doctype html><meta charset="utf-8"><style>body{margin:16px;background:#e9e8e4;font:12px system-ui;width:3160px}
.grid{columns:6;column-gap:16px}.s{break-inside:avoid;margin:0 0 18px;background:#f6f5f2;padding:8px}.s b{display:block;margin-bottom:6px}
.r{display:flex;gap:6px;align-items:flex-start;margin-bottom:6px}.r img{display:block}</style>
<div class="grid">${states
  .map(
    (s) =>
      `<div class="s"><b>${esc(s.name)}</b>${THEMES.map(
        (t) => `<div class="r"><img src="file://${out}/${s.name}-${t}-735.png" style="width:245px"><img src="file://${out}/${s.name}-${t}-360.png" style="width:180px"></div>`,
      ).join('')}</div>`,
  )
  .join('')}</div>`;
writeFileSync(join(out, 'gallery-sheet.html'), sheetHtml);
const { chromium } = await import('playwright-core');
const browser = await chromium.launch();
const sheet = await browser.newPage({ viewport: { width: 3200, height: 1000 } });
await sheet.goto(`file://${out}/gallery-sheet.html`);
await sheet.waitForLoadState('networkidle');
const height = await sheet.evaluate(() => document.body.scrollHeight + 32);
const MAX = 16000;
const pages = Math.ceil(height / MAX);
for (let i = 0; i < pages; i++) {
  await sheet.setViewportSize({ width: 3200, height: Math.min(MAX, height - i * MAX) });
  await sheet.evaluate((y) => window.scrollTo(0, y), i * MAX);
  await sheet.screenshot({ path: join(out, i === 0 ? 'gallery.png' : `gallery-${i + 1}.png`) });
}
await browser.close();

console.log(`\n${states.length} states → ${out}/gallery.html (+ gallery.png${pages > 1 ? ` and ${pages - 1} more sheet(s)` : ''})`);
console.log(baselineReport);
console.log(problems.length ? problems.join('\n') : 'axe: no violations; no overflow; no console errors.');
if (problems.length) process.exitCode = 1;
