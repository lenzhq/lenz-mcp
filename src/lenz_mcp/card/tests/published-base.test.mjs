// The base a card version is judged "published" against: the nearest release
// tag (the highest one HEAD reaches), not main. Runs on throwaway git repositories.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, test } from 'node:test';
import { chooseBase } from './published-base.mjs';

const dirs = [];
after(() => dirs.forEach((dir) => rmSync(dir, { recursive: true, force: true })));

function repo() {
  const dir = mkdtempSync(join(tmpdir(), 'published-base-'));
  dirs.push(dir);
  const git = (...args) => execFileSync('git', args, { cwd: dir, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  git('init', '-q', '-b', 'main');
  git('config', 'user.email', 't@example.com');
  git('config', 'user.name', 't');
  git('config', 'commit.gpgsign', 'false');
  git('config', 'tag.gpgsign', 'false');
  let n = 0;
  const commit = () => {
    writeFileSync(join(dir, 'f'), `${++n}\n`);
    git('add', 'f');
    git('commit', '-q', '-m', `c${n}`);
  };
  return { dir, git, commit };
}

test('the highest release tag is the base, not main', () => {
  const { dir, git, commit } = repo();
  commit();
  git('tag', 'v1.0.0');
  commit();
  git('tag', 'v1.1.0');
  commit(); // merged to main, not tagged: still a draft
  assert.deepEqual(chooseBase({ cwd: dir, env: {} }), { ref: 'v1.1.0', via: 'release tag' });
});

test('an annotated tag counts, and a later commit does not move the base', () => {
  const { dir, git, commit } = repo();
  commit();
  git('tag', '-a', 'v2.0.0', '-m', 'release');
  commit();
  commit();
  assert.equal(chooseBase({ cwd: dir, env: {} }).ref, 'v2.0.0');
});

test('only release-shaped tags count', () => {
  const { dir, git, commit } = repo();
  commit();
  git('tag', 'v1.0.0');
  commit();
  git('tag', 'vnext');
  git('tag', 'v2.0.0-rc1');
  git('tag', 'release-9');
  assert.equal(chooseBase({ cwd: dir, env: {} }).ref, 'v1.0.0');
});

test('a tag that HEAD cannot reach is not the base', () => {
  const { dir, git, commit } = repo();
  commit();
  git('tag', 'v1.0.0');
  git('checkout', '-q', '-b', 'side');
  commit();
  git('tag', 'v1.0.1'); // released from another line
  git('checkout', '-q', 'main');
  commit();
  assert.equal(chooseBase({ cwd: dir, env: {} }).ref, 'v1.0.0');
});

test('with no release tag at all the base falls back to origin/main', () => {
  const { dir, commit } = repo();
  commit();
  const base = chooseBase({ cwd: dir, env: {} });
  assert.equal(base.ref, 'origin/main');
  assert.match(base.via, /no release tag/);
});

test('BASE_REF wins over a tag', () => {
  const { dir, git, commit } = repo();
  commit();
  git('tag', 'v1.0.0');
  assert.deepEqual(chooseBase({ cwd: dir, env: { BASE_REF: 'origin/main' } }), { ref: 'origin/main', via: 'BASE_REF' });
});

test('the tag is looked up from the head ref it is given', () => {
  const { dir, git, commit } = repo();
  commit();
  git('tag', 'v1.0.0');
  const early = git('rev-parse', 'HEAD');
  commit();
  git('tag', 'v1.1.0');
  assert.equal(chooseBase({ cwd: dir, head: early, env: {} }).ref, 'v1.0.0');
});
