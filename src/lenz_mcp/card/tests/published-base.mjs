// Which ref "already published" is judged against.
//
// A card version is published when a RELEASE TAG has it: only then has the
// bundle been built into a wheel and deployed, so only then can a host have
// cached its URI. `main` is not that witness. A version merged to main and not
// yet tagged is still a draft and may be rebuilt (build.mjs --allow-rebuild);
// the first tag that contains it freezes it.
//
// The base is, first match:
//   1. BASE_REF, when set (hand comparisons, and any caller that knows better);
//   2. the highest release tag (exactly vMAJOR.MINOR.PATCH, the shape
//      release.yml accepts) reachable from `head`;
//   3. origin/main, only when no release tag is reachable (nothing could have
//      been published by a tag, so the stricter, older rule applies).
//
// A checkout without tags (a shallow clone, a fetch with --no-tags) falls into
// case 3 and says so; CI checks out with fetch-depth: 0, which brings the tags.
import { execFileSync } from 'node:child_process';

const RELEASE_TAG = /^v\d+\.\d+\.\d+$/;

export function chooseBase({ cwd, head = 'HEAD', env = process.env } = {}) {
  if (env.BASE_REF) return { ref: env.BASE_REF, via: 'BASE_REF' };
  try {
    const tags = execFileSync('git', ['tag', '--merged', head, '--list', 'v*', '--sort=-v:refname'], {
      cwd,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    })
      .split('\n')
      .filter((tag) => RELEASE_TAG.test(tag));
    if (tags.length) return { ref: tags[0], via: 'release tag' };
  } catch {
    // Not a repository, or head does not resolve: fall through.
  }
  return { ref: 'origin/main', via: 'no release tag reachable; origin/main' };
}
