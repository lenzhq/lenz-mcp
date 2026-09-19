// Does the committed bundle actually mount?
//
//   npm run smoke
//
// Two seconds, no fixtures, no virtual clock. It mounts the bundle in the stub
// host and asserts the card rendered something and the page reported no error.
//
// Why this exists as its own script rather than a case in host.test.mjs: a
// bundle that throws at mount fails that suite as a row of 30-second selector
// timeouts, which reads as infrastructure trouble and sends the reader looking
// in the wrong place. A missing import is the typical cause: esbuild builds
// the bundle happily and the card throws `ReferenceError` at mount. Run BEFORE
// the flows, this says it in one line, e.g.
// `ReferenceError: createAnnouncedStore is not defined`.
//
// It picks no state from states.mjs and reads no fixture, so a change to the
// card's states cannot make it ambiguous. (harness.mjs loads fixtures.json at
// import for its own reasons; nothing here depends on the contents.)
import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { CAPABILITIES, startHarness } from './harness.mjs';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const MOUNT_TIMEOUT_MS = 5000;
const problems = [];

// A shape the card should be able to render, built by hand rather than taken
// from fixtures.json: what is asserted is that the bundle runs, not that any
// particular state looks right.
const RESULT = { status: 'completed', claim: 'Smoke test.', verdict: 'True', score: 9, confidence: 'high' };

const CASES = [
  { name: 'prod bundle, no result yet', config: { noResult: true } },
  { name: 'prod bundle, a result', config: { toolResult: RESULT } },
];
// Found, never named. Gating this on a hardcoded bundle filename meant
// that renaming the bundle would drop the dev case in SILENCE — a green run
// with one case fewer, which is the failure mode this file exists to catch.
// tests/host.test.mjs loads the dev bundle too, so its absence is an error
// rather than a reason to skip.
const devBundles = readdirSync(join(root, 'dist-dev')).filter((f) => f.endsWith('.html'));
if (devBundles.length !== 1) {
  console.error(`Expected exactly one bundle in dist-dev/, found ${devBundles.length}: ${devBundles.join(', ') || 'none'}.`);
  process.exit(1);
}
CASES.push({ name: `dev bundle (${devBundles[0]})`, config: { dev: true, toolResult: RESULT } });

const harness = await startHarness();
try {
  for (const testCase of CASES) {
    const { page, context, errors } = await harness.page({});
    await page.evaluate(
      (c) => window.startCard('smoke', c),
      { theme: 'light', width: 735, capabilities: CAPABILITIES, tools: {}, ...testCase.config },
    );
    const frame = await (await page.locator('iframe').elementHandle()).contentFrame();
    try {
      await frame.waitForSelector('#lenz-card > *', { timeout: MOUNT_TIMEOUT_MS });
      // A card that mounted may still have logged an error; say so.
      if (errors.length) problems.push(`${testCase.name}: the card rendered, but the page reported: ${errors.join(' | ')}`);
    } catch {
      // The page error is the diagnosis; the timeout is only the symptom, and
      // reporting the symptom alone sends the reader looking in the wrong place.
      // One line per case, never both halves of the same failure.
      problems.push(
        `${testCase.name}: the card rendered nothing within ${MOUNT_TIMEOUT_MS}ms.` +
          (errors.length ? ` The page reported: ${errors.join(' | ')}` : ' The page reported no error at all.'),
      );
    }
    await context.close();
  }
} finally {
  await harness.close();
}

if (problems.length) {
  console.error(`The built card does not mount:\n  ${problems.join('\n  ')}`);
  process.exitCode = 1;
} else {
  console.log(`The built card mounts and renders (${CASES.length} cases, no page errors).`);
}
