// The host lives behind one file. Rendering, state and polling speak the typed
// operations in interface.js and nothing else, so a surprise in a real host is a
// one-file fix (and a second host is one more adapter, not a sweep through the card).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import { HOST_OPERATIONS } from './interface.js';

const SRC = dirname(dirname(fileURLToPath(import.meta.url)));
const HOST_DIR = join(SRC, 'host') + sep;
// Everything the protocol is made of: only the adapter may name any of it.
const PROTOCOL = [
  /\bpostMessage\b/,
  /\bwindow\.parent\b/,
  /\bparent\.\w/,
  /['"`]ui\/[a-z-]/i,
  /\bjsonrpc\b/i,
  /addEventListener\(\s*['"]message['"]/,
  /\bMessageEvent\b/,
  // A host's own palette variables and its light-dark() values: the theme seam
  // (host/theme.js) maps them, so nothing above the adapter names one.
  /--color-[a-z-]+/,
  /\blight-dark\(/,
];

function files(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return files(path);
    // Test files build fake hosts and name the protocol on purpose.
    return /\.(js|jsx)$/.test(name) && !/\.test\.(js|jsx)$/.test(name) ? [path] : [];
  });
}

test('the guard covers every file outside the adapter directory', () => {
  const scanned = files(SRC).filter((path) => !path.startsWith(HOST_DIR));
  assert.ok(scanned.length >= 5, 'the scan reaches the card');
  // A sibling like src/hostile.js must not be mistaken for src/host/.
  for (const path of scanned) assert.ok(!/\/host\.[jt]sx?$/.test(path) || path.startsWith(HOST_DIR), path);
  assert.ok(files(SRC).some((path) => path.startsWith(HOST_DIR)), 'the adapter itself exists');
});

test('only the host adapter speaks the host protocol', () => {
  const offenders = [];
  for (const path of files(SRC)) {
    if (path.startsWith(HOST_DIR)) continue;
    const text = readFileSync(path, 'utf8');
    for (const [lineno, lineText] of text.split('\n').entries()) {
      // A comment may NAME a method; only code may speak it.
      if (/^\s*(\/\/|\*|\/\*)/.test(lineText)) continue;
      for (const pattern of PROTOCOL) {
        if (pattern.test(lineText)) offenders.push(`${relative(SRC, path)}:${lineno + 1}: ${lineText.trim().slice(0, 90)}`);
      }
    }
  }
  assert.deepEqual(offenders, [], `the host protocol belongs in src/host/:\n${offenders.join('\n')}`);
});

test('the card calls the host only through the named operations', () => {
  const allowed = new Set([...HOST_OPERATIONS, 'capabilities']);
  const used = new Set();
  for (const path of files(SRC)) {
    if (path.startsWith(HOST_DIR)) continue;
    for (const lineText of readFileSync(path, 'utf8').split('\n')) {
      if (/^\s*(\/\/|\*|\/\*)/.test(lineText)) continue;
      for (const match of lineText.matchAll(/\bhost\.([A-Za-z_]\w*)/g)) used.add(match[1]);
      // Reaching the host any other way hides which operations the card uses.
      for (const match of lineText.matchAll(/\bhost\s*\[/g)) used.add(`[computed] ${match[0]}`);
      for (const match of lineText.matchAll(/(?:const|let|var)\s*\{([^}]*)\}\s*=\s*host\b/g)) {
        for (const name of match[1].split(',')) {
          const clean = name.split(':')[0].trim();
          if (clean) used.add(clean);
        }
      }
    }
  }
  assert.ok(used.size > 0, 'the card uses the host somewhere');
  for (const name of used) assert.ok(allowed.has(name), `host.${name} is not in HOST_OPERATIONS (interface.js)`);
});
