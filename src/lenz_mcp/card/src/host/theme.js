// The theme seam. Rendering asks for four slots and knows nothing about which
// variables a host publishes; each adapter maps them (MCP Apps / Claude below,
// a ChatGPT adapter later maps its own). Every slot is optional: a host that
// publishes nothing leaves the card on its Ledger values (src/styles.js).
export const THEME_SLOTS = ['--lz-bg', '--lz-ink', '--lz-meta', '--lz-hair'];

// Claude's MCP Apps variables (measured, probe run 1-2, 2026-09-17). They are
// `light-dark()` values, which is why the root's color-scheme follows the theme.
const MCP_APPS_SLOTS = {
  '--lz-bg': '--color-background-primary',
  '--lz-ink': '--color-text-primary',
  '--lz-meta': '--color-text-secondary',
  '--lz-hair': '--color-border-tertiary',
};

// What the card should set for each slot, given the variables this host sent.
export function mcpAppsTheme(variables) {
  const out = {};
  if (!variables || typeof variables !== 'object') return out;
  for (const [slot, name] of Object.entries(MCP_APPS_SLOTS)) {
    if (typeof variables[name] === 'string' && variables[name]) out[slot] = `var(${name})`;
  }
  return out;
}
