// Runs browser operations on the active tab.

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function currentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) throw new Error("No active tab.");
  return tab;
}

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

// The "Show cursor" switch in the side panel (on unless turned off).
async function showCursor() {
  const { showCursor = true } = await chrome.storage.local.get("showCursor");
  return showCursor;
}

// True while the agent runs a task (set by the side panel). Drives the glowing page border.
let working = false;

export async function setWorking(on) {
  working = on;
  try {
    await callInPage("setWorking", [on && (await showCursor())]);
  } catch {
    // Protected page (e.g. chrome://): nothing to show.
  }
}

// Inject content.js (if needed), then call one of its functions inside the page.
async function callInPage(fnName, args = []) {
  const tab = await webTab();
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
  get_page_info: () => callInPage("getPageInfo"),
  get_elements: () => callInPage("getElements"),
  read_text: () => callInPage("readText"),

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
