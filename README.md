# Krify – AI Browser Agent

Krify is an AI agent that works inside your browser. You tell it what to do in plain language, and it **plans, acts and checks its own work**: opening sites, clicking, typing, choosing options, until the task is done.

> **Example:** *"search amazon.in for wireless mouse and sort by price low to high"*
> Krify opens Amazon, types the search, presses Go, picks "Price: Low to High" and confirms the results are sorted.

**Status:** working prototype. It runs on your own PC using free AI models.

---

## Features

- 🗣️ **Plain-language tasks**: casual wording and Hinglish work ("amazon pe sasta mouse dhundo")
- 🔁 **Plan → act → verify loop**: after every action Krify looks at the page again to check it worked
- 🖱️ **Browser actions**: open URL, back, reload, click, hover, type (+ Enter), press keys, scroll, choose from dropdowns, read page text
- 🗂️ **Tabs**: open a site in a new tab, switch between tabs, close tabs. Krify only sees and closes **its own** tabs, never your other ones
- ✨ **Animations (optional)**: a visible Krify cursor that moves, clicks and types letter by letter, plus a glowing border while Krify works
- 🔐 **Safety prompts in code**: Krify always asks **Allow / Block** before risky actions (paying, buying, sending, submitting, deleting, signing in or out, typing passwords or card details), even if your task already said to do it
- 🧠 **8 free AI models from 3 providers** with a model picker. When one model is busy or out of free quota, Krify **switches to the next automatically**
- 📊 **Usage per model**: the picker shows today's usage next to each model ("14k tokens today", "64% used", "limit reached")
- 🕘 **Task history**: past tasks with their result; click to edit and reuse, ▶ to run again, 🗑 to delete
- 📝 **Notes and screen reading**: Krify reads the text visible on screen and saves key facts (like prices) as notes, so it doesn't guess
- 📊 **Google Sheets**: Krify downloads the open sheet as CSV (your own login, private sheets too, nothing saved) and answers questions with SQL in a small in-memory database (DuckDB). Only the answer rows go to the AI. Read-only
- ⌨️ **Real keyboard when needed**: on Google Sheets/Docs and for shortcuts (Ctrl+Z, Ctrl+End…) Krify presses real keys through Chrome's debugger (Chrome shows a yellow bar while it works). It can jump to a cell, type into it, check the result and undo. Editing a sheet asks Allow / Block first
- 🖱️ **Real mouse when needed**: right-click, double/triple-click, drag and drop, and clicking a spot on a screenshot (what's under the spot gets the usual safety check)
- 🔁 **Repeat detector**: the same step 3 times with nothing changing → Krify is told to try another way
- 📸 **Screenshots**: when text isn't enough (pictures, colours, icons, maps), Krify takes a screenshot with element numbers drawn on it and switches to a model that can see images
- 📋 **Plans and fact check**: for longer tasks Krify writes a checklist first; every number in its final answer must have appeared on a page it read
- ⛔ **Stop anytime**: ends the task instantly
- 💬 **Follow-ups**: "now sort it", "open the second one"
- 📦 **Batch steps**: for forms and other sure steps, Krify does up to 5 actions in one go (still asking Allow / Block for risky ones), so a form takes 4 AI calls instead of 7
- 📉 **Token-saving**: only the latest page view is sent in full, and unchanged pages aren't sent again

---

## How it works

```
 ┌──────────────────────────┐        WebSocket         ┌───────────────────────────┐
 │  Chrome extension (JS)   │ ◄──────────────────────► │  Python server (FastAPI)  │
 │  "hands & eyes"          │                          │  "brain"                  │
 │                          │  ◄── "click element 12"  │                           │
 │ • reads page elements    │  ── "done + new page" ─► │ • agent loop              │
 │ • clicks, types, scrolls │                          │ • safety check            │
 │ • side panel UI          │                          │ • model auto-switch       │
 └──────────────────────────┘                          └─────────────┬─────────────┘
                                                                     ▼
                                                     Groq · Gemini · OpenRouter (free)
```

Krify "sees" a page as a short numbered list of what you can interact with:

```
[4] input text "Search Amazon.in" (empty)
[5] input submit "Go"
[45] select "Sort by" = "Featured" (options: Featured, Price: Low to High, ...)
```

The AI answers with one action at a time, e.g. `type(4, "wireless mouse")`, then `click(5)`. The extension performs it and sends back the updated page.

---

## Setup

**You need:** Windows/macOS/Linux, Python 3.12+, [uv](https://github.com/astral-sh/uv), and Google Chrome or Microsoft Edge.

### 1. Get free API keys

| Provider | Where | Notes |
|---|---|---|
| **Groq** (required) | [console.groq.com](https://console.groq.com) → API Keys | Free, no card |
| **Gemini** (recommended) | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Free, no card |
| **OpenRouter** (optional) | [openrouter.ai](https://openrouter.ai) → Keys | Small free daily allowance |

### 2. Set up the server

```bash
cd backend
cp .env.example .env      # then paste your keys into .env
uv sync
uv run python -m app.main
```

Leave it running. You should see `Uvicorn running on http://127.0.0.1:8765`.

> 🔑 **Never commit `.env`.** It's already listed in `.gitignore`.

### 3. Load the extension

1. Open `chrome://extensions` and turn on **Developer mode**.
2. Click **Load unpacked** and choose the `extension` folder.
3. Pin **Krify** and click its icon. The side panel opens and shows **● Connected**.

### 4. Try it

Type a task in the side panel and press **▶ Start**, for example:

- `search google for wireless mouse and open the first result`
- `go to wikipedia and find out when python programming language was created`
- `go to httpbin.org/forms/post and fill the form: name Arun, size medium, then submit it`

---

## Tech stack

| Part | Choice |
|---|---|
| Extension | Plain JavaScript, Chrome Manifest V3, side panel, content scripts |
| Server | Python 3.12+, FastAPI, Uvicorn, WebSocket |
| AI access | `openai` Python library (OpenAI-compatible APIs) pointed at Groq, Gemini and OpenRouter |
| Database | SQLite (`backend/krify.db`, created automatically) via SQLModel; PostgreSQL later for multi-user hosting |
| Sheet questions | DuckDB, in memory, with file and network access switched off |
| Agent loop | Custom: observe → decide → act → verify |
| Package manager | uv |

### Models (auto-switch order)

| Provider | Models (👁 = can read screenshots) |
|---|---|
| Groq | GPT-OSS 120B, Qwen 3.8 27B 👁, GPT-OSS 20B |
| Gemini | Gemini 3.8 Flash 👁, Gemini 3.5 Flash 👁, Gemini Flash Lite 👁 |
| OpenRouter | Nemotron Ultra, Nemotron Super 👁 |

A provider without a key in `.env` is skipped. Models are configured in [backend/app/config.py](backend/app/config.py).

---

## Project structure

```
krify/
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI server + WebSocket
│   │   ├── config.py        # providers, models, settings
│   │   ├── db.py            # SQLite database: tasks, steps, usage, limits
│   │   ├── history.py       # task history
│   │   ├── sheets.py        # Google Sheets: CSV → DuckDB → answers
│   │   ├── agent/
│   │   │   ├── loop.py      # the agent loop
│   │   │   ├── tools.py     # actions the AI can choose
│   │   │   ├── prompt.py    # instructions for the AI
│   │   │   └── safety.py    # what counts as risky
│   │   └── llm/
│   │       ├── client.py    # AI providers + auto-switch
│   │       └── usage.py     # tokens/requests per model per day
│   ├── krify.db             # your local database (not in Git)
│   ├── .env.example
│   └── pyproject.toml
├── extension/
│   ├── manifest.json
│   ├── background.js        # opens the side panel
│   ├── content.js           # runs in the page: reads it, acts, animations
│   └── sidepanel/           # chat UI, model picker, history, Allow/Block, sheets.js, realinput.js
├── README.md
└── TODO.md                  # progress checklist
```

---

## Roadmap

- [x] Basic actions, agent loop, side panel
- [x] Visible cursor and glowing border
- [x] Safety prompts (Allow / Block)
- [x] Multiple free AI providers with auto-switch
- [x] Tabs: open, switch, close (Krify only uses and closes its own tabs)
- [x] Task history and per-model usage (SQLite database)
- [x] Reliability on long tasks: plan checklist, goal shown at every step, scroll limit, wrap-up warning, fact check
- [x] Hover menus
- [x] Screenshots (let the AI see the page as an image)
- [x] Read Google Sheets (private too)

See [TODO.md](TODO.md) for the detailed checklist.

---

## Safety notes

Krify acts in your **real** browser, with your logged-in accounts.

- Risky actions always need your **Allow**, but stay alert and use **Stop** if anything looks wrong.
- Task history and usage are stored only on your PC in `backend/krify.db`.
- Page content is sent to the AI provider you choose. Free tiers may use that data, so avoid banking, medical or other sensitive pages.
- The server only accepts connections from the Krify extension on your own PC (`127.0.0.1`).
