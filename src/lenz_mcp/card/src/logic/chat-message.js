// The card's result, written as a message to the CHAT.
//
// Claude takes the result silently through `ui/update-model-context`. ChatGPT
// accepts that call, answers success and never delivers it — measured — and
// then tells the user "no full verification was run" beside a card that is
// showing a sourced verdict. Its only working channel is `ui/message`, which it
// SENDS AT ONCE as a user turn.
//
// So this text is read by a person, in their own voice, and the model REPLIES
// to it. Six rules follow, and every one of them is in the wording:
//  1. it reads as something a person would plausibly send;
//  2. it carries what the model needs to answer next: claim, verdict, score,
//     confidence, verification id;
//  3. it states that a check ran and finished, which is the failure it fixes;
//  4. it never reads as an instruction to call a tool, or the user pays twice;
//  5. it carries NO page text. A user turn is the most trusted position in a
//     conversation; the snapshot can quote sources behind an untrusted-evidence
//     header, a user turn cannot. `key_finding` is out too: our sentence, but
//     written from page text;
//  6. it makes the cheap reply the right one — acknowledge, do not re-summarise,
//     do not fetch. The id is offered "for later", never as something to get.
// Change the wording deliberately: each rule above is load-bearing.

import { line, shortClaim } from './snapshot.js';

// Our own quotation marks hold the claim, so a quote mark inside it would break
// out of them. There is no escape character in prose: the inner ones become
// single quotes, and `line` has already collapsed every newline to a space so
// page text cannot forge a second line in the user's turn.
const inner = (claim) => shortClaim(claim).replace(/"/g, "'");

const band = (confidence) => (confidence ? `${line(confidence)} confidence` : '');

// "8/10" or nothing. Never "null/10": a check with no score has no score.
const score = (value) => (Number.isInteger(value) && value >= 1 && value <= 10 ? `${value}/10` : '');

function verdictOf(check) {
  const parts = [line(check.verdict), score(check.score), band(check.confidence)].filter(Boolean);
  return parts.join(', ');
}

// Only completed checks are ever announced. A failed one is on the card, it
// concluded nothing, and a user turn saying "it failed" invites the model to run
// one itself, which charges the user for our outage.
export function buildChatMessage(checks) {
  const done = (Array.isArray(checks) ? checks : []).filter(
    (check) => check && line(check.verificationId) && line(check.verdict),
  );
  if (!done.length) return '';

  if (done.length === 1) {
    const check = done[0];
    const sentences = [
      'Lenz finished the deep check I started from the card.',
      `"${inner(check.claim)}": ${verdictOf(check)}.`,
    ];
    if (line(check.replacesQuick)) sentences.push(`It replaces the quick verdict (${line(check.replacesQuick)}).`);
    sentences.push('I have the full result and its sources in the card, so no need to repeat it.');
    sentences.push(`For later questions it is verification ${line(check.verificationId)}.`);
    return sentences.join(' ');
  }

  const rows = done.map(
    (check, i) => `${i + 1}. "${inner(check.claim)}": ${verdictOf(check)} (verification ${line(check.verificationId)})`,
  );
  return [
    'Lenz finished the deep checks I started from the card:',
    ...rows,
    'I have the full results and their sources in the card, so no need to repeat them.',
  ].join('\n');
}
