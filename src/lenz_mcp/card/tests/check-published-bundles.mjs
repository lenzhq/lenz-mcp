// A published card bundle never changes.
//
//   npm run check:published            against origin/main
//   BASE_REF=<ref> npm run check:published
//
// Claude caches a card by its URI and old chats re-mount the HTML that URI
// names, so every entry in dist/versions.json keeps being served. The
// connector's register_card_resources iterates all of them, on purpose, so a
// conversation that cached an old URI keeps working.
//
// Nothing else enforces that. build.mjs refuses to rewrite a published bundle,
// but only for the version package.json names; and the connector's card test
// hashes each file against the sha256 committed BESIDE it, so a commit that
// edits an old bundle AND its recorded hash together is self-consistent and
// green everywhere. The only witness to "these bytes were already published"
// is history — which is why this check lives in CI, where the base ref is
// fetched, rather than in pytest, where it would have to skip when history is
// unavailable, and a test that silently skips is the failure mode this whole
// job exists to remove.
//
// The rule:
//   - a URI in BOTH the base and this branch must carry a byte-identical
//     `file` and `sha256`;
//   - entries may be ADDED freely;
//   - an entry may be REMOVED only if its bundle is removed with it.
import { execFileSync } from 'node:child_process';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const base = process.env.BASE_REF || 'origin/main';
// Overridable so an arbitrary pair of refs can be compared by hand; in CI the
// checkout IS this ref. Every read below goes through git rather than the
// working tree, so the two sides are always described the same way — a
// file-existence check against the working tree would answer about a
// different tree than the manifest it is judging.
const head = process.env.HEAD_REF || 'HEAD';
const manifest = 'src/lenz_mcp/card/dist/versions.json';

const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8' });
const committed = (ref, path) => {
  try {
    execFileSync('git', ['cat-file', '-e', `${ref}:${path}`], { cwd: root, stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
};

let before;
try {
  before = JSON.parse(git('show', `${base}:${manifest}`));
} catch {
  // No manifest on the base ref: the card is new there, so every entry in this
  // branch is an addition and there is nothing that could have been published.
  console.log(`No ${manifest} on ${base}; nothing published yet to protect.`);
  process.exit(0);
}

const after = JSON.parse(git('show', `${head}:${manifest}`));
const problems = [];

// The one sanctioned exception to "a published bundle never changes":
// comment-only edits to published bundles. Each entry pins the bundle's old and
// new sha256. The edit reworded CSS comments and recomputed the
// Content-Security-Policy style-src hash to match, so the rendered output is
// unchanged: a CSS comment is invisible and the script is byte-identical.
// Exactly these transitions are allowed.
const COMMENT_ONLY_EDITS = {
  'ui://lenz/card-v2': {
    from: '8924c17991375bce7ea067fc81f338c960e899206d6abfa35ad552fa8aa5cb15',
    to: '28e4d795e9f398acfd3bebec0ab7cdf67a28349f37dfbc7feb3dfbe975aaa00e',
  },
  'ui://lenz/card-v3': {
    from: '8bd63ca00ccdd2bf4a2bd27e3c61a1deb21383969bbbb9443ce1105ca1a82017',
    to: 'e32005f43dd5d4ebebcaaf89027bb9ae8019f20b481839757cc2cb8b1c821a65',
  },
};
const sanctioned = (uri, from, to) => COMMENT_ONLY_EDITS[uri]?.from === from && COMMENT_ONLY_EDITS[uri]?.to === to;

for (const [uri, entry] of Object.entries(before)) {
  const now = after[uri];
  if (!now) {
    // Removing a version is allowed, but
    // only together with its bundle. An entry dropped while the file stays is
    // a version that is no longer served and no longer checked by anything.
    if (committed(head, `src/lenz_mcp/card/dist/${entry.file}`)) {
      problems.push(`${uri} was dropped from versions.json but dist/${entry.file} is still committed. Delete the bundle too, or restore the entry.`);
    }
    continue;
  }
  if (now.file !== entry.file) {
    problems.push(`${uri} named dist/${entry.file} on ${base} and names dist/${now.file} here.`);
  }
  if (now.sha256 !== entry.sha256 && !sanctioned(uri, entry.sha256, now.sha256)) {
    problems.push(
      `${uri} is published and its bundle has changed (${entry.sha256.slice(0, 12)}… → ${now.sha256.slice(0, 12)}…). ` +
        'Every Claude conversation that cached that URI re-mounts whatever it names, so add a NEW version rather than editing this one: ' +
        'bump "cardVersion" in package.json and CARD_URI in src/lenz_mcp/mcp_card.py.',
    );
  }
}

if (problems.length) {
  for (const problem of problems) console.error(`::error file=src/lenz_mcp/card/dist/versions.json::${problem}`);
  process.exitCode = 1;
} else {
  const kept = Object.keys(before).filter((uri) => after[uri]);
  const edited = kept.filter((uri) => after[uri].sha256 !== before[uri].sha256);
  const note = edited.length ? ` (${edited.length} with the sanctioned comment-only edit)` : '';
  console.log(`${kept.length} published card version(s) unchanged since ${base}${note}.`);
}
