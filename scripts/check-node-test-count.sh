#!/usr/bin/env bash
# Fail unless a `node --test` run PASSED at least `floor` tests.
#
#   bash scripts/check-node-test-count.sh <log> <floor> <label>
#
# Why this exists: `node --test` reports a clean, green, zero-test run when its
# file glob matches nothing. Measured 2026-09-18 on Node 22 with the card's own
# `node --test "src/**/*.test.js"` — rename src/ and the run exits 0 printing
# `# tests 0`, indistinguishable from a passing suite in the CI summary. An
# ordinary directory rename reaches it.
#
# (Node 20 fails the same situation LOUDLY: it does not expand the glob at all
# and exits 1 with `Could not find 'src/**/*.test.js'`. That is why the card's CI job
# pins Node 22 for the tests to run; it is not why this script exists.)
#
# It counts PASSING tests, not the `tests` total, because the total includes
# skipped and todo: 70 `test.skip(...)` registrations report `tests 70`,
# `pass 0`, `skipped 70` and exit 0, which a total-based floor would accept.
#
# It reads the SUMMARY, never the process exit code: a suite that prints a
# green summary and then dies in teardown is caught by the caller (`set -o
# pipefail` in the workflow step), not here.
#
# A FLOOR, deliberately, not the exact count: pinning the count would fail every
# PR that adds a test, which teaches people to edit the number without reading
# it. Raise the floor when a suite grows substantially.
set -euo pipefail

LOG="${1:?usage: check-node-test-count.sh <log> <floor> <label>}"
FLOOR="${2:?usage: check-node-test-count.sh <log> <floor> <label>}"
LABEL="${3:-node --test}"

fail() {
    echo "::error::${LABEL}: $1" >&2
    exit 1
}

[ -r "${LOG}" ] || fail "cannot read ${LOG}."

# Strip ANSI colour before parsing. The spec reporter colours its summary on a
# TTY, and the escape sequence's own digits stop a naive prefix match — so a
# developer running this locally would see a PASSING run rejected.
CLEAN="$(mktemp)"
trap 'rm -f "${CLEAN}"' EXIT
sed -E $'s/\x1b\\[[0-9;]*[A-Za-z]//g' "${LOG}" >"${CLEAN}"

# `# tests N` without a TTY, `ℹ tests N` with one. Anchored, so a nested
# subtest line or a test NAME containing "tests 0" cannot be read as a summary.
counts_for() {
    sed -nE "s/^[[:space:]]*(#|ℹ) $1 ([0-9]+)[[:space:]]*$/\2/p" "${CLEAN}"
}

TOTALS="$(counts_for tests)"
BLOCKS="$(printf '%s' "${TOTALS}" | grep -c . || true)"

if [ "${BLOCKS}" -eq 0 ]; then
    fail "printed no test summary at all — the suite did not run. See ${LOG}."
fi
if [ "${BLOCKS}" -gt 1 ]; then
    # One invocation prints one summary. Several means something else ran too
    # (an npm lifecycle script, a changed test command), and then "the last
    # summary" is whichever suite happened to finish last — a passing second
    # suite would conceal a zero-test first one.
    fail "printed ${BLOCKS} test summaries; expected exactly one. Something beyond the suite under test is running. See ${LOG}."
fi

# Each of these must be a single value too. `BLOCKS` only counted the `tests`
# lines, so a test that printed `# pass 999` of its own would make PASSED a
# two-line string and the arithmetic below would die with bash's own
# "integer expression expected" — safe, but with no annotation saying why.
one_value() {
    local values count
    values="$(counts_for "$1")"
    count="$(printf '%s' "${values}" | grep -c . || true)"
    [ "${count}" -le 1 ] || fail "printed ${count} '$1' lines; expected one. See ${LOG}."
    printf '%s' "${values}"
}

PASSED="$(one_value pass)"
FAILED="$(one_value fail)"
SKIPPED="$(one_value skipped)"
TODO="$(one_value todo)"

[ -n "${PASSED}" ] || fail "printed a test count but no pass count. See ${LOG}."

if [ -n "${FAILED}" ] && [ "${FAILED}" -gt 0 ]; then
    fail "reported ${FAILED} failing test(s). See ${LOG}."
fi

if [ "${PASSED}" -lt "${FLOOR}" ]; then
    detail="${PASSED} tests passed, below the floor of ${FLOOR}"
    [ -n "${SKIPPED:-}" ] && [ "${SKIPPED}" -gt 0 ] && detail="${detail} (${SKIPPED} skipped)"
    [ -n "${TODO:-}" ] && [ "${TODO}" -gt 0 ] && detail="${detail} (${TODO} todo)"
    fail "${detail}. Either tests were deleted or disabled — say so in the PR and lower the floor in .github/workflows/ci.yml — or the runner stopped finding them."
fi

echo "${LABEL}: ${PASSED} tests passed (floor ${FLOOR})."
