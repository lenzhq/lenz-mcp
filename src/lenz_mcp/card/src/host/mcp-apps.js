// The MCP Apps host adapter: the ONLY file that knows the postMessage protocol.
// Method names and params follow ext-apps src/spec.types.ts
// (2026-01-26), not the published spec page, which was wrong about `appInfo`.
//
// Above this file the card speaks the HostAdapter interface (./interface.js):
// connect, capabilities, callTool, updateModelContext, openLink, sendMessage,
// reportSize, onToolResult / onToolInput / onToolCancelled, onContextChange,
// onTeardown, dispose. Protocol errors are normalised to HostError here.

import { isWebUrl } from '../logic/format.js';

import { mcpAppsTheme } from './theme.js';

export class HostError extends Error {
  constructor(message, code = 'host') {
    super(message);
    this.name = 'HostError';
    this.code = code;
  }
}

const PROTOCOL_VERSION = '2026-01-26';
const DEFAULT_TIMEOUT_MS = 60000;

function payloadOf(result) {
  if (!result || typeof result !== 'object') return null;
  if (result.structuredContent && typeof result.structuredContent === 'object') return result.structuredContent;
  const first = Array.isArray(result.content) ? result.content.find((c) => c && c.type === 'text') : null;
  if (first && typeof first.text === 'string') {
    try {
      const parsed = JSON.parse(first.text);
      return parsed && typeof parsed === 'object' ? parsed : null;
    } catch (_e) {
      return null;
    }
  }
  return null;
}

export function createMcpAppsHost({ win = globalThis.window, timeoutMs = DEFAULT_TIMEOUT_MS, appVersion = '1' } = {}) {
  const parent = win.parent;
  const pending = new Map();
  const subscribers = { toolResult: [], toolInput: [], toolCancelled: [], context: [], teardown: [] };
  const last = { toolResult: undefined, toolInput: undefined };
  let nextId = 1;
  let capabilities = { message: false, updateModelContext: false, openLinks: false, serverTools: false };
  let lastSize = '';

  const post = (msg) => parent.postMessage(msg, '*');

  function request(method, params, ms = timeoutMs) {
    const id = nextId;
    nextId += 1;
    return new Promise((resolve, reject) => {
      const timer = win.setTimeout(() => {
        pending.delete(id);
        reject(new HostError(`no reply to ${method}`, 'timeout'));
      }, ms);
      pending.set(id, { resolve, reject, timer });
      post({ jsonrpc: '2.0', id, method, params });
    });
  }

  const notify = (method, params) => post({ jsonrpc: '2.0', method, params });
  const emit = (name, value) => subscribers[name].forEach((fn) => fn(value));

  function onMessage(event) {
    if (event.source !== parent) return;
    const msg = event.data;
    if (!msg || msg.jsonrpc !== '2.0') return;

    if (msg.id !== undefined && !msg.method && pending.has(msg.id)) {
      const { resolve, reject, timer } = pending.get(msg.id);
      pending.delete(msg.id);
      win.clearTimeout(timer);
      if (msg.error) reject(new HostError(String((msg.error && msg.error.message) || 'host error'), 'rpc'));
      else resolve(msg.result);
      return;
    }

    switch (msg.method) {
      case 'ui/notifications/tool-result': {
        last.toolResult = payloadOf(msg.params);
        emit('toolResult', last.toolResult);
        break;
      }
      case 'ui/notifications/tool-input':
        last.toolInput = (msg.params && msg.params.arguments) || {};
        emit('toolInput', last.toolInput);
        break;
      case 'ui/notifications/tool-cancelled':
        emit('toolCancelled', msg.params || {});
        break;
      case 'ui/notifications/host-context-changed':
        emit('context', msg.params || {});
        break;
      case 'ui/resource-teardown':
        try {
          emit('teardown', msg.params || {});
        } finally {
          if (msg.id !== undefined) post({ jsonrpc: '2.0', id: msg.id, result: {} });
        }
        break;
      default:
        break;
    }
  }

  win.addEventListener('message', onMessage);

  const subscribe = (name) => (fn) => {
    subscribers[name].push(fn);
    if (name === 'toolResult' && last.toolResult !== undefined) fn(last.toolResult);
    if (name === 'toolInput' && last.toolInput !== undefined) fn(last.toolInput);
    return () => {
      subscribers[name] = subscribers[name].filter((f) => f !== fn);
    };
  };

  return {
    get capabilities() {
      return capabilities;
    },
    // The theme seam (host/theme.js): which CSS value each of the card's slots
    // takes from THIS host's published variables.
    themeVariables(context) {
      return mcpAppsTheme(context && context.styles && context.styles.variables);
    },

    async connect() {
      const result = await request('ui/initialize', {
        appInfo: { name: 'lenz-card', version: appVersion },
        appCapabilities: { availableDisplayModes: ['inline'] },
        protocolVersion: PROTOCOL_VERSION,
      });
      const caps = (result && result.hostCapabilities) || {};
      capabilities = {
        message: Boolean(caps.message),
        updateModelContext: Boolean(caps.updateModelContext),
        openLinks: Boolean(caps.openLinks),
        serverTools: Boolean(caps.serverTools),
      };
      notify('ui/notifications/initialized', {});
      return { hostInfo: (result && result.hostInfo) || {}, context: (result && result.hostContext) || {} };
    },

    async callTool(name, args) {
      const result = await request('tools/call', { name, arguments: args });
      const payload = payloadOf(result);
      if (!payload) throw new HostError(`no usable result from ${name}`, 'empty');
      return payload;
    },

    async updateModelContext({ text, structured }) {
      if (!capabilities.updateModelContext) throw new HostError('updateModelContext unsupported', 'unsupported');
      const params = { content: [{ type: 'text', text }] };
      if (structured) params.structuredContent = structured;
      const result = await request('ui/update-model-context', params, 15000);
      if (result && result.isError) throw new HostError('context update refused', 'refused');
    },

    async openLink(url) {
      if (!capabilities.openLinks) throw new HostError('openLinks unsupported', 'unsupported');
      if (!isWebUrl(url)) throw new HostError('refused a non-web link', 'url');
      const result = await request('ui/open-link', { url: url.trim() }, 120000);
      if (result && result.isError) throw new HostError('link refused', 'refused');
    },

    async sendMessage(text) {
      if (!capabilities.message) throw new HostError('message unsupported', 'unsupported');
      const result = await request('ui/message', { role: 'user', content: [{ type: 'text', text }] }, 120000);
      if (result && result.isError) throw new HostError('message refused', 'refused');
    },

    reportSize(width, height) {
      const key = `${width}x${height}`;
      if (key === lastSize) return;
      lastSize = key;
      notify('ui/notifications/size-changed', { width, height });
    },

    onToolResult: subscribe('toolResult'),
    onToolInput: subscribe('toolInput'),
    onToolCancelled: subscribe('toolCancelled'),
    onContextChange: subscribe('context'),
    onTeardown: subscribe('teardown'),

    dispose() {
      win.removeEventListener('message', onMessage);
      for (const { timer, reject } of pending.values()) {
        win.clearTimeout(timer);
        reject(new HostError('disposed', 'disposed'));
      }
      pending.clear();
    },
  };
}
