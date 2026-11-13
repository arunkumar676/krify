// Runs browser operations. During a task Krify works in "its" tab, not whatever tab is active,
// and only sees/uses its own tabs: the one the task started in plus tabs it opened.

import { detachAll, needsRealInput, realClick, realDrag, realKey, realMove, realType, resetRealInput } from "./realinput.js";
import { isSheetUrl, sheetCsv } from "./sheets.js";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- Krify's tabs ----------

// The tab this panel belongs to (each tab where you click Krify gets its own panel and chat).
export const MY_TAB = Number(new URLSearchParams(location.search).get("tab")) || null;

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
  elementFrame.clear();
}

chrome.tabs.onRemoved.addListener((tabId) => {
  const i = krifyTabs.indexOf(tabId);
  if (i !== -1) krifyTabs.splice(i, 1);
  openedByKrify.delete(tabId);
  if (agentTabId === tabId) agentTabId = krifyTabs[krifyTabs.length - 1] ?? null;
});

// A tab opened from Krify's tab during a task (a new-tab link, the site's own window.open): follow it
// there. With "Open links in the same tab" on, then open its address in Krify's own tab instead and
// close the new one – except real pop-up windows (Google sign-in, payment pages talk back to the page
// that opened them) and tabs that never get a normal web address (filled in by the page's script).
let redirecting = null; // set while a new tab is being brought back – waitForLoad waits for it
let redirectNote = ""; // told to the AI with the action's result
chrome.tabs.onCreated.addListener((tab) => {
  if (!working || agentTabId === null || tab.openerTabId !== agentTabId || krifyTabs.includes(tab.id)) return;
  const opener = agentTabId;
  krifyTabs.push(tab.id);
  openedByKrify.add(tab.id);
  agentTabId = tab.id;
  redirecting = (async () => {
    if (!(await sameTabOn())) return;
    const win = await chrome.windows.get(tab.windowId).catch(() => null);
    if (!win || win.type !== "normal") return; // a pop-up window: leave it
    const url = await webUrlOf(tab.id);
    if (!url) return;
    await chrome.tabs.update(opener, { url, active: true });
    agentTabId = opener;
    await chrome.tabs.remove(tab.id).catch(() => {}); // onRemoved takes it off krifyTabs
    redirectNote = ` – it opened a new tab, so Krify opened that page here instead (${url})`;
  })().catch(() => {}).finally(() => (redirecting = null));
});

// A new tab's web address once it has one (it starts blank); "" if it never gets one in ~3 seconds.
async function webUrlOf(tabId) {
  for (let i = 0; i < 15; i++) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) return "";
    const url = tab.pendingUrl || tab.url || "";
    if (/^https?:/i.test(url)) return url;
    await sleep(200);
  }
  return "";
}

// The side panel's "Open links in the same tab" switch (on unless turned off).
async function sameTabOn() {
  const { sameTab = true } = await chrome.storage.local.get("sameTab");
  return sameTab;
}

/** Krify needs you (Allow/Block, a question, the answer): the panel can't follow Krify to the
 *  tabs it opens (Chrome only opens a panel after a click), so bring this panel's tab to the front. */
export async function showMyTab() {
  if (MY_TAB === null) return;
  try {
    const tab = await chrome.tabs.update(MY_TAB, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
  } catch {
    // tab closed
  }
}

/** Back to the tab Krify works in (after you answered in this panel's tab). */
export async function showAgentTab() {
  if (MY_TAB === null || !working || agentTabId === null || agentTabId === MY_TAB) return;
  await chrome.tabs.update(agentTabId, { active: true }).catch(() => {});
}

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

// Wait until the tab finishes loading (max 15 seconds). A new tab the action opened is brought back
// into Krify's tab first (see onCreated), so it waits for the tab Krify is in after that.
async function waitForLoad(tabId) {
  await sleep(300);
  if (redirecting) {
    await redirecting;
    tabId = agentTabId ?? tabId;
  }
  for (let i = 0; i < 50; i++) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab || tab.status === "complete") return;
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

// ---------- embedded pages (iframes) ----------
// An embedded page (payment box, login box, form) is a separate page inside the main one.
// Its elements are numbered after the main page's, and each number remembers which page it's in.

const MAX_ELEMENTS = 100; // in total
const MAIN_SHARE = 80; // the main page's part when there are embedded pages, so theirs still fit
// get_page_model collects the whole page (the server decides how much the AI sees), so its caps
// are only a safety limit. The main page's share is kept below the total so embedded pages (a
// login or payment box) always get room.
const MODEL_MAX = 600;
const MODEL_MAIN_SHARE = 450;
const MODEL_FRAME_SHARE = 150;
const FRAME_TEXT = 1200; // characters of text read per embedded page
const elementFrame = new Map(); // element number → frame id (0 = main page)

// In-page functions whose first argument is an element number.
const ELEMENT_FNS = new Set(["click", "hover", "type", "pointAt", "spotOf", "hoverSpot", "focusCheck", "selectOption", "scrollAt", "describeId", "enterTarget", "leaveCheck"]);

/** Put Krify's page script in `target` (if it isn't there yet), plus pagehook.js in the page's own
 *  world (it reads React dropdowns' options for content.js). The hook is optional: if it can't be
 *  injected, dropdowns just show their options after select_option has opened them. */
async function injectKrify(target) {
  await chrome.scripting.executeScript({ target, files: ["context/content.js"] });
  try {
    await chrome.scripting.executeScript({ target, files: ["context/pagehook.js"], world: "MAIN" });
  } catch {
    // ignore
  }
}

/** The tab's embedded pages worth looking at, in page order: [{ frameId, label }]. */
async function embeddedPages(tabId) {
  try {
    await injectKrify({ tabId, allFrames: true });
    const results = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      func: () => window.__krify?.frameInfo(),
    });
    return results
      .filter((r) => r.frameId !== 0 && r.result?.usable)
      .sort((a, b) => a.frameId - b.frameId)
      .map((r) => ({ frameId: r.frameId, label: r.result.label }));
  } catch {
    return []; // a page Krify can't reach: just the main page
  }
}

/** Run an in-page function in the main page and every embedded page (errors ignored). */
async function callInAllFrames(fnName, args = []) {
  const tab = await webTab();
  try {
    await injectKrify({ tabId: tab.id, allFrames: true });
    await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: (name, a) => window.__krify?.[name](...a),
      args: [fnName, args],
    });
  } catch {
    await callInPage(fnName, args, null, 0);
  }
}

/** pointAt/spotOf as a spot on the main page (embedded pages report spots inside themselves). */
async function spotIn(fnName, element) {
  const spot = await callInPage(fnName, [element]);
  const frameId = elementFrame.get(String(element)) ?? 0;
  if (frameId === 0 || !spot) return spot;
  const off = await callInPage("frameOffset", [], null, frameId);
  return { ...spot, x: spot.x + off.x, y: spot.y + off.y };
}

/** A "(shows on hover)" button: put the real mouse on its row first, so the site shows it. A fake
 *  mouse-over doesn't trigger CSS :hover, which is how most sites show these buttons. */
async function revealOnHover(element) {
  const spot = await spotIn("hoverSpot", element);
  if (!spot) return;
  try {
    await realMove((await currentTab()).id, spot.x, spot.y);
    await sleep(300);
  } catch {
    // No real mouse (e.g. the user cancelled debugging): the normal click still reaches the button.
  }
}

// Inject content.js (if needed), then call one of its functions inside the page.
// frameId: which page – 0 is the main page; by default an element's own page.
async function callInPage(fnName, args = [], tabId = null, frameId = null) {
  const tab = tabId !== null ? await chrome.tabs.get(tabId) : await webTab();
  if (frameId === null) frameId = ELEMENT_FNS.has(fnName) ? elementFrame.get(String(args[0])) ?? 0 : 0;
  const target = { tabId: tab.id, frameIds: [frameId] };
  let result;
  try {
    const animate = await showCursor();
    const unrestricted = await showUnrestricted();
    const sameTab = await sameTabOn();
    // One call when Krify's script is already in the page (the usual case); only if it isn't – a
    // new page, or the page navigated in between – inject it and call again.
    const call = () => chrome.scripting.executeScript({
      target,
      func: async (name, a, animate, glow, unrestricted, sameTab) => {
        if (!window.__krify) return { missing: true };
        try {
          window.__krify.animate = animate;
          window.__krify.unrestricted = unrestricted;
          window.__krify.sameTab = sameTab;
          if (name !== "setWorking") window.__krify.setWorking(glow); // re-show after page loads
          return { ok: true, value: await window.__krify[name](...a) };
        } catch (err) {
          return { ok: false, error: err.message };
        }
      },
      args: [fnName, args, animate, working && animate, unrestricted, sameTab],
    });
    [result] = await call();
    if (result?.result?.missing) {
      await injectKrify(target);
      [result] = await call();
    }
  } catch (err) {
    // The embedded page went away (closed or reloaded).
    if (frameId !== 0) {
      const what = ELEMENT_FNS.has(fnName) ? `Element [${args[0]}] not found` : "The embedded page went away";
      throw new Error(`${what}. The page may have changed – get the elements again.`);
    }
    throw err;
  }
  const res = result?.result;
  if (!res) throw new Error("No answer from the page. Try again.");
  if (!res.ok) throw new Error(res.error);
  return res.value;
}

const clickWord = (button, count) =>
  `${button === "right" ? "Right-clicked" : ["Clicked", "Double-clicked", "Triple-clicked"][count - 1]}`;
// Sheets draws its right-click menu where page text can't be read (checked: not in the page,
// its frames or shadow parts), so the AI is pointed to what works.
const SHEETS_MENU_NOTE =
  " – a menu opened, but it can't be read as text: use the top menu bar" +
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
  redirectNote = "";
  const value = await callInPage(fnName, args, null, frameId);
  await waitForLoad((await currentTab()).id);
  // Content that loads after the action (a form's "Saved" message, results filled in by XHR).
  await callInPage("settle").catch(() => {});
  const note = redirectNote;
  redirectNote = "";
  return note && typeof value === "string" ? value + note : value;
}

export const operations = {
  // See the page
  async get_page_info() {
    const info = await callInPage("getPageInfo");
    if (krifyTabs.length > 1) info.tabs = await tabList();
    return info;
  },
  async get_elements() {
    // Main page first, then each embedded page, numbered on from where the last one stopped.
    const tab = await webTab();
    const frames = await embeddedPages(tab.id);
    if (frames.length) await callInAllFrames("forget"); // old numbers must not linger anywhere
    elementFrame.clear();
    const main = await callInPage("getElements", [0, frames.length ? MAIN_SHARE : MAX_ELEMENTS], null, 0);
    const parts = main.text ? [main.text] : [];
    let last = main.count;
    // Unrestricted: the main page isn't capped, so its count alone may pass MAX_ELEMENTS – that
    // must not skip the embedded pages (content.js ignores `max` in that mode anyway).
    const unrestricted = await showUnrestricted();
    for (const { frameId, label } of frames) {
      if (!unrestricted && last >= MAX_ELEMENTS) break;
      let inner;
      try {
        inner = await callInPage("getElements", [last, MAX_ELEMENTS - last], null, frameId);
      } catch {
        continue; // went away meanwhile
      }
      if (!inner.count) continue;
      for (let n = last + 1; n <= last + inner.count; n++) elementFrame.set(String(n), frameId);
      parts.push(`--- inside embedded page "${label}" ---\n${inner.text}`);
      last += inner.count;
    }
    return parts.join("\n") || "(no interactive elements visible)";
  },
  // Structured page data (see content.js buildPageModel): the main page and each embedded page,
  // numbered on from where the last one stopped. `full` = the side panel's Unrestricted switch:
  // the server shows everything instead of a screen-sized view.
  async get_page_model() {
    const tab = await webTab();
    const frames = await embeddedPages(tab.id);
    if (frames.length) await callInAllFrames("forget");
    elementFrame.clear();
    const full = await showUnrestricted();
    const main = await callInPage("getPageModel", [0, frames.length ? MODEL_MAIN_SHARE : MODEL_MAX], null, 0);
    const pages = [{ ...main, frame: "" }];
    let last = main.count;
    for (const { frameId, label } of frames) {
      // Unrestricted: every embedded page, all of it (content.js ignores `max` in that mode).
      const room = full ? MODEL_FRAME_SHARE : Math.min(MODEL_MAX - last, MODEL_FRAME_SHARE);
      if (room <= 0) break;
      let inner;
      try {
        inner = await callInPage("getPageModel", [last, room], null, frameId);
      } catch {
        continue; // went away meanwhile
      }
      if (!inner.count) continue;
      for (let n = last + 1; n <= last + inner.count; n++) elementFrame.set(String(n), frameId);
      pages.push({ ...inner, frame: label });
      last += inner.count;
    }
    return { full, pages };
  },
  // Main page only for now (not frame-aware): a fresh listing there, so the ids it returns are
  // immediately usable – forget any embedded-page ids from an earlier get_elements so nothing
  // from this page's new numbering is mistaken for one of them.
  find_elements: ({ query } = {}) => {
    elementFrame.clear();
    return callInPage("findElements", [query], null, 0);
  },
  video: ({ action, value } = {}) => callInPage("videoControl", [action, value ?? ""], null, 0),
  find_text: ({ query } = {}) => {
    elementFrame.clear();
    return callInPage("findText", [query], null, 0);
  },
  async read_text({ scope = "screen" } = {}) {
    // The main page's text, then each embedded page's (shorter), under its own heading.
    const tab = await webTab();
    const parts = [await callInPage("readText", [scope], null, 0)];
    const unrestricted = await showUnrestricted(); // then each embedded page's text is read whole too
    for (const { frameId, label } of await embeddedPages(tab.id)) {
      const text = await callInPage("readText", [scope, unrestricted ? null : FRAME_TEXT], null, frameId).catch(() => "");
      if (text && !text.startsWith("(no text")) parts.push(`--- inside embedded page "${label}" ---\n${text}`);
    }
    return parts.join("\n");
  },
  // Main page only for now (not frame-aware) – same reasoning as find_elements/find_text above.
  inspect({ ref, filter = "interactive", depth } = {}) {
    const frameId = ref != null ? elementFrame.get(String(ref)) ?? 0 : 0;
    if (ref == null) elementFrame.clear();
    return callInPage("getAccessibilityTree", [{ ref, filter, depth }], null, frameId);
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
    if (!krifyTabs.includes(tab.id)) krifyTabs.push(tab.id);
    openedByKrify.add(tab.id);
    await setWorkingIn(current.id, false);
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
  async click({ element, cell, button = "left", count = 1, real = false }) {
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
    await revealOnHover(element);
    if (btn === "left" && n === 1 && !real) return act("click", [element]); // a normal click: no debugger needed
    const tab = await currentTab();
    if (isSheetUrl(tab.url)) {
      // On Sheets the numbered elements are toolbar parts or Sheets' hidden cell editor, not cells:
      // right-clicking that editor opens Chrome's own text menu.
      throw new Error('On Google Sheets, right-click or double-click cells with click(cell="B22"), not element numbers.');
    }
    const spot = await spotIn("pointAt", element);
    await realClick(tab.id, spot.x, spot.y, { button: btn, clickCount: n });
    await sleep(300);
    await waitForLoad(tab.id);
    const note = btn === "right" && !needsRealInput(tab.url) ? RIGHT_CLICK_NOTE : "";
    return `${clickWord(btn, n)} [${element}]${real && btn === "left" && n === 1 ? " with the real mouse" : ""}${note}`;
  },
  // What [id] currently points at (for the staleness check before acting on an element number).
  describe_element: ({ element } = {}) => callInPage("describeId", [element]),
  // Would clicking [element] leave the form being filled (for the unsaved-form guard)?
  leave_check: ({ element } = {}) => callInPage("leaveCheck", [element]),
  // What pressing Enter would send (for the safety check): field, its form's default button, password?
  enter_target: ({ element } = {}) => callInPage("enterTarget", [element ?? null]),
  async drag({ from_element, to_element }) {
    const tab = await currentTab();
    const from = await spotIn("pointAt", from_element);
    const to = await spotIn("spotOf", to_element);
    const kind = await realDrag(tab.id, from, to);
    await callInPage("showPoint", [to.x, to.y, false]).catch(() => {});
    if (needsRealInput(tab.url)) edited();
    await sleep(300);
    return `Dragged [${from_element}] to [${to_element}]${kind === "html5" ? " (drag and drop)" : ""}`;
  },
  async hover({ element, real = false }) {
    if (!real) return act("hover", [element]);
    // A real mouse move, for menus that only open for a real mouse.
    const tab = await currentTab();
    const spot = await spotIn("pointAt", element);
    await realMove(tab.id, spot.x, spot.y);
    await sleep(600);
    return `Hovered over [${element}] with the real mouse`;
  },
  async type({ element, text, submit = false }) {
    const tab = await currentTab();
    if (!needsRealInput(tab.url)) {
      try {
        return await act("type", [element, text, Boolean(submit)]);
      } catch (e) {
        // A rich-text editor that ignored the in-page insert: type with the real keyboard instead.
        if (!/didn't register/.test(String(e?.message || e))) throw e;
      }
    }
    // Google Sheets/Docs ignore JavaScript typing: click the box for real, check the cursor is
    // really in it (never Ctrl+A: in Sheets that selects every cell), then type for real.
    const spot = await spotIn("pointAt", element);
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
    if (simple && times === 1 && !needsRealInput(tab.url)) return act("pressKey", [key]);
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
  // With an element: scroll the box/embedded page it's in. Without: the main page.
  // Then wait for what the scroll loads (infinite lists, lazy images and rows) before the next look.
  async scroll({ direction, element }) {
    const result = element != null ? await callInPage("scrollAt", [element, direction]) : await callInPage("scroll", [direction]);
    await callInPage("settle", [300, 2000]).catch(() => {});
    return result;
  },
  select_option: ({ element, option }) => act("selectOption", [element, option]),

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
