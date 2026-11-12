// Real keyboard input through Chrome's debugger. Key presses made in the page by JavaScript are
// marked "not trusted", and apps like Google Sheets ignore them; these come from Chrome itself,
// like a real keyboard. While attached, Chrome shows a yellow "started debugging" bar.
// Attached only to Krify's own tab, only when needed, and detached when the task ends.

const attached = new Set();
let cancelled = false; // the user clicked "Cancel" on the yellow bar

chrome.debugger.onDetach.addListener((source, reason) => {
  attached.delete(source.tabId);
  if (reason === "canceled_by_user") cancelled = true;
});

/** Sites where Krify's normal (JavaScript) typing and keys don't work. */
export const needsRealInput = (url) => /^https:\/\/docs\.google\.com\/(spreadsheets|document|presentation)\//.test(url || "");

/** A new task may ask again, even if the user cancelled last time. */
export function resetRealInput() {
  cancelled = false;
}

export async function detachAll() {
  for (const tabId of [...attached]) {
    attached.delete(tabId);
    await chrome.debugger.detach({ tabId }).catch(() => {});
  }
}

async function attach(tabId) {
  if (cancelled) {
    throw new Error(
      "Real keyboard is off: the user clicked Cancel on Chrome's yellow bar. Don't use type_here or shortcuts; tell the user."
    );
  }
  if (attached.has(tabId)) return;
  await chrome.debugger.attach({ tabId }, "1.3");
  attached.add(tabId);
  // The side panel has the focus, not the page; let the page act as if it were focused.
  await send(tabId, "Emulation.setFocusEmulationEnabled", { enabled: true }).catch(() => {});
}

const send = (tabId, method, params) => chrome.debugger.sendCommand({ tabId }, method, params);

// Keys by name: [key, code, Windows key code, text it types]
const NAMED = {
  enter: ["Enter", "Enter", 13, "\r"], tab: ["Tab", "Tab", 9], escape: ["Escape", "Escape", 27],
  esc: ["Escape", "Escape", 27], backspace: ["Backspace", "Backspace", 8], delete: ["Delete", "Delete", 46],
  space: [" ", "Space", 32, " "], home: ["Home", "Home", 36], end: ["End", "End", 35],
  pageup: ["PageUp", "PageUp", 33], pagedown: ["PageDown", "PageDown", 34], arrowup: ["ArrowUp", "ArrowUp", 38],
  arrowdown: ["ArrowDown", "ArrowDown", 40], arrowleft: ["ArrowLeft", "ArrowLeft", 37],
  arrowright: ["ArrowRight", "ArrowRight", 39], up: ["ArrowUp", "ArrowUp", 38], down: ["ArrowDown", "ArrowDown", 40],
  left: ["ArrowLeft", "ArrowLeft", 37], right: ["ArrowRight", "ArrowRight", 39], f2: ["F2", "F2", 113],
};
const MODIFIERS = { alt: 1, ctrl: 2, control: 2, meta: 4, cmd: 4, shift: 8 };

/** "Ctrl+Shift+Z" → { modifiers, key, code, keyCode, text } */
export function parseKey(combo) {
  const parts = String(combo).split("+").map((p) => p.trim()).filter(Boolean);
  let modifiers = 0;
  let main = "";
  for (const part of parts) {
    const mod = MODIFIERS[part.toLowerCase()];
    if (mod && parts.length > 1) modifiers |= mod;
    else main = part;
  }
  const named = NAMED[main.toLowerCase()];
  if (named) {
    const [key, code, keyCode, text] = named;
    return { modifiers, key, code, keyCode, text };
  }
  if (main.length === 1) {
    const upper = main.toUpperCase();
    const code = /[A-Z]/.test(upper) ? `Key${upper}` : /\d/.test(main) ? `Digit${main}` : "";
    const keyCode = /[A-Z0-9]/.test(upper) ? upper.charCodeAt(0) : 0;
    const key = modifiers & MODIFIERS.shift ? upper : main.toLowerCase();
    return { modifiers, key, code, keyCode, text: key };
  }
  throw new Error(`Unknown key "${combo}". Use e.g. Enter, Tab, Escape, Delete, ArrowDown, Ctrl+Z, Ctrl+End.`);
}

async function pressOnce(tabId, k) {
  // A key with Ctrl/Alt/Meta is a shortcut: it types no text.
  const text = k.modifiers & (MODIFIERS.ctrl | MODIFIERS.alt | MODIFIERS.meta) ? undefined : k.text;
  const base = { modifiers: k.modifiers, key: k.key, code: k.code, windowsVirtualKeyCode: k.keyCode, nativeVirtualKeyCode: k.keyCode };
  await send(tabId, "Input.dispatchKeyEvent", { ...base, type: text ? "keyDown" : "rawKeyDown", text, unmodifiedText: text });
  await send(tabId, "Input.dispatchKeyEvent", { ...base, type: "keyUp" });
}

/** Press a key or shortcut for real, `repeat` times. */
export async function realKey(tabId, combo, repeat = 1) {
  const k = parseKey(combo);
  await attach(tabId);
  for (let i = 0; i < repeat; i++) await pressOnce(tabId, k);
}

/** A real mouse click at a spot on the page (CSS pixels from the top-left of the visible area). */
export async function realClick(tabId, x, y, { button = "left", clickCount = 1 } = {}) {
  await attach(tabId);
  const buttons = { left: 1, right: 2, middle: 4 }[button] || 1;
  await send(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  for (let n = 1; n <= clickCount; n++) {
    await send(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button, buttons, clickCount: n });
    await send(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button, buttons: 0, clickCount: n });
  }
}

/** A real mouse move (for menus that open only for a real mouse). */
export async function realMove(tabId, x, y) {
  await attach(tabId);
  await send(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
}

/** A real drag from one spot to another. Handles both kinds of drag: apps that follow the mouse
 *  (sliders, Sheets), and "HTML5" drag and drop, where Chrome's own drag events are needed.
 *  Returns "html5" or "mouse". */
export async function realDrag(tabId, from, to) {
  await attach(tabId);
  let dragData = null;
  const onEvent = (source, method, params) => {
    if (source.tabId === tabId && method === "Input.dragIntercepted") dragData = params.data;
  };
  chrome.debugger.onEvent.addListener(onEvent);
  await send(tabId, "Input.setInterceptDrags", { enabled: true }).catch(() => {});
  const mouse = (type, p, extra = {}) => send(tabId, "Input.dispatchMouseEvent", { type, x: p.x, y: p.y, ...extra });
  try {
    await mouse("mouseMoved", from);
    await mouse("mousePressed", from, { button: "left", buttons: 1, clickCount: 1 });
    const steps = 12; // move in small steps, like a hand
    for (let i = 1; i <= steps; i++) {
      const p = { x: from.x + ((to.x - from.x) * i) / steps, y: from.y + ((to.y - from.y) * i) / steps };
      await mouse("mouseMoved", p, { button: "left", buttons: 1 });
      await new Promise((r) => setTimeout(r, 25));
      if (dragData) break; // an HTML5 drag started: finish it with drag events below
    }
    if (dragData) {
      for (const type of ["dragEnter", "dragOver", "drop"]) {
        await send(tabId, "Input.dispatchDragEvent", { type, x: to.x, y: to.y, data: dragData });
      }
    }
    await mouse("mouseReleased", to, { button: "left", buttons: 0, clickCount: 1 });
  } finally {
    chrome.debugger.onEvent.removeListener(onEvent);
    await send(tabId, "Input.setInterceptDrags", { enabled: false }).catch(() => {});
  }
  return dragData ? "html5" : "mouse";
}

/** Type text for real into whatever is focused (a Sheets cell, the Name box...).
 *  "\t" presses Tab and "\n" presses Enter, so a whole row can be typed at once. */
export async function realType(tabId, text) {
  await attach(tabId);
  for (const ch of String(text)) {
    if (ch === "\t") await pressOnce(tabId, parseKey("Tab"));
    else if (ch === "\n") await pressOnce(tabId, parseKey("Enter"));
    else if (/^[a-z0-9]$/i.test(ch)) await pressOnce(tabId, parseKey(ch === ch.toUpperCase() && /[A-Z]/.test(ch) ? `Shift+${ch}` : ch));
    else await send(tabId, "Input.insertText", { text: ch }); // symbols, spaces, other languages
  }
}
