import { currentLocation, initLocation } from "./location.js";
import { beginTask, resetTabs, runOperation, setWorking, showAgentTab, showMyTab } from "../tools/operations.js";

// The Krify server: the shared office server by default. Each person can change it under
// side panel → Server (e.g. 127.0.0.1 to use a server running on their own PC).
const DEFAULT_SERVER = "ws://10.10.2.125:8765/ws";
const DEFAULT_PORT = 8765;
let serverUrl = DEFAULT_SERVER;

// Temporarily off: token counts in the panel (the "· used 1.2k tokens" line under answers, the model
// picker's "64% used" / "18k tokens today", the AI view's "≈ N tokens"). The server still counts them.
// Set to true to show them again.
const SHOW_TOKENS = false;

const $ = (id) => document.getElementById(id);
const logEl = $("log");
const taskInput = $("taskInput");
const startBtn = $("startBtn");
const stopBtn = $("stopBtn");
const statusEl = $("status");

let ws = null;

// ---------- step log ----------
// Escape HTML, then turn **text** into bold and "- item" / "1. item" lines into lists. Nothing else
// from the AI becomes HTML.
const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;

function formatText(text) {
  const escaped = String(text)
    .replace(/\\n/g, "\n") // a model that wrote "\n" inside its answer instead of a line break
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  const out = [];
  let list = null; // "ul" | "ol" while inside a list
  for (const line of escaped.split("\n")) {
    if (!line.trim() && (list || out.at(-1)?.startsWith("</"))) continue; // blank line after a list
    const bullet = BULLET.exec(line);
    const numbered = !bullet && NUMBERED.exec(line);
    const kind = bullet ? "ul" : numbered ? "ol" : null;
    if (list && kind !== list) out.push(`</${list}>`);
    if (kind && kind !== list) {
      // A list starts its own line: drop the line break (and blank lines) before it.
      while (out.at(-1) === "\n") out.pop();
      if (out.length) out[out.length - 1] = out.at(-1).replace(/\n$/, "");
      out.push(`<${kind}>`);
    }
    list = kind;
    if (kind) out.push(`<li>${(bullet || numbered)[1]}</li>`);
    else out.push(`${line}\n`);
  }
  if (list) out.push(`</${list}>`);
  return out.join("").replace(/\n$/, "");
}

// One-line plain text for previews (task history): no ** marks, list lines joined with ", ".
function plainText(text) {
  const lines = String(text || "").replace(/\\n/g, "\n").replace(/\*\*/g, "").split("\n").map((l) => l.trim()).filter(Boolean);
  const items = lines.map((l) => (BULLET.exec(l) || NUMBERED.exec(l) || [l, l])[1]);
  const lead = lines.length > 1 && !BULLET.test(lines[0]) && !NUMBERED.test(lines[0]) ? items.shift() : "";
  return lead ? `${lead} ${items.join(", ")}` : items.join(", ");
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
  const host = new URL(serverUrl).hostname;
  const local = host === "127.0.0.1" || host === "localhost";
  statusEl.textContent = on ? "● Connected" : local ? "● Not connected – start the server"
    : `● Not connected – is the Krify server at ${host} running?`;
  statusEl.className = `status ${on ? "on" : "off"}`;
  startBtn.disabled = !on;
}

// ---------- connection to the Python server ----------

// "192.168.1.20", "192.168.1.20:8765" or "ws://192.168.1.20:8765/ws" → full ws:// address.
function normalizeServer(input) {
  let text = (input || "").trim();
  if (!text) return DEFAULT_SERVER;
  if (!/^wss?:\/\//i.test(text)) text = `ws://${text.replace(/^https?:\/\//i, "")}`;
  const url = new URL(text); // throws on nonsense
  if (!url.port) url.port = String(DEFAULT_PORT);
  if (url.pathname === "/" || !url.pathname) url.pathname = "/ws";
  url.search = "";
  return url.toString();
}

// A random id this install keeps, so the server keeps each person's task history apart.
async function clientId() {
  let { clientId: id } = await chrome.storage.local.get("clientId");
  if (!id) {
    id = crypto.randomUUID();
    await chrome.storage.local.set({ clientId: id });
  }
  return id;
}

async function connect() {
  const saved = (await chrome.storage.local.get("serverUrl")).serverUrl;
  serverUrl = saved || DEFAULT_SERVER;
  // Fill the Server box only once: this runs again every 3 s while disconnected, and refilling it
  // would wipe an address the user is typing (exactly when they need to type one).
  if (!serverInput.dataset.filled) {
    serverInput.value = serverUrl;
    serverInput.dataset.filled = "1";
  }
  ws = new WebSocket(`${serverUrl}?client=${encodeURIComponent(await clientId())}`);
  ws.onopen = () => {
    setConnected(true);
    serverNote.textContent = "";
    send({ type: "dev_mode", on: devModeToggle.checked }); // resync after (re)connect
  };
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
      case "llm_report":
        showReport(msg);
        break;
      case "ai_view":
        showAiView(msg);
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
        showMyTab(); // you answer here, so show this panel's tab
        showConfirm(msg.id, msg.text);
        break;
      case "plan_review":
        showPlanReview(msg.id, msg.steps);
        break;
      case "step":
        addEntry("step", msg.text);
        break;
      case "done":
        addEntry("done", `✅ ${SHOW_TOKENS ? msg.text : msg.text.replace(/\n· used [\d.]+k tokens.*$/, "")}`);
        setBusy(false);
        showMyTab();
        break;
      case "ask":
        addEntry("ask", `❓ ${msg.text}`);
        setBusy(false);
        showMyTab();
        taskInput.focus();
        break;
      case "stopped":
        // Any open Allow / Block question, or plan review, no longer matters.
        logEl.querySelectorAll(".entry.confirm .row").forEach((row) => row.remove());
        logEl.querySelectorAll(".entry.plan-review .row, .entry.plan-review .plan-step-add").forEach((el) => el.remove());
        logEl.querySelectorAll(".entry.plan-review input").forEach((input) => (input.disabled = true));
        logEl.querySelectorAll(".entry.plan-review .plan-step-remove").forEach((btn) => btn.remove());
        addEntry("stopped", "⛔ Stopped.");
        setBusy(false);
        break;
      case "error":
        addEntry("error", `⚠️ ${msg.text}`);
        setBusy(false);
        showMyTab();
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
    showAgentTab(); // back to the tab Krify is working in
  };
  allow.onclick = () => answer(true);
  block.onclick = () => answer(false);
}

// Medium/large task: show the proposed plan (each step editable) and wait for
// Approve / Update / Reject before Krify does anything on the page.
function showPlanReview(id, steps) {
  logEl.querySelector(".hint")?.remove();
  const box = document.createElement("div");
  box.className = "entry plan-review";

  const title = document.createElement("div");
  title.innerHTML = `<strong>📋 Krify's plan – review before it starts:</strong>`;

  const list = document.createElement("div");
  list.className = "plan-steps";

  const addRow = (text) => {
    const row = document.createElement("div");
    row.className = "plan-step-row";
    const input = document.createElement("input");
    input.type = "text";
    input.value = text;
    const remove = document.createElement("button");
    remove.className = "plan-step-remove";
    remove.type = "button";
    remove.textContent = "✕";
    remove.title = "Remove this step";
    remove.onclick = () => row.remove();
    row.append(input, remove);
    list.append(row);
  };
  (steps || []).forEach(addRow);

  const addBtn = document.createElement("button");
  addBtn.className = "plan-step-add";
  addBtn.type = "button";
  addBtn.textContent = "+ Add step";
  addBtn.onclick = () => addRow("");

  const buttons = document.createElement("div");
  buttons.className = "row";
  const approve = document.createElement("button");
  approve.className = "allow";
  approve.textContent = "✅ Approve";
  const update = document.createElement("button");
  update.className = "update";
  update.textContent = "✏️ Update";
  const reject = document.createElement("button");
  reject.className = "block";
  reject.textContent = "❌ Reject";
  buttons.append(approve, update, reject);

  box.append(title, list, addBtn, buttons);
  logEl.append(box);
  logEl.scrollTop = logEl.scrollHeight;

  const currentSteps = () =>
    [...list.querySelectorAll("input")].map((i) => i.value.trim()).filter(Boolean);

  const answer = (action) => {
    send({ type: "plan_review_result", id, action, steps: currentSteps() });
    addBtn.remove();
    buttons.remove();
    list.querySelectorAll(".plan-step-remove").forEach((btn) => btn.remove());
    list.querySelectorAll("input").forEach((input) => (input.disabled = true));
    box.classList.add(action === "reject" ? "blocked" : "allowed");
  };
  approve.onclick = () => answer("approve");
  update.onclick = () => answer("update");
  reject.onclick = () => answer("reject");
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
  send({ type: "task", text, model: selectedModel, location: currentLocation() });
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
  modelButton.querySelector(".model-status").textContent = SHOW_TOKENS ? m?.status || "" : "";
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
    status.textContent = SHOW_TOKENS ? m.status || "" : "";
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
  if (open) reportView.hidden = true;
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
    summary.textContent = plainText(item.summary);
    const run = document.createElement("button");
    run.textContent = "▶";
    run.title = "Run again (in a new chat)";
    const rep = document.createElement("button");
    rep.textContent = "📊";
    rep.title = item.calls ? `LLM report: ${item.calls} calls, ${item.prompt_tokens} in / ${item.completion_tokens} out tokens` : "LLM report";
    const del = document.createElement("button");
    del.textContent = "🗑";
    del.title = "Delete from history";
    bottom.append(summary, rep, run, del);
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
    rep.onclick = (e) => {
      e.stopPropagation();
      openHistory(false);
      openReport(true, item.id);
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

// ---------- LLM report: every request sent to an AI model, with its answer and token counts ----------

const reportView = $("reportView");
const reportBody = $("reportBody");
let lastReport = null; // { report, markdown } – what the download buttons save

function openReport(open, taskId) {
  reportView.hidden = !open;
  logEl.hidden = open;
  historyView.hidden = true;
  if (!open) return;
  reportBody.textContent = "Loading…";
  send(taskId ? { type: "llm_report", task_id: taskId } : { type: "llm_report" });
}

const fmtN = (n) => (n == null ? "n/a" : n.toLocaleString());

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function messageText(m) {
  let body = m.content;
  if (Array.isArray(body)) body = body.map((p) => p.text ?? p.image_url?.url ?? "").join("\n");
  if (m.tool_calls?.length) body = `${body || ""}\ntool_calls: ${JSON.stringify(m.tool_calls)}`;
  return body || "";
}

function table(headers, rows) {
  const t = el("table");
  const head = el("tr");
  headers.forEach((h) => head.append(el("th", h)));
  t.append(head);
  rows.forEach((r) => {
    const tr = el("tr");
    r.forEach((c) => tr.append(el("td", c)));
    t.append(tr);
  });
  return t;
}

function callBlock(c) {
  const box = el("details");
  if (c.status !== "ok") box.className = "failed";
  const where = c.purpose === "planner" ? "planner" : `step ${c.step}`;
  const tokens = c.status === "ok" ? `${fmtN(c.prompt_tokens)} in / ${fmtN(c.completion_tokens)} out` : c.status;
  box.append(el("summary", `#${c.seq} ${where} · ${c.model.split(":")[1] || c.model} · ${tokens} · ${(c.duration_ms / 1000).toFixed(1)}s`));
  if (c.error) box.append(el("div", c.error, "muted"));
  if (!c.request) {
    box.append(el("div", "Text no longer stored (older than the newest 50 tasks).", "muted"));
    return box;
  }
  const roles = Object.entries(c.sent_by_role).map(([r, v]) => `${r} ×${v.messages} (${fmtN(v.chars)} chars)`).join(" · ");
  box.append(el("h4", `Sent – ${c.message_count} messages: ${roles}`));
  box.append(el("div", `Tools offered: ${c.request.tools.join(", ") || "none"}`, "muted"));
  for (const m of c.request.messages) {
    const d = el("details");
    d.append(el("summary", `${m.role}${m.tool_calls ? " (tool calls)" : ""} · ${fmtN(messageText(m).length)} chars`));
    d.append(el("pre", messageText(m)));
    box.append(d);
  }
  box.append(el("h4", `Received – ${fmtN(c.completion_tokens)} tokens${c.reasoning_tokens ? ` (${fmtN(c.reasoning_tokens)} reasoning)` : ""}`));
  const r = c.response || {};
  if (r.content) box.append(el("pre", r.content));
  for (const call of r.tool_calls || []) box.append(el("pre", `${call.function?.name}(${call.function?.arguments})`));
  if (!r.content && !(r.tool_calls || []).length) box.append(el("div", "(no reply)", "muted"));
  return box;
}

function showReport(msg) {
  reportBody.replaceChildren();
  if (!msg.ok) {
    reportBody.textContent = msg.error;
    lastReport = null;
    return;
  }
  lastReport = msg;
  const { task, totals: t, by_model, calls } = msg.report;
  $("reportTitle").textContent = `📊 LLM report – task ${task.id}`;
  reportBody.append(el("div", `${STATUS_ICON[task.status] || "•"} ${task.text}`));
  const cards = el("div", null, "report-cards");
  [
    [t.calls, `LLM calls (${t.ok} ok, ${t.failed} failed)`],
    [fmtN(t.prompt_tokens), "tokens sent"],
    [fmtN(t.completion_tokens), "tokens received"],
    [fmtN(t.total_tokens), "tokens total"],
    [fmtN(t.reasoning_tokens), "of received: reasoning"],
    [`${(t.duration_ms / 1000).toFixed(1)}s`, "waiting for models"],
  ].forEach(([v, label]) => {
    const card = el("div", null, "report-card");
    card.append(el("b", String(v)), el("span", label));
    cards.append(card);
  });
  reportBody.append(cards);
  if (t.tokens_unknown) reportBody.append(el("div", `${t.tokens_unknown} call(s): the provider sent no token counts.`, "muted"));
  reportBody.append(table(["Model", "Calls", "Failed", "Sent", "Received", "Total"],
    Object.entries(by_model).map(([m, g]) => [m, g.calls, g.failed, fmtN(g.prompt_tokens), fmtN(g.completion_tokens), fmtN(g.total_tokens)])));
  reportBody.append(el("h4", "Every call"));
  calls.forEach((c) => reportBody.append(callBlock(c)));
}

function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  a.click();
  URL.revokeObjectURL(url);
}

$("reportBtn").onclick = () => openReport(reportView.hidden);
$("closeReportBtn").onclick = () => openReport(false);
$("reportMdBtn").onclick = () => lastReport && download(`krify-llm-report-task-${lastReport.report.task.id}.md`, lastReport.markdown, "text/markdown");
$("reportJsonBtn").onclick = () =>
  lastReport && download(`krify-llm-report-task-${lastReport.report.task.id}.json`, JSON.stringify(lastReport.report, null, 2), "application/json");

// ---------- location (country + optional exact place) ----------

initLocation((text) => addEntry("step", text));

// ---------- "Show cursor" switch (remembered between sessions) ----------

const cursorToggle = $("cursorToggle");
chrome.storage.local.get("showCursor").then(({ showCursor = true }) => {
  cursorToggle.checked = showCursor;
});
cursorToggle.addEventListener("change", () => {
  chrome.storage.local.set({ showCursor: cursorToggle.checked });
});

// ---------- "Dev mode" switch (debug trace under the final answer; synced to the server) ----------

const devModeToggle = $("devModeToggle");
chrome.storage.local.get("devMode").then(({ devMode = false }) => {
  devModeToggle.checked = devMode;
  send({ type: "dev_mode", on: devMode });
});
devModeToggle.addEventListener("change", () => {
  chrome.storage.local.set({ devMode: devModeToggle.checked });
  send({ type: "dev_mode", on: devModeToggle.checked });
});

// ---------- "Unrestricted" switch (skip content.js's token-saving caps; purely extension-side,
// read fresh by operations.js on every page call – no server involvement needed) ----------

const unrestrictedToggle = $("unrestrictedToggle");
chrome.storage.local.get("unrestricted").then(({ unrestricted = false }) => {
  unrestrictedToggle.checked = unrestricted;
});
unrestrictedToggle.addEventListener("change", () => {
  chrome.storage.local.set({ unrestricted: unrestrictedToggle.checked });
});

// ---------- "Same tab" switch (new-tab links open in Krify's tab; read by operations.js per call) ----------

const sameTabToggle = $("sameTabToggle");
chrome.storage.local.get("sameTab").then(({ sameTab = true }) => {
  sameTabToggle.checked = sameTab;
});
sameTabToggle.addEventListener("change", () => {
  chrome.storage.local.set({ sameTab: sameTabToggle.checked });
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

// "AI view": the server renders the page the same way it does for the AI (pageview.py), so this
// shows the real text the model gets – not the raw get_page_model JSON or the old flat list.
$("aiViewBtn").addEventListener("click", () => {
  output.classList.remove("error");
  if (ws?.readyState !== WebSocket.OPEN) {
    output.textContent = "Not connected – start the server first.";
    output.classList.add("error");
    return;
  }
  output.textContent = "Working…";
  send({ type: "ai_view" });
});

function showAiView(msg) {
  if (!msg.ok) {
    output.textContent = msg.error;
    output.classList.add("error");
    return;
  }
  const chars = msg.text.length;
  const mode = unrestrictedToggle.checked ? "🔓 Unrestricted (whole page, full labels)" : "restricted (on-screen, short labels)";
  const size = SHOW_TOKENS ? `${chars} chars ≈ ${Math.round(chars / 4)} tokens` : `${chars} chars`;
  output.textContent = `${msg.text}\n\n— ${size} · ${mode}`;
  output.scrollTop = 0; // start at the top ([Page], [Form], …) – not the last line of a long view
}

document.querySelectorAll("button[data-op]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    output.textContent = "Working…";
    output.classList.remove("error");
    try {
      if (btn.dataset.op === "screenshot") await runOperation("get_elements"); // number the elements first
      const value = await runOperation(btn.dataset.op, argsFor(btn));
      if (typeof value === "string" && value.startsWith("data:image/")) {
        const img = document.createElement("img");
        img.src = value;
        img.style.maxWidth = "100%";
        output.replaceChildren(img, `\n${Math.round(value.length / 1024)} KB`);
        return;
      }
      output.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    } catch (err) {
      output.textContent = err.message;
      output.classList.add("error");
    }
  });
});

// ---------- start ----------

setConnected(false);
// ---------- server address ----------
const serverInput = $("serverInput");
const serverNote = $("serverNote");
$("serverSave").onclick = async () => {
  let url;
  try {
    url = normalizeServer(serverInput.value);
  } catch {
    serverNote.textContent = "That doesn't look like an address. Try e.g. 192.168.1.20";
    return;
  }
  await chrome.storage.local.set({ serverUrl: url });
  serverInput.value = url;
  serverNote.textContent = `Connecting to ${url} …`;
  ws?.close(); // onclose reconnects, to the new address
};
serverInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("serverSave").click();
});

connect();
