// Krify's side panel belongs to one tab: the tab where you clicked the Krify icon.
// Each such tab has its own chat. Any other tab (one you open, or one Krify opens) has no panel,
// so the panel is closed there. There's no shared panel: Chrome keeps a shared panel open on
// every tab, even ones told to hide it.
const PANEL = "sidepanel/sidepanel.html";

// Tabs that already have their panel, so clicking the icon again doesn't wipe their chat.
// Kept in session storage too, because Chrome stops this script when it's idle.
const hasChat = new Set();
const loaded = chrome.storage.session.get("hasChat").then((saved) => {
  (saved.hasChat || []).forEach((id) => hasChat.add(id));
  loaded.done = true;
});
const save = () => chrome.storage.session.set({ hasChat: [...hasChat] });

function setup() {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false }); // we open it ourselves below
  chrome.sidePanel.setOptions({ enabled: false }); // no shared panel
}
chrome.runtime.onInstalled.addListener(setup);
chrome.runtime.onStartup.addListener(setup);

function openPanel(tab) {
  // No await before open(): Chrome only opens a panel straight after a click.
  if (!hasChat.has(tab.id)) {
    hasChat.add(tab.id);
    save();
    chrome.sidePanel.setOptions({ tabId: tab.id, path: `${PANEL}?tab=${tab.id}`, enabled: true });
  }
  chrome.sidePanel.open({ tabId: tab.id });
}

chrome.action.onClicked.addListener((tab) => {
  if (loaded.done) openPanel(tab);
  else loaded.then(() => openPanel(tab)); // just woke up (rare): may need a second click
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (hasChat.delete(tabId)) save();
});
