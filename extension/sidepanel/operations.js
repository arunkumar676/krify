// Runs browser operations. During a task Krify works in "its" tab, not whatever tab is active,
// and only sees/uses its own tabs: the one the task started in plus tabs it opened.

import { detachAll, needsRealInput, realClick, realDrag, realKey, realMove, realType, resetRealInput } from "./realinput.js";
import { isSheetUrl, sheetCsv } from "./sheets.js";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- Krify's tabs ----------

let agentTabId = null; // tab Krify is working in during a task
const krifyTabs = []; // tab ids in the order Krify got them; [0] is the starting tab
const openedByKrify = new Set(); // only these may be closed by Krify

/** Called when a task starts: Krify works in the tab you're looking at. */
export async function beginTask() {
  resetRealInput();
  const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!active) return;
  if (!krifyTabs.includes(active.id)) krifyTabs.push(active.id);
  agentTabId = active.id;
}

/** New chat: forget Krify's tabs (the tabs themselves stay open). */
export function resetTabs() {
  detachAll();
  krifyTabs.length = 0;
  openedByKrify.clear();
  agentTabId = null;
  frameMap = [];
  lastFrameId = null;
}

chrome.tabs.onRemoved.addListener((tabId) => {
  const i = krifyTabs.indexOf(tabId);
  if (i !== -1) krifyTabs.splice(i, 1);
  openedByKrify.delete(tabId);
  if (agentTabId === tabId) agentTabId = krifyTabs[krifyTabs.length - 1] ?? null;
});

// A link that opens in a new tab (target="_blank"): follow it there.
chrome.tabs.onCreated.addListener((tab) => {
  if (working && agentTabId !== null && tab.openerTabId === agentTabId) {
    krifyTabs.push(tab.id);
    openedByKrify.add(tab.id);
    agentTabId = tab.id;
  }
});

async function currentTab() {
  // During a task: Krify's tab. Otherwise (manual test buttons): the tab you're looking at.
  if (working && agentTabId !== null) {
    try {
      return await chrome.tabs.get(agentTabId);
    } catch {
      agentTabId = null; // closed
    }
  }
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) throw new Error("No active tab.");
  return tab;
}

/** Krify's tabs as the AI sees them: "1: Amazon (current) | 2: Flipkart". */
async function tabList() {
  const parts = [];
  for (const [i, id] of krifyTabs.entries()) {
    try {
      const t = await chrome.tabs.get(id);
      parts.push(`${i + 1}: ${(t.title || t.url || "").slice(0, 40)}${id === agentTabId ? " (current)" : ""}`);
    } catch {
      // tab closed meanwhile
    }
  }
  return parts.join(" | ");
}

function tabIdFor(number) {
  const id = krifyTabs[Number(number) - 1];
  if (id === undefined) throw new Error(`There is no tab ${number}. Use a number from the [Tabs] list.`);
  return id;
}

async function focusTab(tabId) {
  await setWorkingIn(agentTabId, false); // move the glowing border along
  agentTabId = tabId;
  const tab = await chrome.tabs.update(tabId, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true });
}

// ---------- page access ----------

// Page reading/acting only works on normal websites.
async function webTab() {
  const tab = await currentTab();
  if (!/^(https?|file):/.test(tab.url || "")) {
    throw new Error("Krify can't work on this page (browser pages like chrome:// are protected). Open a normal website.");
  }
  return tab;
}

// Wait until the tab finishes loading (max 15 seconds).
async function waitForLoad(tabId) {
  await sleep(300);
  for (let i = 0; i < 50; i++) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === "complete") return;
    await sleep(300);
  }
}

// The "Show animations" switch in the side panel (on unless turned off).
async function showCursor() {
  const { showCursor = true } = await chrome.storage.local.get("showCursor");
  return showCursor;
}

// "Unrestricted" mode: skip content.js's token-saving caps (element/text limits) and send the
// full page data instead – for a high-context model where you'd rather pay more tokens than miss
// something the caps would otherwise cut.
async function showUnrestricted() {
  const { unrestricted = false } = await chrome.storage.local.get("unrestricted");
  return unrestricted;
}

// True while the agent runs a task (set by the side panel). Drives the glowing page border.
let working = false;

export async function setWorking(on) {
  working = on;
  if (on) {
    await setWorkingIn(agentTabId, await showCursor());
  } else {
    await detachAll(); // task over: real keyboard off, Chrome's yellow bar goes away
    for (const id of krifyTabs) await setWorkingIn(id, false);
    if (!krifyTabs.length) await setWorkingIn(null, false);
  }
}

async function setWorkingIn(tabId, on) {
  try {
    await callInPage("setWorking", [on], tabId);
  } catch {
    // Protected or closed page: nothing to show.
  }
}

// Inject content.js (if needed), then call one of its functions inside the page. frameId: target
// one specific frame (an iframe found by acrossFrames/resolveElement) instead of the main frame.
async function callInPage(fnName, args = [], tabId = null, frameId = null) {
  const tab = tabId !== null ? await chrome.tabs.get(tabId) : await webTab();
  const target = frameId != null ? { tabId: tab.id, frameIds: [frameId] } : { tabId: tab.id };
  await chrome.scripting.executeScript({ target, files: ["content.js"] });
  const animate = await showCursor();
  const unrestricted = await showUnrestricted();
  const [result] = await chrome.scripting.executeScript({
    target,
    func: async (name, a, animate, glow, unrestricted) => {
      try {
        window.__krify.animate = animate;
        window.__krify.unrestricted = unrestricted;
        if (name !== "setWorking") window.__krify.setWorking(glow); // re-show after page loads
        return { ok: true, value: await window.__krify[name](...a) };
      } catch (err) {
        return { ok: false, error: err.message };
      }
    },
    args: [fnName, args, animate, working && animate, unrestricted],
  });
  const res = result?.result;
  if (!res) throw new Error("No answer from the page. Try again.");
  if (!res.ok) throw new Error(res.error);
  return res.value;
}

// ---------- iframes ----------
// content.js itself never needs to know about frames: injected with allFrames, each frame gets
// its own independent window.__krify, numbering its own elements 1..N exactly as it always has.
// Everything frame-aware lives here: run it in every frame, then fold each frame's own numbering
// onto one running [N] the model sees, and remember where each id really lives so actions on it
// can be routed back to the right frame.

let frameMap = []; // [{globalId, frameId, localId}], rebuilt on every full element listing
let lastFrameId = null; // frame of the last action the model aimed at a specific element

// Same inject-then-call as callInPage, but across every frame in the tab. A frame that fails to
// inject (cross-origin without access, sandboxed, about:blank) is just missing from the result,
// not an error – same spirit as "Protected or closed page: nothing to show" elsewhere.
async function callInAllFrames(fnName, args = [], tabId = null) {
  const tab = tabId !== null ? await chrome.tabs.get(tabId) : await webTab();
  const target = { tabId: tab.id, allFrames: true };
  await chrome.scripting.executeScript({ target, files: ["content.js"] });
  const animate = await showCursor();
  const unrestricted = await showUnrestricted();
  const results = await chrome.scripting.executeScript({
    target,
    func: async (name, a, animate, glow, unrestricted) => {
      try {
        window.__krify.animate = animate;
        window.__krify.unrestricted = unrestricted;
        if (name !== "setWorking") window.__krify.setWorking(glow);
        return { ok: true, value: await window.__krify[name](...a), url: location.href };
      } catch (err) {
        return { ok: false, error: err.message, url: location.href };
      }
    },
    args: [fnName, args, animate, working && animate, unrestricted],
  });
  return results.map((r) => ({ frameId: r.frameId, ...r.result }));
}

// Rewrites each line's leading "[id]" (plain, or indented like inspect()'s tree) into a new,
// globally-unique id continuing the running counter, and records where it really lives.
function relabel(text, frameId, map) {
  return String(text ?? "").replace(/^(\s*)\[(\d+)\] /gm, (_, indent, localId) => {
    const globalId = map.length + 1;
    map.push({ globalId, frameId, localId: Number(localId) });
    return `${indent}[${globalId}] `;
  });
}

// get_elements/find_elements/read_text/find_text/inspect(no ref): run across every frame, relabel
// onto one numbering, and show any iframe's content as its own labelled block.
async function acrossFrames(fnName, args) {
  const frames = await callInAllFrames(fnName, args);
  frameMap = [];
  const main = frames.find((f) => f.frameId === 0);
  const parts = [relabel(main?.ok ? main.value : "(can't read the main page)", 0, frameMap)];
  for (const f of frames) {
    if (f.frameId === 0 || !f.ok) continue;
    const body = relabel(f.value, f.frameId, frameMap);
    if (body.trim()) parts.push(`--- iframe: ${f.url} ---\n${body}`);
  }
  return parts.join("\n\n");
}

// A global element id -> where it really lives. Ordinary main-frame ids (almost all of them)
// resolve to themselves untouched, exactly as before this feature existed.
function resolveElement(id) {
  const hit = frameMap.find((m) => m.globalId === Number(id));
  if (!hit || hit.frameId === 0) return { frameId: null, localId: id };
  return { frameId: hit.frameId, localId: hit.localId };
}

// A frame-routed action's result echoes content.js's own (local) id back – show the model its id instead.
function renumber(text, localId, globalId) {
  return localId === globalId ? text : text.replace(`[${localId}]`, `[${globalId}]`);
}

const IFRAME_UNSUPPORTED = (element, what) =>
  new Error(`[${element}] is inside an iframe – ${what} isn't supported there yet. Try a single left-click, ` +
    "typing, or screenshot + click_at.");

// Make a screenshot smaller (fewer tokens): at most maxWidth pixels wide, compressed JPEG.
// Returns { dataUrl, width }.
async function shrinkImage(dataUrl, maxWidth) {
  const bitmap = await createImageBitmap(await (await fetch(dataUrl)).blob());
  const scale = Math.min(1, maxWidth / bitmap.width);
  const canvas = new OffscreenCanvas(Math.round(bitmap.width * scale), Math.round(bitmap.height * scale));
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const blob = await canvas.convertToBlob({ type: "image/jpeg", quality: 0.6 });
  const url = await new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.readAsDataURL(blob);
  });
  return { dataUrl: url, width: canvas.width };
}

// ---------- clicks by position (click_at, drag) ----------

// The last screenshot: the AI's x, y are pixels on that (shrunk) picture.
let lastShot = null; // { tabId, url, scrollX, scrollY, scale }

/** A spot on the last screenshot → a spot on the page. Refuses if the page moved since. */
async function shotToPage(tab, x, y) {
  if (!lastShot || lastShot.tabId !== tab.id) throw new Error("Take a screenshot first – x, y are positions on it.");
  const vp = await callInPage("viewport");
  if (tab.url !== lastShot.url || vp.scrollX !== lastShot.scrollX || vp.scrollY !== lastShot.scrollY) {
    throw new Error("The page changed since the last screenshot – take a new screenshot first.");
  }
  if (!Number.isFinite(Number(x)) || !Number.isFinite(Number(y))) throw new Error("x and y must be numbers.");
  return { x: Math.round(Number(x) * lastShot.scale), y: Math.round(Number(y) * lastShot.scale) };
}

const clickWord = (button, count) =>
  `${button === "right" ? "Right-clicked" : ["Clicked", "Double-clicked", "Triple-clicked"][count - 1]}`;
// Sheets draws its right-click menu where page text can't be read (checked: not in the page,
// its frames or shadow parts), so the AI is pointed to what works.
const SHEETS_MENU_NOTE =
  " – a menu opened, but it can't be read as text: take a screenshot to see it, or use the top menu bar" +
  " (Insert, Format, Data…) whose items are listed. Escape closes it.";
const RIGHT_CLICK_NOTE =
  " – on normal websites this opens Chrome's own menu, which Krify can't use: press Escape.";

// ---------- Google Sheets: saving ----------

// When Krify last typed or pressed keys for real. The downloaded copy of a sheet only has an
// edit once Google has saved it, so reading right after an edit waits for the "saved" sign.
let lastEdit = 0;
const edited = () => (lastEdit = Date.now());

async function waitForSave() {
  if (Date.now() - lastEdit > 15000) return; // no recent edit
  let sawSaving = false;
  const start = Date.now();
  while (Date.now() - start < 5000) {
    const state = await callInPage("saveState").catch(() => "unknown");
    const since = Date.now() - lastEdit;
    if (state === "saving") sawSaving = true;
    // "saved" right after typing may still be the old status, so trust it only after "saving"
    // was seen, or once saving had time to start.
    if (state === "saved" && (sawSaving ? since > 300 : since > 1500)) return;
    if (state === "unknown" && since > 2000) return; // can't see the sign: plain wait instead
    await sleep(200);
  }
}

// Run an in-page action, then give the page time to react or load.
async function act(fnName, args, frameId = null) {
  const value = await callInPage(fnName, args, null, frameId);
  await waitForLoad((await currentTab()).id);
  return value;
}

export const operations = {
  // See the page
  async get_page_info() {
    const info = await callInPage("getPageInfo");
    if (krifyTabs.length > 1) info.tabs = await tabList();
    return info;
  },
  get_elements: () => acrossFrames("getElements", []),
  find_elements: ({ query } = {}) => acrossFrames("findElements", [query]),
  find_text: ({ query } = {}) => acrossFrames("findText", [query]),
  async screenshot() {
    // Element numbers drawn on → picture of the visible area → labels removed → shrink.
    const tab = await currentTab();
    await chrome.tabs.update(tab.id, { active: true }); // only the visible tab can be captured
    await callInPage("showLabels", [true]);
    await sleep(150);
    let shot;
    try {
      shot = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 70 });
    } finally {
      await callInPage("showLabels", [false]);
    }
    const vp = await callInPage("viewport");
    const { dataUrl, width } = await shrinkImage(shot, 1024);
    lastShot = { tabId: tab.id, url: tab.url, scrollX: vp.scrollX, scrollY: vp.scrollY, scale: vp.width / width };
    return dataUrl;
  },
  read_text: ({ scope = "screen" } = {}) => acrossFrames("readText", [scope]),
  inspect({ ref, filter = "interactive", depth } = {}) {
    if (ref == null) return acrossFrames("getAccessibilityTree", [{ filter, depth }]);
    const { frameId, localId } = resolveElement(ref);
    return callInPage("getAccessibilityTree", [{ ref: localId, filter, depth }], null, frameId);
  },
  // Google Sheets: the open sheet tab as CSV (the server answers questions about it).
  async sheet_csv() {
    const tab = await currentTab();
    if (!isSheetUrl(tab.url)) throw new Error("This isn't a Google Sheets page.");
    await waitForSave();
    const csv = await sheetCsv(tab.url);
    const name = await callInPage("sheetTabName").catch(() => "");
    return { csv, tab: name, title: (tab.title || "").replace(/ - Google Sheets$/, "") };
  },

  // Navigate
  async open_url({ url }) {
    if (!/^https?:\/\//i.test(url)) url = `https://${url}`;
    const tab = await currentTab();
    await chrome.tabs.update(tab.id, { url });
    await waitForLoad(tab.id);
    return `Opened ${url}`;
  },
  async go_back() {
    const tab = await currentTab();
    await chrome.tabs.goBack(tab.id);
    await waitForLoad(tab.id);
    return "Went back";
  },
  async reload() {
    const tab = await currentTab();
    await chrome.tabs.reload(tab.id);
    await waitForLoad(tab.id);
    return "Reloaded the page";
  },

  // Tabs
  async new_tab({ url }) {
    if (!/^https?:\/\//i.test(url)) url = `https://${url}`;
    const current = await currentTab();
    const tab = await chrome.tabs.create({ url, active: true, windowId: current.windowId, index: current.index + 1 });
    krifyTabs.push(tab.id);
    openedByKrify.add(tab.id);
    await setWorkingIn(agentTabId, false);
    agentTabId = tab.id;
    await waitForLoad(tab.id);
    return `Opened ${url} in new tab ${krifyTabs.length}`;
  },
  async switch_tab({ tab }) {
    await focusTab(tabIdFor(tab));
    return `Switched to tab ${tab}`;
  },
  async close_tab({ tab }) {
    const id = tabIdFor(tab);
    if (!openedByKrify.has(id)) throw new Error(`Tab ${tab} is the user's tab – Krify only closes tabs it opened.`);
    if (id === agentTabId) await focusTab(krifyTabs[0]);
    await chrome.tabs.remove(id); // onRemoved updates the list
    return `Closed tab ${tab}`;
  },

  // Interact
  async click({ element, cell, button = "left", count = 1 }) {
    const n = Math.min(Math.max(Math.round(Number(count)) || 1, 1), 3);
    const btn = button === "right" ? "right" : "left";
    if (cell) {
      // Google Sheets: a cell by address – go there, then click the middle of the selected cell.
      const tab = await currentTab();
      await operations.go_to_cell({ cell });
      const spot = await callInPage("activeCellSpot");
      await callInPage("showPoint", [spot.x, spot.y]);
      await realClick(tab.id, spot.x, spot.y, { button: btn, clickCount: n });
      await sleep(400);
      return `${clickWord(btn, n)} cell ${String(cell).toUpperCase()}${btn === "right" ? SHEETS_MENU_NOTE : ""}`;
    }
    const { frameId, localId } = resolveElement(element);
    if (btn === "left" && n === 1) {
      lastFrameId = frameId;
      const result = await act("click", [localId], frameId); // a normal click: no debugger needed
      return renumber(result, localId, element);
    }
    if (frameId != null) throw IFRAME_UNSUPPORTED(element, "right-click/double-click/triple-click");
    const tab = await currentTab();
    if (isSheetUrl(tab.url)) {
      // On Sheets the numbered elements are toolbar parts or Sheets' hidden cell editor, not cells:
      // right-clicking that editor opens Chrome's own text menu.
      throw new Error('On Google Sheets, right-click or double-click cells with click(cell="B22"), not element numbers.');
    }
    const spot = await callInPage("pointAt", [element]);
    await realClick(tab.id, spot.x, spot.y, { button: btn, clickCount: n });
    await sleep(300);
    await waitForLoad(tab.id);
    const note = btn === "right" && !needsRealInput(tab.url) ? RIGHT_CLICK_NOTE : "";
    return `${clickWord(btn, n)} [${element}]${note}`;
  },
  // What's under a spot of the last screenshot (for the safety check before click_at).
  async element_at({ x, y }) {
    const tab = await currentTab();
    const spot = await shotToPage(tab, x, y);
    return callInPage("elementAt", [spot.x, spot.y]);
  },
  // What [id] currently points at (for the staleness check before acting on an element number).
  describe_element: ({ element } = {}) => {
    const { frameId, localId } = resolveElement(element);
    return callInPage("describeId", [localId], null, frameId);
  },
  async click_at({ x, y, button = "left", count = 1 }) {
    const n = Math.min(Math.max(Math.round(Number(count)) || 1, 1), 3);
    const btn = button === "right" ? "right" : "left";
    const tab = await currentTab();
    const spot = await shotToPage(tab, x, y);
    const what = await callInPage("elementAt", [spot.x, spot.y]);
    await callInPage("showPoint", [spot.x, spot.y]);
    await realClick(tab.id, spot.x, spot.y, { button: btn, clickCount: n });
    await sleep(300);
    await waitForLoad(tab.id);
    const note = btn !== "right" ? "" : isSheetUrl(tab.url) ? SHEETS_MENU_NOTE : needsRealInput(tab.url) ? "" : RIGHT_CLICK_NOTE;
    return `${clickWord(btn, n)} at (${x}, ${y})${what ? ` – ${what}` : ""}${note}`;
  },
  async drag({ from_element, to_element, from_x, from_y, to_x, to_y }) {
    const tab = await currentTab();
    for (const id of [from_element, to_element]) {
      if (id != null && resolveElement(id).frameId != null) throw IFRAME_UNSUPPORTED(id, "drag");
    }
    const from = from_element != null ? await callInPage("pointAt", [from_element]) : await shotToPage(tab, from_x, from_y);
    const to = to_element != null ? await callInPage("spotOf", [to_element]) : await shotToPage(tab, to_x, to_y);
    const kind = await realDrag(tab.id, from, to);
    await callInPage("showPoint", [to.x, to.y, false]).catch(() => {});
    if (needsRealInput(tab.url)) edited();
    await sleep(300);
    const a = from_element != null ? `[${from_element}]` : `(${from_x}, ${from_y})`;
    const b = to_element != null ? `[${to_element}]` : `(${to_x}, ${to_y})`;
    return `Dragged ${a} to ${b}${kind === "html5" ? " (drag and drop)" : ""}`;
  },
  async hover({ element, real = false }) {
    const { frameId, localId } = resolveElement(element);
    if (!real) {
      lastFrameId = frameId;
      const result = await act("hover", [localId], frameId);
      return renumber(result, localId, element);
    }
    if (frameId != null) throw IFRAME_UNSUPPORTED(element, "a real mouse hover");
    // A real mouse move, for menus that only open for a real mouse.
    const tab = await currentTab();
    const spot = await callInPage("pointAt", [element]);
    await realMove(tab.id, spot.x, spot.y);
    await sleep(600);
    return `Hovered over [${element}] with the real mouse`;
  },
  async type({ element, text, submit = false }) {
    const tab = await currentTab();
    const { frameId, localId } = resolveElement(element);
    if (!needsRealInput(tab.url)) {
      lastFrameId = frameId;
      const result = await act("type", [localId, text, Boolean(submit)], frameId);
      return renumber(result, localId, element);
    }
    if (frameId != null) throw IFRAME_UNSUPPORTED(element, "real keyboard typing (needed for Google Sheets/Docs)");
    // Google Sheets/Docs ignore JavaScript typing: click the box for real, check the cursor is
    // really in it (never Ctrl+A: in Sheets that selects every cell), then type for real.
    const spot = await callInPage("pointAt", [element]);
    if (spot.nameBox) return operations.go_to_cell({ cell: text });
    await realClick(tab.id, spot.x, spot.y);
    await sleep(200);
    if (!(await callInPage("focusCheck", [element]))) {
      throw new Error(`Couldn't put the cursor in [${element}] – nothing was typed.`);
    }
    await realType(tab.id, String(text ?? ""));
    if (submit) await realKey(tab.id, "Enter");
    edited();
    await sleep(300);
    return `Typed "${text}" into [${element}]${submit ? " and pressed Enter" : ""}`;
  },

  // Google Sheets: the selected cell and its formula bar text – live, no waiting for a save.
  async sheet_active_cell() {
    const tab = await currentTab();
    if (!isSheetUrl(tab.url)) throw new Error("This isn't a Google Sheets page.");
    await sleep(150); // let the formula bar catch up with the last key
    return callInPage("activeCell");
  },

  // Google Sheets: jump to a cell through the Name box, and check it really got there before
  // anything is typed (typing into the wrong cell would overwrite data).
  async go_to_cell({ cell }) {
    const tab = await currentTab();
    if (!isSheetUrl(tab.url)) throw new Error("go_to_cell only works on a Google Sheets page.");
    const ref = String(cell || "").trim().toUpperCase().replace(/\$/g, "");
    if (!/^[A-Z]{1,3}[0-9]{1,7}(:[A-Z]{1,3}[0-9]{1,7})?$/.test(ref)) {
      throw new Error(`"${cell}" isn't a cell like A130 or a range like A1:C5.`);
    }
    // Click the Name box. If something else kept the focus (an open menu, a cell being edited),
    // press Escape – it only closes/cancels, never changes saved data – and try once more.
    for (let attempt = 1; ; attempt++) {
      const box = await callInPage("nameBox");
      await realClick(tab.id, box.x, box.y);
      await sleep(250);
      if (await callInPage("selectNameBox")) break;
      if (attempt === 2) {
        throw new Error("Couldn't put the cursor in the Name box – nothing was typed. Stop and tell the user.");
      }
      await realKey(tab.id, "Escape");
      await sleep(250);
    }
    await realType(tab.id, ref);
    await realKey(tab.id, "Enter");
    await sleep(400);
    const now = String(await callInPage("nameBoxValue")).toUpperCase();
    if (now !== ref) {
      throw new Error(`The Name box shows "${now}", not ${ref} – not at that cell, nothing typed. Stop and tell the user.`);
    }
    return `At cell ${ref} – type_here now types into it`;
  },
  async press_key({ key, repeat = 1 }) {
    const tab = await currentTab();
    const times = Math.min(Math.max(Math.round(Number(repeat)) || 1, 1), 20);
    const simple = ["Enter", "Escape", "Tab", "ArrowDown", "ArrowUp"].includes(key);
    // Targets the frame the last type/click aimed at – its own activeElement, not the <iframe> tag.
    if (simple && times === 1 && !needsRealInput(tab.url)) return act("pressKey", [key], lastFrameId);
    // Shortcuts, other keys, and Google Sheets/Docs: real key presses.
    await callInPage("showKeyBadge", [key]).catch(() => {});
    await realKey(tab.id, key, times);
    edited();
    await sleep(300);
    await waitForLoad(tab.id);
    return `Pressed ${key}${times > 1 ? ` ${times} times` : ""}`;
  },
  async type_here({ text }) {
    const tab = await currentTab();
    // AI models often write Enter/Tab as the two characters \n and \t.
    text = String(text ?? "").replace(/\\n/g, "\n").replace(/\\t/g, "\t");
    await realType(tab.id, text);
    // Sheets: a value not ended by Tab/Enter stays half-typed, and Escape would throw it away.
    const save = isSheetUrl(tab.url) && text && !/[\t\n]$/.test(text);
    if (save) await realKey(tab.id, "Enter");
    edited();
    await sleep(300);
    return `Typed "${text}" where the cursor is${save ? " (Enter pressed to save it – the cursor moved down one row)" : ""}`;
  },
  scroll: ({ direction }) => callInPage("scroll", [direction]),
  async select_option({ element, option }) {
    const { frameId, localId } = resolveElement(element);
    const result = await act("selectOption", [localId, option], frameId);
    return renumber(result, localId, element);
  },

  // Control
  async wait({ seconds = 2 }) {
    const s = Math.min(Math.max(Number(seconds) || 2, 1), 10);
    await sleep(s * 1000);
    return `Waited ${s} seconds`;
  },
};

export async function runOperation(name, args = {}) {
  const op = operations[name];
  if (!op) throw new Error(`Unknown operation: ${name}`);
  return op(args);
}
