// Every string the card shows a person, in one place. The reader is someone
// using Lenz inside an assistant: the verb is "check", and there are no tool
// names, no credit numbers and no links to plans. The same card runs in every
// host, so no string names an assistant: "ask for …", never "ask <name> …".
// The host shows the app's name above the card, so the card does not repeat it
// as a label; "Lenz" appears only inside a sentence.

// Quick check
export const QUICK_EYEBROW = 'Quick check';
export const QUICK_FOOTER = 'Quick check · no sources';
export const QUICK_VERDICT_LABEL = 'quick verdict';
export const CONFIDENCE_LINE = {
  high: 'Confidence: high · a first read, no sources',
  medium: 'Confidence: medium · a first read, no sources',
  low: 'Confidence: low · Lenz is not sure about this one',
};
export const REASONING_LABEL = "Reviewers' reasoning";
export const DISSENT_PREFIX = 'One reviewer disagreed: ';
export const SHOW_MORE = 'Show more';
export const SHOW_LESS = 'Show less';
// No timing on the button: it wrapped the label onto two rows on a phone, and
// the running state's USUALLY line already sets the expectation.
export const CHECK_BUTTON = 'Check against sources';

// Quick check, several claims
// The list card always has two or more, but the picker can start exactly one,
// and "1 claims checked" is the kind of thing a reader notices.
export const listHeading = (n) => (n === 1 ? '1 claim checked' : `${n} claims checked`);
// The tally under the heading: verdicts in words, never colour.
// Wrong first, because that is what the reader came for; zeros are omitted.
export const TALLY_WORDS = {
  wrong: (n) => (n === 1 ? '1 looks wrong' : `${n} look wrong`),
  mixed: (n) => `${n} mixed`,
  hold: (n) => (n === 1 ? '1 holds up' : `${n} hold up`),
  error: (n) => `${n} could not be checked`,
};
export const tallyLine = (counts) =>
  ['wrong', 'mixed', 'hold', 'error']
    .filter((key) => counts[key] > 0)
    .map((key) => TALLY_WORDS[key](counts[key]))
    .join(' · ');
// A collapsed row says what is unusual about it, in words.
export const ROW_NOT_SURE = 'not sure';
export const ROW_SPLIT = 'reviewers split';
export const rowSources = (n) => `Checked against ${n} ${n === 1 ? 'source' : 'sources'}`;
export const SHOW_FULL_CHECK = 'Show the full check';
export const HIDE_FULL_CHECK = 'Hide the full check';
export const showMoreRows = (n) => `Show ${n} more`;

// Deep check, running
export const RUNNING_HEADING = 'Checking against sources';
export const STAGE_LABELS = {
  framing: 'Reading the claim',
  research: 'Finding sources',
  debate: 'Weighing both sides',
  adjudication: 'Reviewing',
  conclusion: 'Writing up',
};
export const stepLine = (label, index, total) => `${label} · step ${index} of ${total}`;
export const USUALLY = 'Usually about a minute to a minute and a half';
export const LONG_RUN = 'This is taking longer than usual. It keeps running.';

// Deep check, the ways it does not finish
export const UNAVAILABLE = 'Lost touch with this check. It keeps running. Once it finishes, ask for your recent Lenz checks.';
export const UNRECOVERABLE = 'This check could not be recovered. Ask for your recent Lenz checks.';
export const OUTAGE = 'Sources could not be reached just now.';
export const TRY_AGAIN = 'Try again';
export const FAILED_HEADING = 'This check did not finish.';
// A closed table: the server's own message is never shown.
export const FAILED_BY_CLASS = {
  invalid_input: 'Lenz could not read a checkable claim in this text.',
};
export const FAILED_DEFAULT = 'Something went wrong on our side. Nothing was charged for it.';
export const QUOTA_EMPTY = 'You are out of Lenz credits.';
export const QUOTA_SHORT = 'Not enough Lenz credits for a deep check.';
export const QUOTA_NEXT = 'Ask how many Lenz credits you have left.';

// Deep check, completed
export const DEEP_EYEBROW = 'Claim checked';
export const changedFrom = (verdict) => `Changed from the quick verdict: was ${verdict}`;
export const deepConfidence = (bucket) => `Confidence: ${bucket}`;
export const scoreLabel = (score) => `Score ${score} out of 10`;
export const CAVEATS = 'Caveats';
export const showMoreCaveats = (n) => `Show ${n} more ${n === 1 ? 'caveat' : 'caveats'}`;
export const sourcesLabel = (total, shown) =>
  total > shown ? `${total} sources · showing ${shown}` : `${total} ${total === 1 ? 'source' : 'sources'}`;
export const showMoreSources = (n) => `Show ${n} more ${n === 1 ? 'source' : 'sources'}`;
export const NO_SOURCES = 'No sources could be read for this check.';
export const deepFooter = (total) =>
  total > 0 ? `Deep check · ${total} ${total === 1 ? 'source' : 'sources'}` : 'Deep check · no sources';
export const FOLLOW_UP = 'Ask a follow-up';
export const followUpPrefill = (verificationId) => `Ask Lenz about this check (verification ${verificationId}): `;

// A quick check that did not produce a verdict (row error codes, whole-call envelopes)
export const NOTHING_TO_CHECK = 'Nothing to check here';
export const ROW_ERROR = {
  no_claim: 'Not a checkable statement',
  timeout: 'Could not be checked just now',
  upstream_unavailable: 'Could not be checked just now',
  framing_failed: 'Could not be read',
};
export const ROW_ERROR_DEFAULT = 'Could not be checked just now';
export const RECONNECT = 'Reconnect Lenz in your settings, under apps or connectors.';
export const ASK_AGAIN = 'Ask for it to run again.';

// Whole-call failures. Each gets its own frame, because one generic
// "did not finish" frame answers none of the three questions a failure
// has to answer: what happened, was it me, what now.
// The two retryable ones share a frame, because they share an answer.
export const OUTAGE_HEADING = 'Lenz could not reach its sources';
// NOT "nothing was charged": this envelope also comes from a failed READ of a
// check that already ran and was already paid for, and a read failing says
// nothing about the charge.
export const OUTAGE_BODY = 'Nothing was wrong with your claim.';
// The wait in words, from the server's own number. Never "shortly": in front of
// a daily cap that can be hours away, "shortly" is a lie.
export const retryIn = (seconds) => {
  if (!Number.isFinite(seconds) || seconds <= 0) return 'Try again in a minute.';
  if (seconds < 90) return 'Try again in about a minute.';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `Try again in about ${minutes} minutes.`;
  const hours = Math.round(seconds / 3600);
  return hours === 1 ? 'Try again in about an hour.' : `Try again in about ${hours} hours.`;
};
export const IN_PROGRESS_HEADING = 'This one is already being checked';
// Never "run it again": the whole point of this frame is that a check is
// already running, so a second one would be a second charge for one answer.
export const IN_PROGRESS_BODY = 'Nothing new was started. Once it finishes, ask for your recent Lenz checks.';
export const ALREADY_RESOLVED_HEADING = 'Those claims were already chosen';
export const ALREADY_RESOLVED_BODY = 'This check has moved on. Ask for your recent Lenz checks.';

// The picker. No credit numbers anywhere: the
// count lives in the button, and the checkboxes already say which ones.
export const PICKER_HEADING = 'This text makes several claims';
export const PICKER_LEAD = 'Each check covers one. Pick the ones that matter.';
export const PICKER_EMPTY_BUTTON = 'Choose a claim to check';
export const pickerButton = (n) => `Check ${n} ${n === 1 ? 'claim' : 'claims'}`;
export const pickerCap = (n) => `Up to ${n} at a time.`;
export const PICKER_SUBMITTING = 'Starting';
// NOT "nothing was charged": a lost reply may mean the checks DID start, which
// is the whole reason the selection locks. What is true, and what the line
// beside it says, is that pressing again costs nothing new.
export const PICKER_FAILED = 'That did not start.';
export const showMoreClaims = (n) => `Show ${n} more`;
// The checks the picker started. While any is running the heading must not say
// "checked": nothing is, yet.
export const pickerRunningHeading = (n) => (n === 1 ? 'Checking 1 claim' : `Checking ${n} claims`);
// These rows are deep checks, every one of them, so the quick-check footer
// would be wrong. The per-row source counts are on the rows.
export const DEEP_ROWS_FOOTER = 'Deep checks';
// A collapsed row whose check ended without a verdict.
export const ROW_DID_NOT_FINISH = 'did not finish';
// Some picks started and some did not: never silently drop the rest.
export const pickerPartial = (started, asked) =>
  `${started} of ${asked} started. Ask to check the rest.`;
// A heading counts VERDICTS, not rows that stopped: "3 claims checked" over
// three failures would be a lie.
export const pickerDoneHeading = (checked, total) =>
  checked === total ? listHeading(total) : `${checked} of ${total} claims checked`;
// A selection that was sent but never confirmed. Pressing again re-sends the
// SAME claims, which is what makes it free.
export const PICKER_RETRY = 'Try again';
export const PICKER_UNCONFIRMED = 'Trying again costs nothing new.';

// The one polite status region
// A list row names itself in the status region: two rows running at once must
// never be told apart by guesswork.
export const rowLabel = (n) => `Claim ${n}`;
export const announceRow = (text, label) => (label ? `${label}: ${text}` : text);
export const announceStage = (stage, label = '') => announceRow(stage, label);
export const announceResult = (verdict, score, confidence, label = '') =>
  announceRow(announceResultText(verdict, score, confidence), label);
const announceResultText = (verdict, score, confidence) =>
  score == null
    ? `Result: ${verdict}, confidence ${confidence}`
    : `Result: ${verdict}, ${score} out of 10, confidence ${confidence}`;

// Before the host has delivered the tool result
export const WAITING = 'Checking with Lenz';
