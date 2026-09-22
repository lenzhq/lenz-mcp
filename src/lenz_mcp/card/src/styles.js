// The card's CSS. Ground, text, secondary text and hairlines
// are four SLOTS (--lz-bg / ink / meta / hair) with Ledger values here as the
// default; the host adapter fills them from whatever variables that host
// publishes (src/host/theme.js), inline on the root, so nothing below names a
// host's variables. OURS either way: the five verdict inks, the one filled
// button and the mono labels.
//
// Rules: a hairline frame, radius 10 (top and bottom rules only on a full-bleed
// frame), no shadow, no pills, no animation; the
// verdict word is the one coloured thing; the dimmed quick verdict uses the
// secondary ink, never opacity (opacity fails AA: 2.4-3.1 on both grounds).
// Measured AA on Claude's grounds (#FFFFFF, rgb(48,48,46)), 2026-09-17: light
// inks 4.92-6.47, dark inks 4.78-7.92, primary 5.55 / on-dark 7.03.

export const CSS = `
:root {
  color-scheme: light dark;
  --lz-font: var(--font-sans, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif);
  --lz-mono: var(--font-mono, ui-monospace, 'SF Mono', Menlo, Consolas, monospace);
  --lz-bg: #FFFFFF;
  --lz-ink: #1A1816;
  --lz-meta: #635E59;
  --lz-hair: #E8E4DD;
  --lz-true: #14783A;
  --lz-mostly-true: #A16207;
  --lz-mixed: #635E59;
  --lz-mostly-false: #C2410C;
  --lz-false: #B91C1C;
  --lz-primary: #3D65BC;
  --lz-on-primary: #FFFFFF;
  --lz-off: #E8E4DD;
  /* The reading measure for prose inside a card wider than a comfortable line:
     a frozen length, never a font-relative unit. The rows are not prose and span
     the card. */
  --lz-measure: 640px;
}
:root[data-theme='dark'] {
  --lz-bg: #2A2724;
  --lz-ink: #FFFDF7;
  --lz-meta: #A8A29E;
  --lz-hair: #3D3935;
  --lz-true: #4ADE80;
  --lz-mostly-true: #FBBF24;
  --lz-mixed: #A8A29E;
  --lz-mostly-false: #FB923C;
  --lz-false: #F87171;
  --lz-primary: #7BA3F0;
  --lz-on-primary: #1A1816;
  --lz-off: #3D3935;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme]) {
    --lz-bg: #2A2724;
    --lz-ink: #FFFDF7;
    --lz-meta: #A8A29E;
    --lz-hair: #3D3935;
    --lz-true: #4ADE80;
    --lz-mostly-true: #FBBF24;
    --lz-mixed: #A8A29E;
    --lz-mostly-false: #FB923C;
    --lz-false: #F87171;
    --lz-primary: #7BA3F0;
    --lz-on-primary: #1A1816;
    --lz-off: #3D3935;
  }
}
html, body { margin: 0; padding: 0; background: transparent; }
body { font-family: var(--lz-font); color: var(--lz-ink); -webkit-font-smoothing: antialiased; }
* { box-sizing: border-box; }
::selection { background: color-mix(in srgb, var(--lz-primary) 25%, transparent); }

.lz {
  background: var(--lz-bg);
  border: 1px solid var(--lz-hair);
  border-radius: 10px;
  padding: 16px;
  /* Fill the frame the host gives, up to a reading width: a frame wider than the
     card used to leave an empty band on its right and wrap claims that fit. */
  max-width: 880px;
  overflow-wrap: anywhere;
}
@media (max-width: 419px) { .lz { padding: 16px; } }
/* A frame the host gives the whole screen width has no room for side rules or
   rounded corners (logic/bleed.js): keep the top and bottom hairlines only, and
   span the frame, so a landscape phone wider than the card's column does not
   show a card that simply stops, with no rule on its right. */
:root[data-bleed='full'] .lz { border-left-width: 0; border-right-width: 0; border-radius: 0; max-width: none; }

/* Zero specificity: the reset must never outrank a component's own margin. */
:where(.lz) :where(h1, h2, p) { margin: 0; }
.lz-eyebrow {
  font-family: var(--lz-mono); font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--lz-meta); margin: 0 0 8px;
}
.lz-claim { font-size: 17px; font-weight: 500; line-height: 1.4; }
.lz-claim:focus { outline: none; }
.lz-verdict { margin-top: 16px; font-size: 30px; font-weight: 500; line-height: 1.1; }
.lz-verdict.dimmed { color: var(--lz-meta); }
.lz-verdict .lz-verdict-label {
  font-family: var(--lz-mono); font-size: 12px; font-weight: 400; letter-spacing: 0.04em;
  color: var(--lz-meta); margin-left: 8px; vertical-align: middle;
}
.lz-meta { font-family: var(--lz-mono); font-size: 13px; line-height: 1.5; color: var(--lz-meta); }
.lz-conf { margin-top: 6px; }

.v-true { color: var(--lz-true); }
.v-mostly-true { color: var(--lz-mostly-true); }
.v-mixed { color: var(--lz-mixed); }
.v-mostly-false { color: var(--lz-mostly-false); }
.v-false { color: var(--lz-false); }
.v-unknown { color: var(--lz-ink); }

.lz-region { margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--lz-hair); }
.lz-region.first { margin-top: 12px; padding-top: 0; border-top: 0; }
.lz-body { font-size: 15px; line-height: 1.6; }
.lz-body, .lz-finding, .lz-quote, .lz-note, .lz-caveats, .lz-conf { max-width: var(--lz-measure); }
.lz-body + .lz-body { margin-top: 10px; }
.lz-clamp { display: -webkit-box; -webkit-line-clamp: 6; -webkit-box-orient: vertical; overflow: hidden; }
.lz-label {
  font-family: var(--lz-mono); font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--lz-meta); margin: 8px 0 0;
}
h2.lz-label { margin: 0 0 10px; font-weight: 400; }

.lz-link {
  appearance: none; background: none; border: 0; padding: 4px 0; margin: 0; cursor: pointer;
  font: inherit; font-size: 15px; color: var(--lz-primary); text-decoration: underline;
  text-underline-offset: 4px; text-align: left; min-height: 24px;
}
a.lz-link { display: inline-block; }
/* A disclosure that closes a list spans its row, so it reads as the list's last line. */
.lz-row-toggle { display: block; width: 100%; margin-top: 8px; min-height: 44px; }
/* Touch: every quiet control keeps a 44px hit area. */
@media (pointer: coarse) { .lz-link { min-height: 44px; } }
.lz [tabindex='-1']:focus { outline: none; }
.lz-actions { margin-top: 16px; }
.lz-button {
  appearance: none; font: inherit; font-size: 15px; font-weight: 500; cursor: pointer;
  min-height: 44px; padding: 10px 16px; border-radius: 8px; text-align: center; width: 100%;
}
.lz-button.filled { background: var(--lz-primary); color: var(--lz-on-primary); border: 1px solid var(--lz-primary); }
.lz-button.outlined { background: transparent; color: var(--lz-primary); border: 1px solid var(--lz-primary); }
@media (min-width: 420px) { .lz-button { width: auto; } }
.lz :focus-visible { outline: 2px solid var(--lz-primary); outline-offset: 2px; border-radius: 4px; }

.lz-running h2 { font-size: 15px; font-weight: 500; }
.lz-running .lz-meta { margin-top: 6px; font-variant-numeric: tabular-nums; }
/* A sentence is prose, not data: secondary ink in the text face, never mono. */
.lz-note { margin-top: 10px; font-size: 14px; line-height: 1.5; color: var(--lz-meta); }
/* A button under a note or an alert keeps the 16px a button in .lz-actions has; alone in its region it sits on the rule. */
.lz-region > * + .lz-button { margin-top: 16px; }

.lz-changed { margin-top: 16px; }
/* The changed line belongs to the stamp below it, not to the claim above. */
.lz-changed + .lz-stamp { margin-top: 4px; }
.lz-stamp { margin-top: 12px; display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 16px; }
.lz-stamp-word { font-size: 34px; font-weight: 500; line-height: 1; }
@media (max-width: 419px) { .lz-stamp-word.two-words { font-size: 28px; } }
.lz-stamp-score { font-family: var(--lz-mono); font-size: 20px; font-variant-numeric: tabular-nums; }
.lz-stamp-score .denom { color: var(--lz-meta); }
.lz-bar { display: inline-flex; gap: 3px; flex-basis: 100%; }
.lz-bar i { display: block; width: 14px; height: 7px; background: var(--lz-off); }
.lz-bar i.on { background: currentColor; }
@media (forced-colors: active) { .lz-bar i { border: 1px solid CanvasText; } .lz-bar i.on { background: CanvasText; } }
.lz-finding { margin-top: 14px; font-size: 17px; font-weight: 500; line-height: 1.45; }

.lz-list { list-style: none; margin: 0; padding: 0; }
.lz-list > li + li { margin-top: 8px; padding-top: 8px; border-top: 1px dotted var(--lz-hair); }
/* Caveats are a quiet bulleted list, not rows between hairlines: the bullet
   has its own column, so a wrapped line aligns with the
   text above it and not under the bullet. Hairlines keep their job: sections and
   source rows. The list carries role=list because list-style none drops list
   semantics in Safari, and the count is worth announcing. */
.lz-caveats { list-style: none; }
.lz-caveats > li { display: grid; grid-template-columns: 0.9em 1fr; gap: 0 6px; font-size: 15px; line-height: 1.6; }
.lz-caveats > li::before { content: '•'; color: var(--lz-meta); }
.lz-caveats > li + li { margin-top: 8px; padding-top: 0; border-top: 0; }
/* The disclosure is not a fourth caveat: 8px between items, more below the list. */
.lz-caveats + .lz-link { margin-top: 12px; }
/* The picker's option rows. The checkbox and the number each get a
   column, so a wrapped claim aligns with the claim above it. 44px minimum on a
   coarse pointer is handled by the shared rule below; the label is the target,
   so the whole row is tappable, not just the box. */
.lz-picks > li + li { margin-top: 0; padding-top: 0; border-top: 0; }
.lz-pick { display: grid; grid-template-columns: auto 2ch 1fr; gap: 0 10px; align-items: start;
  min-height: 44px; padding: 10px 0; font-size: 15px; line-height: 1.5; cursor: pointer; }
.lz-pick + .lz-pick { border-top: 1px dotted var(--lz-hair); }
.lz-picks > li + li .lz-pick { border-top: 1px dotted var(--lz-hair); }
.lz-pick input { margin: 2px 0 0; width: 16px; height: 16px; accent-color: var(--lz-primary); }
.lz-pick input:disabled { cursor: not-allowed; }
.lz-pick-n { font-family: var(--lz-mono); font-size: 13px; color: var(--lz-meta); padding-top: 2px; }
.lz-alert { color: var(--lz-false); }
.lz-button:disabled { opacity: 0.55; cursor: not-allowed; }

.lz-source-meta { margin-top: 2px; }
.lz-quote { margin-top: 6px; font-size: 15px; line-height: 1.6; text-indent: -0.4em; }
.lz-rows > li { display: grid; grid-template-columns: 2ch minmax(0, 1fr); gap: 0 10px; }
/* A row is one line: the claim, and its verdict in a right-hand column so the
   verdicts scan down one edge. The meta (not sure, reviewers split, sources) sits
   under the claim only when there is one. The rule between rows is the whole gap:
   the header's own padding, nothing added. */
.lz-rows > li + li { margin-top: 0; padding-top: 0; }
.lz-rows-region { margin-top: 12px; padding-top: 0; }
/* A row header IS the disclosure: the whole line is the target, and the
   affordance is on the claim's text, never a box. */
.lz-row-head {
  appearance: none; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 16px;
  align-items: baseline; width: 100%; text-align: left; background: none; border: 0;
  padding: 8px 0; margin: 0; font: inherit; color: inherit; cursor: pointer;
}
.lz-row-head:hover .row-claim { text-decoration: underline; text-underline-offset: 3px; }
.lz-row-head .row-claim { grid-column: 1; }
/* The known labels fit on one line; an unknown long one wraps inside a bounded
   column rather than push the row past the frame's edge. */
.lz-row-head .row-line { grid-column: 2; grid-row: 1; text-align: right; max-width: 16em; }
.lz-row-head .row-meta { grid-column: 1; }
/* A narrow frame has no room for a second column: the verdict goes under the claim. */
@media (max-width: 419px) {
  .lz-row-head { grid-template-columns: minmax(0, 1fr); }
  .lz-row-head .row-line { grid-column: 1; grid-row: auto; text-align: left; max-width: none; }
}
.lz-row-head .row-verdict.dimmed { color: var(--lz-meta); }
.lz-row-head .row-score { font-family: var(--lz-mono); font-size: 13px; font-variant-numeric: tabular-nums; margin-left: 6px; }
@media (pointer: coarse) { .lz-row-head { min-height: 44px; } }
.lz-row-panel { margin-top: 0; padding-bottom: 8px; }
.lz-row-changed { margin-top: -4px; }
.lz-tally { margin-top: 6px; color: var(--lz-meta); }
/* The list card's title and its tally share one line when they fit. */
.lz-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 12px; }
.lz-head .lz-tally { margin-top: 0; }
.lz-rows > li > .num { font-family: var(--lz-mono); font-size: 13px; color: var(--lz-meta); padding-top: 8px; }
.lz-rows > li .row-claim { font-size: 15px; line-height: 1.5; }
.lz-rows > li .row-verdict { font-size: 15px; font-weight: 500; }

.lz-footer {
  margin-top: 12px; padding-top: 8px; border-top: 1px solid var(--lz-hair);
  display: flex; justify-content: space-between; gap: 12px;
  font-family: var(--lz-mono); font-size: 12px; color: var(--lz-meta);
}
.lz-sr { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }
`;
