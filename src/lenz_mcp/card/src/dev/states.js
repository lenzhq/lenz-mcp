// Every card state the gallery (tests/render.mjs) and the dev fixture switcher
// (src/dev/switcher.jsx) show, built from fixtures/fixtures.json (payloads made by
// lenz-mcp's own tool code; see fixtures/build_fixtures.py). A state is a tool
// result, plus, for a deep-check state, the scripted answers to the card's two
// tool calls and a press. Dev and test code only: the production bundle never
// imports this file.

// A deep result belongs to the claim the quick card showed.
function withClaim(toolResult, claim) {
  const copy = JSON.parse(JSON.stringify(toolResult));
  if (claim && copy.claims && copy.claims[0]) copy.claims[0].claim = claim;
  return copy;
}

export function buildStates(fixtures) {
  const { quick, deep, polls, starts } = fixtures;
  const submitted = starts.submitted.result;
  const states = [];
  for (const [name, f] of Object.entries(quick)) {
    states.push({ name, group: name.startsWith('list') ? 'Interim list' : 'Quick check', provenance: f.provenance, toolResult: f.toolResult });
  }
  states.push({
    name: 'quick-low-no-server-tools',
    group: 'Quick check',
    provenance: 'composed from the quick-low rows, on a host that does not let a card call tools',
    toolResult: quick['quick-low'].toolResult,
    capabilities: { message: {}, updateModelContext: {}, openLinks: {} },
  });
  states.push({ name: 'waiting', group: 'Quick check', provenance: 'no tool result yet', noResult: true });

  const base = quick['quick-low'];
  for (const [name, f] of Object.entries(polls)) {
    if (name === 'poll-error') continue;
    states.push({
      name,
      group: name.startsWith('running') ? 'Running' : 'Did not finish',
      provenance: f.provenance,
      toolResult: base.toolResult,
      press: true,
      tools: { start_verification_widget: [submitted], get_verification_widget: [f.result] },
    });
  }
  states.push({
    name: 'unavailable',
    group: 'Did not finish',
    provenance: `${polls['poll-error'].provenance}, every poll, past the error cap`,
    toolResult: base.toolResult,
    press: true,
    fastForwardMs: 120000,
    tools: { start_verification_widget: [submitted], get_verification_widget: [polls['poll-error'].result] },
  });
  for (const name of ['quota-empty', 'quota-short', 'start-error']) {
    states.push({
      name,
      group: 'Did not finish',
      provenance: starts[name].provenance,
      toolResult: base.toolResult,
      press: true,
      tools: { start_verification_widget: [starts[name].result] },
    });
  }
  // The list card's own states: a row open, a row running, a row checked.
  const listQuick = quick['list-8-with-errors'].toolResult;
  states.push({
    name: 'list-row-open',
    group: 'Interim list',
    provenance: `${quick['list-8-with-errors'].provenance}; row 5 open`,
    toolResult: listQuick,
    openRow: 4,
  });
  states.push({
    name: 'list-row-error-open',
    group: 'Interim list',
    provenance: `${quick['list-8-with-errors'].provenance}; the no-claim row open`,
    toolResult: listQuick,
    openRow: 7,
  });
  states.push({
    name: 'list-row-running',
    group: 'Interim list',
    provenance: `${polls['running-research'].provenance}; row 5 checking`,
    toolResult: listQuick,
    openRow: 4,
    press: true,
    tools: { start_verification_widget: [submitted], get_verification_widget: [polls['running-research'].result] },
  });
  const listDone = { ...deep['deep-28-sources-3-warnings'].result, verdict: 'True', lenz_score: 9 };
  states.push({
    name: 'list-row-checked',
    group: 'Interim list',
    provenance: `${deep['deep-28-sources-3-warnings'].provenance}; row 5 checked, its full check open`,
    toolResult: listQuick,
    openRow: 4,
    press: true,
    openFullCheck: true,
    tools: { start_verification_widget: [submitted], get_verification_widget: [listDone] },
  });

  // A deep check the model ran: its own card.
  const { payloads } = fixtures;
  states.push({
    name: 'deep-card-completed',
    group: 'Deep result',
    provenance: `${payloads['deep-card-completed'].provenance}; the model ran this check`,
    toolResult: payloads['deep-card-completed'].result,
  });
  states.push({
    name: 'deep-card-running',
    group: 'Running',
    provenance: `${payloads['deep-card-submitted'].provenance}; the card adopts the run`,
    toolResult: payloads['deep-card-submitted'].result,
    tools: { get_verification_widget: [polls['running-debate'].result] },
  });
  states.push({
    name: 'deep-card-failed',
    group: 'Did not finish',
    provenance: `${payloads['deep-card-submitted'].provenance} then ${polls['failed-not-retryable'].provenance}`,
    toolResult: payloads['deep-card-submitted'].result,
    tools: { get_verification_widget: [polls['failed-not-retryable'].result] },
    settle: 3600,
  });

  // ── The picker ────────────────────────────────────────────
  const pickerTools = (n, claims = []) => ({
    select_claims_widget: [
      {
        status: 'submitted',
        claims: Array.from({ length: n }, (_, i) => ({ task_id: `p${i}`.padEnd(32, '0'), claim: claims[i] || '' })),
      },
    ],
    get_verification_widget: [polls['running-research'].result],
  });
  const pickerClaims = (payloads['picker-long'].result.claims || []).map((c) => c.text);

  states.push({
    name: 'picker',
    group: 'Picker',
    provenance: payloads['deep-card-needs-input'].provenance,
    toolResult: payloads['deep-card-needs-input'].result,
  });
  states.push({
    name: 'picker-long',
    group: 'Picker',
    provenance: `${payloads['picker-long'].provenance}; 8 shown, the rest behind the disclosure`,
    toolResult: payloads['picker-long'].result,
  });
  states.push({
    name: 'picker-at-cap',
    group: 'Picker',
    provenance: `${payloads['picker-long'].provenance}; five ticked, so the rest are disabled`,
    toolResult: payloads['picker-long'].result,
    pick: 5,
  });
  states.push({
    name: 'picker-one-chosen',
    group: 'Picker',
    provenance: `${payloads['deep-card-needs-input'].provenance}; one ticked`,
    toolResult: payloads['deep-card-needs-input'].result,
    pick: 1,
  });
  // Five running checks in one card: the layout the picker's cap allows.
  states.push({
    name: 'picker-five-running',
    group: 'Picker',
    provenance: `${payloads['picker-long'].provenance}; five started, all still running`,
    toolResult: payloads['picker-long'].result,
    pick: 5,
    pickSubmit: true,
    tools: pickerTools(5, pickerClaims),
  });
  states.push({
    name: 'picker-five-checked',
    group: 'Picker',
    provenance: `${payloads['picker-long'].provenance}; five started, all completed`,
    toolResult: payloads['picker-long'].result,
    pick: 5,
    pickSubmit: true,
    tools: {
      ...pickerTools(5, pickerClaims),
      get_verification_widget: [payloads['deep-card-completed'].result],
    },
  });
  states.push({
    name: 'picker-five-checked-open',
    group: 'Picker',
    provenance: `${payloads['picker-long'].provenance}; five completed, the first row open on its full check`,
    toolResult: payloads['picker-long'].result,
    pick: 5,
    pickSubmit: true,
    openRow: 0,
    openFullCheck: true,
    tools: {
      ...pickerTools(5, pickerClaims),
      get_verification_widget: [payloads['deep-card-completed'].result],
    },
  });
  states.push({
    name: 'picker-unconfirmed',
    group: 'Picker',
    provenance: `${payloads['deep-card-needs-input'].provenance}; the reply was lost, so the selection is locked`,
    toolResult: payloads['deep-card-needs-input'].result,
    pick: 2,
    pickSubmit: true,
    tools: { select_claims_widget: [{}] },
  });
  states.push({
    name: 'picker-partial',
    group: 'Picker',
    provenance: `${payloads['picker-long'].provenance}; three ticked, one started`,
    toolResult: payloads['picker-long'].result,
    pick: 3,
    pickSubmit: true,
    tools: {
      select_claims_widget: [{ status: 'partial', claims: [{ task_id: 'p0'.padEnd(32, '0'), claim: pickerClaims[0] }] }],
      get_verification_widget: [payloads['deep-card-completed'].result],
    },
  });
  states.push({
    name: 'picker-failed-to-start',
    group: 'Picker',
    provenance: `${payloads['deep-card-needs-input'].provenance}; the submit failed, the picks are kept`,
    toolResult: payloads['deep-card-needs-input'].result,
    pick: 2,
    pickSubmit: true,
    tools: { select_claims_widget: [{ status: 'error', message: 'nope' }] },
  });

  // Whole-call failures, one frame each.
  states.push({
    name: 'call-outage',
    group: 'Did not finish',
    provenance: 'hand-built (503 service_unavailable envelope)',
    toolResult: { status: 'service_unavailable', message: 'x', retry_after_seconds: 90 },
  });
  states.push({
    name: 'call-rate-limited',
    group: 'Did not finish',
    provenance: 'hand-built (429 rate_limited envelope, a daily cap hours away)',
    toolResult: { status: 'rate_limited', message: 'x', retry_after_seconds: 7200 },
  });
  states.push({
    name: 'call-in-progress',
    group: 'Did not finish',
    provenance: 'hand-built (409 in_progress envelope)',
    toolResult: { status: 'in_progress', message: 'x' },
  });
  states.push({
    name: 'call-already-resolved',
    group: 'Did not finish',
    provenance: 'hand-built (409 no_selection_pending envelope)',
    toolResult: { status: 'already_resolved', message: 'x' },
  });

  for (const [name, f] of Object.entries(deep)) {
    states.push({
      name,
      group: 'Deep result',
      provenance: `${f.provenance}; quick card: ${f.quick}, its claim text set to this check's claim`,
      toolResult: withClaim(quick[f.quick].toolResult, f.result.claim),
      press: true,
      tools: { start_verification_widget: [submitted], get_verification_widget: [f.result] },
    });
  }
  return states;
}
