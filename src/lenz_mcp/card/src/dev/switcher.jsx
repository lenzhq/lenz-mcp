// DEV ONLY (the dev probe's bundle, dist-dev/): a switcher above the card that
// steps through every fixture state in a real host. The production bundle
// resolves `lenz-card-dev` to off.js instead, and a test checks that no fixture
// code or text reaches dist/.
//
// "Live" leaves the card as the host delivered it: a press goes to the probe's
// fake start/get tools, which replay a canned run. Any other entry replaces the
// payload with that fixture and answers the card's tool calls from its script,
// inside the card, so no tool is called at all.
import { useState } from 'preact/hooks';

import fixtures from '../../fixtures/fixtures.json';
import { buildStates } from './states.js';

const STATES = buildStates(fixtures);
const ANSWER_DELAY_MS = 600;

export function createDevHost(base) {
  let script = null;
  let capabilities = null;
  const host = Object.create(base);
  Object.defineProperty(host, 'capabilities', {
    get: () => capabilities || base.capabilities,
  });
  host.callTool = (name, args) => {
    if (!script) return base.callTool(name, args);
    const queue = script[name] || [];
    const answer = queue.length > 1 ? queue.shift() : queue[0];
    if (answer === undefined) return new Promise(() => {});
    return new Promise((resolve) => setTimeout(() => resolve(JSON.parse(JSON.stringify(answer))), ANSWER_DELAY_MS));
  };
  return {
    host,
    use(state) {
      // A stored run from another fixture with the same claim would be recovered instead.
      try {
        for (const key of Object.keys(globalThis.localStorage)) {
          if (key.startsWith('lenz-card:')) globalThis.localStorage.removeItem(key);
        }
      } catch (_e) {
        // no storage in this frame
      }
      script = state ? JSON.parse(JSON.stringify(state.tools || {})) : null;
      capabilities = state && state.capabilities
        ? { message: !!state.capabilities.message, updateModelContext: !!state.capabilities.updateModelContext, openLinks: !!state.capabilities.openLinks, serverTools: !!state.capabilities.serverTools }
        : null;
    },
  };
}

export function DevSwitcher({ dev, onSelect }) {
  const [name, setName] = useState('');
  const groups = [...new Set(STATES.map((s) => s.group))];
  const current = STATES.find((s) => s.name === name);
  const choose = (next) => {
    setName(next);
    const state = STATES.find((s) => s.name === next) || null;
    dev.use(state);
    onSelect(state);
  };
  const index = STATES.findIndex((s) => s.name === name);
  return (
    <div class="lz-dev" style={{ font: '12px ui-monospace, monospace', padding: '6px 0 10px', display: 'flex', gap: '6px', flexWrap: 'wrap', alignItems: 'center' }}>
      <label>
        Dev fixture{' '}
        <select value={name} onChange={(e) => choose(e.currentTarget.value)}>
          <option value="">Live (probe tools, canned run)</option>
          {groups.map((g) => (
            <optgroup label={g} key={g}>
              {STATES.filter((s) => s.group === g).map((s) => (
                <option value={s.name} key={s.name}>
                  {s.name}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </label>
      <button type="button" onClick={() => choose(STATES[(index + 1) % STATES.length].name)}>
        Next
      </button>
      {current ? <span>{current.press ? 'press the button · ' : ''}{current.provenance}</span> : null}
    </div>
  );
}
