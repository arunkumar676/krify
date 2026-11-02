// Runs browser operations. During a task Krify works in "its" tab, not whatever tab is active,
// and only sees/uses its own tabs: the one the task started in plus tabs it opened.

import { detachAll, needsRealInput, realClick, realKey, realType, resetRealInput } from "./realinput.js";
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

// Inject content.js (if needed), then call one of its functions inside the page.
async function callInPage(fnName, args = [], tabId = null) {
  const tab = tabId !== null ? await chrome.tabs.get(tabId) : await webTab();
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  const animate = await showCursor();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: async (name, a, animate, glow) => {
      try {
        window.__krify.animate = animate;
        if (name !== "setWorking") window.__krify.setWorking(glow); // re-show after page loads
        return { ok: true, value: await window.__krify[name](...a) };
      } catch (err) {
        return { ok: false, error: err.message };
      }
    },
    args: [fnName, args, animate, working && animate],
  });
  const res = result?.result;
  if (!res) throw new Error("No answer from the page. Try again.");
  if (!res.ok) throw new Error(res.error);
  return res.value;
}

// Make a screenshot smaller (fewer tokens): at most maxWidth pixels wide, compressed JPEG.
async function shrinkImage(dataUrl, maxWidth) {
  const bitmap = await createImageBitmap(await (await fetch(dataUrl)).blob());
  const scale = Math.min(1, maxWidth / bitmap.width);
  const canvas = new OffscreenCanvas(Math.round(bitmap.width * scale), Math.round(bitmap.height * scale));
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const blob = await canvas.convertToBlob({ type: "image/jpeg", quality: 0.6 });
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.readAsDataURL(blob);
  });
}

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
async function act(fnName, args) {
  const value = await callInPage(fnName, args);
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
  get_elements: () => callInPage("getElements"),
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
    return shrinkImage(shot, 1024);
  },
  read_text: ({ scope = "screen" } = {}) => callInPage("readText", [scope]),
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
  click: ({ element }) => act("click", [element]),
  hover: ({ element }) => act("hover", [element]),
  async type({ element, text, submit = false }) {
    const tab = await currentTab();
    if (!needsRealInput(tab.url)) return act("type", [element, text, Boolean(submit)]);
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
    const box = await callInPage("nameBox");
    await realClick(tab.id, box.x, box.y);
    await sleep(250);
    if (!(await callInPage("selectNameBox"))) {
      throw new Error("Couldn't put the cursor in the Name box – nothing was typed. Stop and tell the user.");
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
    edited();
    await sleep(300);
    return `Typed "${text}" where the cursor is`;
  },
  scroll: ({ direction }) => callInPage("scroll", [direction]),
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
