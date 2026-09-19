// Build the Lenz card for Claude into a committed, versioned bundle.
//
//   npm run build                       build the version in package.json "cardVersion"
//   npm run build -- --dev              the dev probe's card with the fixture switcher (dist-dev/)
//   npm run build -- --allow-rebuild    overwrite that version's file (ONLY before its URI
//                                       has ever been published; see below)
//
// Output: dist/card-v{N}.html (script and styles inline, pinned by a CSP of
// their sha256 hashes) and its entry in dist/versions.json:
//   { "ui://lenz/card-v{N}": { "file": "card-v{N}.html", "sha256": "…" } }
//
// Claude caches a card by its URI and old chats re-mount the HTML their URI names,
// so a published version's bundle never changes. Changing the card means bumping
// "cardVersion" here AND CARD_URI in src/lenz_mcp/mcp_card.py (a literal, by hand);
// tests/test_card.py fails if a bundle changes under an old URI or
// the Python URI is not the newest version.
import { build } from 'esbuild';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const dist = join(root, 'dist');
mkdirSync(dist, { recursive: true });

const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
const version = Number(pkg.cardVersion);
if (!Number.isInteger(version) || version < 1) throw new Error('package.json cardVersion must be a positive integer');
const uri = `ui://lenz/card-v${version}`;
const file = `card-v${version}.html`;
const allowRebuild = process.argv.includes('--allow-rebuild');
// --dev: the dev probe's bundle (scripts/probe), with the fixture switcher,
// written to dist-dev/ and never pinned or served by lenz-mcp.
const devBuild = process.argv.includes('--dev');

const result = await build({
  entryPoints: [join(root, 'src', 'main.jsx')],
  bundle: true,
  format: 'iife',
  platform: 'browser',
  target: ['es2019'],
  jsx: 'automatic',
  jsxImportSource: 'preact',
  minify: true,
  legalComments: 'none',
  write: false,
  define: { 'process.env.NODE_ENV': '"production"' },
  alias: { 'lenz-card-dev': join(root, 'src', 'dev', devBuild ? 'switcher.jsx' : 'off.js') },
  loader: { '.json': 'json' },
});
// A literal "</script" inside the bundle would end the inline script early.
const js = result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
const { CSS } = await import(pathToFileURL(join(root, 'src', 'styles.js')).href);

const hash = (text) => `'sha256-${createHash('sha256').update(text, 'utf8').digest('base64')}'`;
const csp = [
  "default-src 'none'",
  `script-src ${hash(js)}`,
  `style-src ${hash(CSS)}`,
  "img-src 'none'",
  "font-src 'none'",
  "connect-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
].join('; ');

const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="lenz-card" content="v${version}">
<title>Lenz result</title>
<style>${CSS}</style>
</head>
<body>
<main id="lenz-card"></main>
<script>${js}</script>
</body>
</html>
`;

if (devBuild) {
  const devDist = join(root, 'dist-dev');
  mkdirSync(devDist, { recursive: true });
  const devHtml = html.replace(`content="v${version}"`, `content="v${version}-dev"`);
  writeFileSync(join(devDist, 'card-dev.html'), devHtml, 'utf8');
  console.log(`Built dist-dev/card-dev.html (${(Buffer.byteLength(devHtml) / 1024).toFixed(1)} KB), the dev probe's card`);
  process.exit(0);
}

const versionsPath = join(dist, 'versions.json');
const versions = existsSync(versionsPath) ? JSON.parse(readFileSync(versionsPath, 'utf8')) : {};
const sha256 = createHash('sha256').update(html, 'utf8').digest('hex');
const existing = versions[uri];
if (existing && existing.sha256 !== sha256 && !allowRebuild) {
  throw new Error(
    `${file} is already recorded for ${uri} with another hash. A published card never changes: ` +
      'bump "cardVersion" in package.json and CARD_URI in src/lenz_mcp/mcp_card.py. ' +
      '(Use --allow-rebuild only for a version that has never been published.)',
  );
}
writeFileSync(join(dist, file), html, 'utf8');
versions[uri] = { file, sha256 };
writeFileSync(versionsPath, `${JSON.stringify(versions, null, 2)}\n`, 'utf8');
console.log(`Built ${file} (${(Buffer.byteLength(html) / 1024).toFixed(1)} KB) for ${uri}`);
