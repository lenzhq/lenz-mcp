// Which mounting a card belongs to.
//
// What the model has been told is remembered in the sandbox origin's
// localStorage, and that outlives a conversation: a new chat about the same
// claim finds the old chat's records and, without more, stays silent about a
// finished check the new chat's model has never heard of. The server stamps the
// result that mounts a card with two values under the card's own namespace
// (src/lenz_mcp/mcp_card.py, with_mounting_identity):
//
// - `conversation`: a short hash of the host's conversation id, present only
//   when the host sends one on the tool call (ChatGPT and Codex do; Claude's
//   app does not). It scopes what a chat has been told. Never the id itself.
// - `call_id`: a nonce minted once per mounting tool result. A host that shows
//   the same result again shows the same id; a new tool call has a new one.
//
// Both are read as short tokens or not at all, never coerced.
const TOKEN = /^[A-Za-z0-9_-]{1,64}$/;

const token = (value) => (typeof value === 'string' && TOKEN.test(value) ? value : '');

export function mountIdentity(payload) {
  const card = payload && typeof payload === 'object' ? payload._card : null;
  const stamp = card && typeof card === 'object' ? card : {};
  const conversation = token(stamp.conversation);
  const callId = token(stamp.call_id);
  return { conversation, callId, key: conversation || callId ? `${conversation}|${callId}` : '' };
}
