// The host interface the card is written against. Rendering,
// state and polling use ONLY these; each host gets one adapter file that
// implements them (host/mcp-apps.js today). A future adapter proves itself with
// the contract test in interface.test.js, never by assumption.
//
// connect()                          → Promise<{hostInfo, context}>; fills `capabilities`
// capabilities                       → {message, updateModelContext, openLinks, serverTools} booleans
// themeVariables(context)            → {'--lz-bg': 'var(--host-var)', …} for the slots this
//                                      host publishes (host/theme.js); rendering never names
//                                      a host's variables
// callTool(name, args)               → Promise<payload object>; throws HostError
// updateModelContext({text, structured}) → Promise; throws HostError when unsupported or refused
// openLink(url)                      → Promise; http(s) only
// sendMessage(text)                  → Promise; prefills the chat, the user sends it
// reportSize(width, height)          → void, deduplicated
// onToolResult(fn) / onToolInput(fn) / onToolCancelled(fn) → unsubscribe; a result that
//                                      arrived before the subscriber is replayed to it
// onContextChange(fn)                → unsubscribe; theme, styles, display mode
// onTeardown(fn)                     → unsubscribe; the adapter answers the host after fn
// dispose()                          → stop listening, reject pending requests

export const HOST_OPERATIONS = [
  'connect',
  'themeVariables',
  'callTool',
  'updateModelContext',
  'openLink',
  'sendMessage',
  'reportSize',
  'onToolResult',
  'onToolInput',
  'onToolCancelled',
  'onContextChange',
  'onTeardown',
  'dispose',
];

export const HOST_CAPABILITIES = ['message', 'updateModelContext', 'openLinks', 'serverTools'];
