import { runOperation } from "./operations.js";

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

function start() {
  const text = taskInput.value.trim();
  if (!text || startBtn.disabled) return;
  addEntry("user", text);
  taskInput.value = "";
  setBusy(true);
  send({ type: "task", text, model: modelSelect.value });
}

// ---------- model picker (list comes from the server, choice is remembered) ----------

const modelSelect = $("modelSelect");

async function fillModels(models) {
  const { model: saved } = await chrome.storage.local.get("model");
  modelSelect.innerHTML = "";
  for (const m of models) {
    const option = document.createElement("option");
    option.value = m.id;
    option.textContent = m.label;
    modelSelect.append(option);
  }
  if (models.some((m) => m.id === saved)) modelSelect.value = saved;
}

modelSelect.addEventListener("change", () => {
  chrome.storage.local.set({ model: modelSelect.value });
});

startBtn.onclick = start;
stopBtn.onclick = () => send({ type: "stop" });
taskInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    start();
  }
});
$("newBtn").onclick = () => {
  send({ type: "reset" });
  logEl.innerHTML = '<div class="hint">New chat. What should I do?</div>';
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
