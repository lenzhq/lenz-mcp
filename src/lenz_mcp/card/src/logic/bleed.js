// Whether the host gives the card's frame the whole screen width.
//
// ChatGPT's iOS app renders the card's iframe FULL-BLEED, edge to edge with no
// inset (seen on a phone, 2026-09-18): the card's side rules then sit on or
// past the screen edge and its rounded corners float. A drawn frame has nowhere
// to live there, so on a full-bleed frame the card keeps only its top and bottom
// hairlines. Decided from geometry rather than from which host it is, because
// ChatGPT's hostInfo is unmeasured and the same shape on any other host deserves
// the same answer; and never by adding an outer margin, which would double up on
// the hosts that already inset the frame.
//
// The frame is full-bleed when it is as wide as the screen in EITHER dimension:
// iOS reports `screen.width` as the portrait width in both orientations, and
// `screen.orientation` cannot settle which dimension is current (a real iPhone
// in landscape and Chromium's phone emulation report the same pair). Only on a
// touch screen: a desktop chat column can equal a laptop screen's HEIGHT (768).
//
// Two known misses, both accepted. An INSET frame exactly
// as wide as the phone's portrait width, shown in landscape, reads as full-bleed
// and loses its side rules: a coincidence of numbers, and the cost is a card
// with top and bottom rules only. An edge-to-edge iPad split-view pane matches
// neither screen dimension and keeps its frame. The signal that would catch it,
// `outerWidth`, would also match a host that gives each card its own webview,
// sized to the card and inset around it, which is worse.
export function isFullBleed(win) {
  if (!win) return false;
  const width = Number(win.innerWidth) || 0;
  const screen = win.screen || {};
  const dims = [Number(screen.width) || 0, Number(screen.height) || 0].filter((d) => d > 0);
  if (!width || !dims.length) return false;
  if (!coarsePointer(win)) return false;
  // One pixel of slack for a fractional device-pixel-ratio rounding.
  return dims.some((d) => Math.abs(width - d) <= 1);
}

function coarsePointer(win) {
  try {
    return Boolean(win.matchMedia && win.matchMedia('(pointer: coarse)').matches);
  } catch (_e) {
    return false;
  }
}
