// WHICH mechanism this card must use to tell the model.
//
// The card cannot work it out: ChatGPT DECLARES `updateModelContext` and drops
// it silently, and a dropped push still returns success, so neither the
// capability nor the outcome is a signal. The SERVER knows — the card's own
// tool calls carry the client's User-Agent — and says so under `_card` in every
// card-only tool result (src/lenz_mcp/mcp_card.py, with_delivery).
//
// Read from the MOST RECENT card-tool response, never inferred. Absent or
// unrecognised means `context`: an old server with a new card keeps the quiet
// mechanism, and a host nobody has measured is never made to post messages into
// the user's own turn.
export const DELIVER_CONTEXT = 'context';
export const DELIVER_MESSAGE = 'message';

const MODES = new Set([DELIVER_CONTEXT, DELIVER_MESSAGE]);
const modes = new WeakMap();

// `_card` is the card's own namespace in a tool result, so it can never collide
// with a field a model-facing schema grows later.
export function deliveryOf(result) {
  const card = result && typeof result === 'object' ? result._card : null;
  const deliver = card && typeof card === 'object' ? card.deliver : null;
  return MODES.has(deliver) ? deliver : '';
}

export function readDelivery(host) {
  return modes.get(host) || DELIVER_CONTEXT;
}

// One wrapper for the whole card: every tool call goes through the host, so
// this is the single place the hint is read.
//
// Object.create, NOT a spread. The adapter exposes `capabilities` as a GETTER
// that reads what the host declared at connect; spreading evaluates it once, at
// wrap time, which is BEFORE connect — every capability reads false for the
// life of the card and every button silently disappears. The prototype chain
// keeps it live.
export function watchDelivery(host) {
  const watched = Object.create(host);
  watched.callTool = async (name, args) => {
    const out = await host.callTool(name, args);
    const deliver = deliveryOf(out);
    if (deliver) modes.set(watched, deliver);
    return out;
  };
  return watched;
}
