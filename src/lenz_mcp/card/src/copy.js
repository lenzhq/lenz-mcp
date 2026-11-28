// Every string the card shows a person, in one place. The reader is someone
// using Lenz inside an assistant: the verb is "check", and there are no tool
// names, no links to plans, and credit numbers only when a check is refused for
// lack of credits, where the number is the answer. The same card runs in every
// host, so no string names an assistant: "ask for …", never "ask <name> …".
// The host shows the app's name above the card, so the card does not repeat it
// as a label; "Lenz" appears only inside a sentence.

// Quick check
export const QUICK_EYEBROW = 'Quick check';
export const QUICK_VERDICT_LABEL = 'quick verdict';
export const CONFIDENCE_LINE = {
  high: 'Confidence: high · a first read',
  medium: 'Confidence: medium · a first read',
  low: 'Confidence: low · Lenz is not sure about this one',
};
export const REASONING_LABEL = "Reviewers' reasoning";
export const DISSENT_PREFIX = 'One reviewer disagreed: ';
// The pages the reviewers looked at, closed by default under the reviewers'
// note. "Pages", never "sources" or "evidence": those belong to the deep check.
export const pagesSummary = (n) => `Pages the reviewers looked at (${n})`;
export const showAllPages = (n) => `Show all ${n}`;
export const SHOW_MORE = 'Show more';
export const SHOW_LESS = 'Show less';
// No timing on the button: it wrapped the label onto two rows on a phone, and
// the running state's USUALLY line already sets the expectation.
export const CHECK_BUTTON = 'Check against sources';
export const CHECK_DURATION_HINT = 'Takes a minute or two';

// Quick check, several claims
// The list card always has two or more, but the picker can start exactly one,
// and "1 claims checked" is the kind of thing a reader notices.
export const listHeading = (n) => (n === 1 ? '1 claim checked' : `${n} claims checked`);
export const itemsHeading = (n) => (n === 1 ? '1 item checked' : `${n} items checked`);
// The tally under the heading: verdicts in words, never colour.
// Wrong first, because that is what the reader came for; zeros are omitted.
export const TALLY_WORDS = {
  wrong: (n) => (n === 1 ? '1 looks wrong' : `${n} look wrong`),
  mixed: (n) => `${n} mixed`,
  hold: (n) => (n === 1 ? '1 holds up' : `${n} hold up`),
  norecord: (n) => (n === 1 ? '1 has no public record' : `${n} have no public record`),
  error: (n) => `${n} could not be checked`,
};
export const tallyLine = (counts) =>
  ['wrong', 'mixed', 'hold', 'norecord', 'error']
    .filter((key) => counts[key] > 0)
    .map((key) => TALLY_WORDS[key](counts[key]))
    .join(' · ');
// A collapsed row says what is unusual about it, in words.
export const ROW_NOT_SURE = 'not sure';
export const ROW_SPLIT = 'reviewers split';
// A row whose deep check is running keeps its quick verdict, named by the check
// that gave it (the card's eyebrow, no new term), so it is not read as the result
// of the check that is running.
export const quickCheckVerdict = (verdict) => `${QUICK_EYEBROW}: ${verdict}`;
export const rowSources = (n) => `Checked against ${n} ${n === 1 ? 'source' : 'sources'}`;
export const SHOW_FULL_CHECK = 'Show the full check';
export const HIDE_FULL_CHECK = 'Hide the full check';
export const showMoreRows = (n) => `Show ${n} more`;

// Action buckets (conversational grouping)
export const BUCKET_BAD = 'To fix';
export const BUCKET_LOOK = 'Closer look';
export const BUCKET_OK = 'Checks out';
export const BUCKET_NONE = 'Unchecked';
export const bucketOkToggle = (n) => `Checks out (${n})`;
export const checkAllFlagged = (n) => `Check all ${n} flagged against sources`;
// After the press, in the button's place. The count of finished checks is the
// progress: no bar, no percentage. The button starts two or more, but a reopened
// card restores only the rows that still have a check, which can be one.
export const batchRunning = (n, done) => {
  const head = n === 1 ? 'Checking 1 claim against sources' : `Checking ${n} claims against sources`;
  return done > 0 ? `${head} · ${done} of ${n} done` : head;
};
// When every check in the batch has stopped: what they found, in the tally's words,
// so a claim that moved to "Checks out" is still accounted for here.
export const batchDone = (counts) => {
  const rest = tallyLine(counts);
  return rest ? `Checked against sources · ${rest}` : 'Checked against sources';
};

// Mini-trace audit
export const AUDIT_TRACE_SUMMARY = 'How Lenz reached this';
export const auditCounts = (searched, kept) => {
  const parts = [];
  if (searched > 0) parts.push(`${searched} ${searched === 1 ? 'source' : 'sources'} searched`);
  if (kept > 0) parts.push(`${kept} cited in the check`);
  return parts.join(' · ');
};

// Citation check card
export const CITECHECK_EYEBROW = 'Citation check';
export const CITECHECK_CHECKING = 'Checking citations…';
// A citation check that did not finish (logic/citecheck-failure.js picks the
// frame). Never "ask for it to run again": the connector sends the same input
// with the same idempotency key, and the API replays a key's first answer for a
// day, a failed check included, so the same text gets the same failure back.
// The one exception is CITECHECK_RETRY_LATER's case (see there).
// No citations: one line, with the charge only where the record says 0.
export const CITECHECK_NO_CITATIONS_HEADING = 'No citations found in this text';
export const CITECHECK_WHAT_COUNTS = 'Lenz can check links, DOIs and numbered references.';
export const CITECHECK_WHAT_COUNTS_UNCHARGED = 'Lenz can check links, DOIs and numbered references. Nothing was charged.';
// A failure on Lenz's side that can pass (`upstream_unavailable`, `retryable`).
// "A few minutes" holds only once the API stops replaying such a failure to the
// same request for a day; release this card after that API change is deployed.
export const CITECHECK_RETRY_HEADING = 'Lenz could not check these citations';
export const CITECHECK_RETRY_LATER = 'Try again in a few minutes.';
export const CITECHECK_RETRY_LATER_UNCHARGED = 'Nothing was charged. Try again in a few minutes.';
export const CITECHECK_SAME_RESULT = 'Asking again with the same text gives this same result for up to a day.';
export const CITECHECK_OUR_SIDE = 'Something went wrong on our side.';
export const CITECHECK_INVALID_HEADING = 'This text could not be checked as sent';
export const CITECHECK_SEND_FULL_TEXT = 'Ask again with the full text, including its links or references.';
// A check the card holds an id for but cannot read: not found under this
// connection, or a read that failed. It may have finished, so not "did not finish".
export const CITECHECK_UNREADABLE_HEADING = 'Lenz could not open this citation check';
export const CITECHECK_GONE_HEADING = 'This citation check is no longer available';
export const CITECHECK_GONE_BODY = 'Checks on this account are removed after a set period.';
export const CITECHECK_ASK_AGAIN = 'Ask to check the citations again.';
// Shown only beside a FAILED check whose own record says it charged 0
// (`credits_charged`, the net of the ledger when it was read): never on a read
// that failed, which says nothing about the charge (see OUTAGE_BODY).
export const NOTHING_CHARGED = 'Nothing was charged for it.';
export const citecheckProgress = (done, total) => `${done} of ${total} citations checked`;
// The heading of a finished citation check: how many were checked, then the
// most serious thing left, in the order issues, a closer look, not checked.
// "all check out" (the group under it is "Checks out") needs every citation
// checked and supported: zero issues is also what a check that read nothing
// reports, and the heading must never say more than the rows under it.
// `total` is every citation in the list; `checked` the ones with a finding;
// `issues` and `look` the rows under "To fix" and "Closer look".
const wholeCount = (value) => (Number.isInteger(value) && value > 0 ? value : 0);
export const citecheckSummary = (counts) => {
  const c = counts && typeof counts === 'object' ? counts : {};
  const issues = wholeCount(c.issues);
  const look = wholeCount(c.look);
  // A row with an issue or a closer look was checked, whatever a count beside it says.
  const checked = Math.max(wholeCount(c.checked), issues + look);
  const total = Math.max(wholeCount(c.total), checked);
  if (checked === 0) return total === 1 ? 'The citation could not be checked' : 'No citations could be checked';
  const noun = total === 1 ? 'citation' : 'citations';
  const head = checked === total ? `${total} ${noun} checked` : `${checked} of ${total} ${noun} checked`;
  const unchecked = total - checked;
  const rest =
    issues > 0
      ? issues === 1
        ? '1 needs attention'
        : `${issues} need attention`
      : look > 0
        ? look === 1
          ? '1 needs a closer look'
          : `${look} need a closer look`
        : unchecked > 0
          ? `${unchecked} could not be checked`
          : total === 1
            ? 'it checks out'
            : 'all check out';
  return `${head} · ${rest}`;
};
export const CITECHECK_ISSUES_HEADING = 'Issues';
export const citecheckHoldsUpToggle = (n) => `Checks out (${n})`;

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
export const UNAVAILABLE = 'Lost touch with this check. It keeps running. In a minute or two, ask for your recent Lenz checks; if it is not listed yet, ask again a little later.';
export const UNRECOVERABLE = 'This check could not be recovered. Ask for your recent Lenz checks.';
export const OUTAGE = 'Sources could not be reached just now.';
export const TRY_AGAIN = 'Try again';
export const FAILED_HEADING = 'This check did not finish.';
// A closed table: the server's own message is never shown.
export const FAILED_BY_CLASS = {
  invalid_input: 'Lenz could not read a checkable claim in this text.',
  insufficient_evidence: 'Lenz found too few public sources to check this claim. Nothing was charged for it.',
};
export const FAILED_DEFAULT = 'Something went wrong on our side. Nothing was charged for it.';
export const QUOTA_EMPTY = 'You are out of Lenz credits.';
export const QUOTA_SHORT = 'Not enough Lenz credits for a deep check.';
export const QUOTA_NEXT = 'Ask how many Lenz credits you have left.';
// The refusal with its numbers, from the connector's own facts. The one place a
// credit number is shown: it is the answer to why the check did not run.
const creditCount = (n) => Number(n).toLocaleString('en-GB');
const creditsOf = (n) => `${creditCount(n)} credit${n === 1 ? '' : 's'}`;
export const QUOTA_DEEP_NEEDS = (needs, has) => `A deep check needs ${creditsOf(needs)} and you have ${creditCount(has)}.`;
export const QUOTA_CALL_NEEDS = (needs, has) => `This check needs ${creditsOf(needs)} and you have ${creditCount(has)}.`;
// The day then the month, in the reader's own time zone; the year only when the
// date is more than eleven months off, so "1 December" never means two years.
export const quotaResets = (ms, now = Date.now()) => {
  if (!Number.isFinite(ms)) return '';
  const limit = new Date(now);
  limit.setMonth(limit.getMonth() + 11);
  const options = ms > limit.getTime() ? { day: 'numeric', month: 'long', year: 'numeric' } : { day: 'numeric', month: 'long' };
  return `Your credits reset on ${new Intl.DateTimeFormat('en-GB', options).format(new Date(ms))}.`;
};

// Deep check, completed
export const DEEP_EYEBROW = 'Claim checked';
export const changedFrom = (verdict, now = '') =>
  now.trim().toLowerCase() === 'no public record'
    ? `Changed from the quick verdict: was ${verdict}. The deep check found no public record of this claim.`
    : `Changed from the quick verdict: was ${verdict}`;
export const deepConfidence = (bucket) => `Confidence: ${bucket}`;
export const scoreLabel = (score) => `Score ${score} out of 10`;
export const CAVEATS = 'Caveats';
// The same label as the claim page and the Workbench.
export const SUGGESTED_REWRITE = 'Suggested rewrite';
export const COPY_REWRITE = 'Copy';
export const COPIED_REWRITE = 'Copied';
export const showMoreCaveats = (n) => `Show ${n} more ${n === 1 ? 'caveat' : 'caveats'}`;
export const sourcesLabel = (total, shown) =>
  total > shown ? `${total} sources · showing ${shown}` : `${total} ${total === 1 ? 'source' : 'sources'}`;
export const showMoreSources = (n) => `Show ${n} more ${n === 1 ? 'source' : 'sources'}`;
export const NO_SOURCES = 'No sources could be read for this check.';
export const SEE_SOURCES_IN_LENZ = 'See sources in Lenz';
export const deepFooter = (total) =>
  total > 0 ? `Deep check · ${total} ${total === 1 ? 'source' : 'sources'}` : 'Deep check · no sources';

// A quick check that did not produce a verdict (row error codes, whole-call envelopes)
export const NOTHING_TO_CHECK = 'Nothing to check here';
// Keyed on what the row says failed: the input it could not read (no_claim is the
// server's word for no_checkable_claim; page_unreadable is a link whose page could
// not be read), else the failure's class. A code not listed reads as the default.
export const ROW_ERROR = {
  no_claim: 'Not a checkable statement',
  no_checkable_claim: 'Not a checkable statement',
  page_unreadable: 'The page could not be read',
  upstream_unavailable: 'Could not be checked just now',
  internal: 'Could not be checked',
};
export const ROW_ERROR_DEFAULT = 'Could not be checked';
// Under a quick No Public Record verdict: what the label means. Never "false".
export const NO_RECORD_QUICK = 'A quick search found no public record that would show this.';
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
export const IN_PROGRESS_BODY = 'Nothing new was started. In a minute or two, ask for your recent Lenz checks.';
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
