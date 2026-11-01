import { beginTask, resetTabs, runOperation, setWorking } from "./operations.js";

const SERVER_URL = "ws://127.0.0.1:8765/ws";

const $ = (id) => document.getElementById(id);
const logEl = $("log");
const taskInput = $("taskInput");
const startBtn = $("startBtn");
const stopBtn = $("stopBtn");
const statusEl = $("status");

let ws = null;

// ---------- step log ----------

// Escape HTML, then turn **text** into bold. Nothing else from the AI becomes HTML.
function formatText(text) {
  const escaped = String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return escaped.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function addEntry(kind, text) {
  logEl.querySelector(".hint")?.remove();
  const el = document.createElement("div");
  el.className = `entry ${kind}`;
  el.innerHTML = formatText(text);
  logEl.append(el);
  logEl.scrollTop = logEl.scrollHeight;
}

function setBusy(busy) {
  startBtn.hidden = busy;
  stopBtn.hidden = !busy;
  taskInput.disabled = busy;
  setWorking(busy); // glowing border on the page while Krify works
}

function setConnected(on) {
  statusEl.textContent = on ? "● Connected" : "● Not connected – start the server";
  statusEl.className = `status ${on ? "on" : "off"}`;
  startBtn.disabled = !on;
}

// ---------- connection to the Python server ----------

function connect() {
  ws = new WebSocket(SERVER_URL);
  ws.onopen = () => setConnected(true);
  ws.onclose = () => {
    setConnected(false);
    setBusy(false);
    setTimeout(connect, 3000); // keep trying
  };
  ws.onmessage = async (event) => {
    const msg = JSON.parse(event.data);
    switch (msg.type) {
      case "models":
        fillModels(msg.models);
        break;
      case "history":
        showHistory(msg.items);
        break;
      case "run": // the agent wants a browser operation
        try {
          const value = await runOperation(msg.name, msg.args);
          send({ type: "result", id: msg.id, ok: true, value });
        } catch (err) {
          send({ type: "result", id: msg.id, ok: false, error: err.message });
        }
        break;
      case "confirm":
        showConfirm(msg.id, msg.text);
        break;
      case "step":
        addEntry("step", msg.text);
        break;
      case "done":
        addEntry("done", `✅ ${msg.text}`);
        setBusy(false);
        break;
      case "ask":
        addEntry("ask", `❓ ${msg.text}`);
        setBusy(false);
        taskInput.focus();
        break;
      case "stopped":
        // Any open Allow / Block question no longer matters.
        logEl.querySelectorAll(".entry.confirm .row").forEach((row) => row.remove());
        addEntry("stopped", "⛔ Stopped.");
        setBusy(false);
        break;
      case "error":
        addEntry("error", `⚠️ ${msg.text}`);
        setBusy(false);
        break;
    }
  };
}

// Risky action: ask the user before Krify does it.
function showConfirm(id, text) {
  logEl.querySelector(".hint")?.remove();
  const box = document.createElement("div");
  box.className = "entry confirm";

  const title = document.createElement("div");
  title.innerHTML = `<strong>⚠️ Krify wants to:</strong>`;
  const what = document.createElement("div");
  what.textContent = text;
  const buttons = document.createElement("div");
  buttons.className = "row";
  const allow = document.createElement("button");
  allow.className = "allow";
  allow.textContent = "✅ Allow";
  const block = document.createElement("button");
  block.className = "block";
  block.textContent = "⛔ Block";
  buttons.append(allow, block);
  box.append(title, what, buttons);
  logEl.append(box);
  logEl.scrollTop = logEl.scrollHeight;

  const answer = (ok) => {
    send({ type: "confirm_result", id, allow: ok });
    buttons.remove();
    box.classList.add(ok ? "allowed" : "blocked");
  };
  allow.onclick = () => answer(true);
  block.onclick = () => answer(false);
}

function send(msg) {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

// ---------- task box ----------

async function start() {
  const text = taskInput.value.trim();
  if (!text || startBtn.disabled) return;
  addEntry("user", text);
  taskInput.value = "";
  await beginTask(); // Krify works in the tab you're looking at
  setBusy(true);
  send({ type: "task", text, model: selectedModel });
}

// ---------- model picker (list comes from the server, choice is remembered) ----------
// A custom dropdown, so each row can show the name on the left and today's usage on the right.

const modelButton = $("modelButton");
const modelList = $("modelList");
let models = [];
let selectedModel = "";

function showSelected() {
  const m = models.find((x) => x.id === selectedModel);
  modelButton.querySelector(".model-name").textContent = m ? m.label : "(connect to the server)";
  modelButton.querySelector(".model-status").textContent = m?.status || "";
  for (const li of modelList.children) li.setAttribute("aria-selected", li.dataset.id === selectedModel);
}

async function fillModels(list) {
  const { model: saved } = await chrome.storage.local.get("model");
  models = list;
  const wanted = selectedModel || saved;
  selectedModel = list.some((m) => m.id === wanted) ? wanted : list[0]?.id || "";

  modelList.innerHTML = "";
  for (const m of list) {
    const li = document.createElement("li");
    li.role = "option";
    li.dataset.id = m.id;
    const name = document.createElement("span");
    name.className = "model-name";
    name.textContent = m.label;
    const status = document.createElement("span");
    status.className = "model-status";
    status.textContent = m.status || "";
    li.append(name, status);
    li.onclick = () => {
      selectedModel = m.id;
      chrome.storage.local.set({ model: m.id });
      showSelected();
      modelList.hidden = true;
    };
    modelList.append(li);
  }
  showSelected();
}

modelButton.onclick = () => {
  if (models.length) modelList.hidden = !modelList.hidden;
};
document.addEventListener("click", (e) => {
  if (!e.target.closest(".model-picker")) modelList.hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") modelList.hidden = true;
});

startBtn.onclick = start;
stopBtn.onclick = () => send({ type: "stop" });
taskInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    start();
  }
});
function newChat() {
  send({ type: "reset" });
  resetTabs();
  logEl.innerHTML = '<div class="hint">New chat. What should I do?</div>';
}
$("newBtn").onclick = newChat;

// ---------- task history (saved by the server in its database) ----------

const historyView = $("historyView");
const historyList = $("historyList");
const STATUS_ICON = { done: "✅", ask: "❓", stopped: "⛔", error: "⚠️", running: "⏳" };

function openHistory(open) {
  historyView.hidden = !open;
  logEl.hidden = open;
  $("clearConfirm").hidden = true;
  if (open) send({ type: "history" });
}

function timeAgo(iso) {
  const minutes = Math.round((Date.now() - new Date(iso)) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  if (minutes < 24 * 60) return `${Math.round(minutes / 60)}h ago`;
  const days = Math.round(minutes / (24 * 60));
  return days === 1 ? "yesterday" : `${days} days ago`;
}

function showHistory(items) {
  historyList.innerHTML = "";
  if (!items.length) {
    historyList.innerHTML = '<li class="history-empty">No tasks yet.</li>';
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    li.title = "Click to put this task in the box (you can edit it)";

    const top = document.createElement("div");
    top.className = "top";
    const task = document.createElement("span");
    task.className = "task";
    task.textContent = `${STATUS_ICON[item.status] || "•"} ${item.text}`;
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = timeAgo(item.created_at);
    top.append(task, when);

    const bottom = document.createElement("div");
    bottom.className = "bottom";
    const summary = document.createElement("span");
    summary.className = "summary";
    summary.textContent = item.summary || "";
    const run = document.createElement("button");
    run.textContent = "▶";
    run.title = "Run again (in a new chat)";
    const del = document.createElement("button");
    del.textContent = "🗑";
    del.title = "Delete from history";
    bottom.append(summary, run, del);
    li.append(top, bottom);

    li.onclick = () => {
      // Put the task in the box so it can be edited before Start.
      openHistory(false);
      taskInput.value = item.text;
      taskInput.focus();
    };
    run.onclick = (e) => {
      e.stopPropagation();
      if (startBtn.disabled || startBtn.hidden) return; // not connected, or a task is running
      openHistory(false);
      newChat();
      taskInput.value = item.text;
      start();
    };
    del.onclick = (e) => {
      e.stopPropagation();
      send({ type: "history_delete", text: item.text });
    };
    historyList.append(li);
  }
}

$("historyBtn").onclick = () => openHistory(historyView.hidden);
$("closeHistoryBtn").onclick = () => openHistory(false);
$("clearHistoryBtn").onclick = () => ($("clearConfirm").hidden = false);
$("clearNoBtn").onclick = () => ($("clearConfirm").hidden = true);
$("clearYesBtn").onclick = () => {
  send({ type: "history_clear" });
  $("clearConfirm").hidden = true;
};

// ---------- "Show cursor" switch (remembered between sessions) ----------

const cursorToggle = $("cursorToggle");
chrome.storage.local.get("showCursor").then(({ showCursor = true }) => {
  cursorToggle.checked = showCursor;
});
cursorToggle.addEventListener("change", () => {
  chrome.storage.local.set({ showCursor: cursorToggle.checked });
});

// ---------- manual test buttons ----------

const output = $("output");

function argsFor(btn) {
  const element = Number($("elementInput").value);
  const text = $("textInput").value;
  switch (btn.dataset.op) {
    case "click": return { element };
    case "hover": return { element };
    case "type": return { element, text };
    case "select_option": return { element, option: text };
    case "open_url": return { url: text };
    case "press_key": return { key: btn.dataset.key };
    case "scroll": return { direction: btn.dataset.direction };
    case "wait": return { seconds: Number(btn.dataset.seconds) };
    default: return {};
  }
}

document.querySelectorAll("button[data-op]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    output.textContent = "Working…";
    output.classList.remove("error");
    try {
      const value = await runOperation(btn.dataset.op, argsFor(btn));
      output.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    } catch (err) {
      output.textContent = err.message;
      output.classList.add("error");
    }
  });
});

// ---------- start ----------

setConnected(false);
connect();
