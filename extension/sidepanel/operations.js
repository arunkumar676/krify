// Runs browser operations. During a task Krify works in "its" tab, not whatever tab is active,
// and only sees/uses its own tabs: the one the task started in plus tabs it opened.

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- Krify's tabs ----------

let agentTabId = null; // tab Krify is working in during a task
const krifyTabs = []; // tab ids in the order Krify got them; [0] is the starting tab
const openedByKrify = new Set(); // only these may be closed by Krify

/** Called when a task starts: Krify works in the tab you're looking at. */
export async function beginTask() {
  const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!active) return;
  if (!krifyTabs.includes(active.id)) krifyTabs.push(active.id);
  agentTabId = active.id;
}

/** New chat: forget Krify's tabs (the tabs themselves stay open). */
export function resetTabs() {
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
  read_text: ({ scope = "screen" } = {}) => callInPage("readText", [scope]),

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
  type: ({ element, text }) => act("type", [element, text]),
  press_key: ({ key }) => act("pressKey", [key]),
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
