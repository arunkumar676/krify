# Krify – AI Browser Agent

Krify is an AI-powered browser agent. You tell the browser what to do in plain language, and the AI **plans, executes and verifies** the task for you, with minimal manual effort.

> **Example:** *"Search for wireless mice under ₹1000 on this site and sort by rating."*
> Krify reads the page, types the search, applies the filter, sorts the results and confirms the task is done.

> **Status:** 🚧 Early development. Currently in the planning and setup phase.

---

## How it works

Krify runs a continuous **observe → decide → act → verify** loop:

```
User: "Find the cheapest flight from Hyderabad to Delhi next Friday"
        │
        ▼
 ┌──────────────┐
 │  1. PLAN     │  AI breaks the goal into steps
 └──────┬───────┘
        ▼
 ┌──────────────┐
 │  2. OBSERVE  │  Extension reads the page and lists interactive elements
 └──────┬───────┘
        ▼
 ┌──────────────┐
 │  3. DECIDE   │  AI picks one action: click [12], type "Delhi" into [7], scroll...
 └──────┬───────┘
        ▼
 ┌──────────────┐
 │  4. ACT      │  Extension performs the action in the real browser
 └──────┬───────┘
        ▼
 ┌──────────────┐
 │  5. VERIFY   │  Re-observe: did it work? If not, retry or re-plan
 └──────┬───────┘
        └──── repeat until done ──► final check ──► report to user
```

### Architecture

```
┌─────────────────────────┐   WebSocket (two-way)   ┌──────────────────────────┐
│  Browser Extension (JS) │ ◄─────────────────────► │  Python Backend          │
│  "Hands & Eyes"         │                         │  "Brain"                 │
│                         │  ◄── "click element 12" │                          │
│ • Reads page elements   │  ── "done, new page" ─► │ • Agent loop             │
│ • Clicks, types, scrolls│                         │ • Calls the AI model     │
│ • Opens/switches tabs   │                         │ • Planning + verification│
│ • Side panel UI         │                         │ • Safety rules           │
│ • Live step log + Stop  │                         │ • Logging                │
└─────────────────────────┘                         └────────────┬─────────────┘
                                                                 │
                                                                 ▼
                                                        ┌─────────────────┐
                                                        │  Groq AI (LLM)  │
                                                        └─────────────────┘
```

**How the AI "sees" the page:** the extension turns each page into a compact, numbered list of visible interactive elements, for example:

```
[1]  link    "Home"
[7]  input   "Search products" (empty)
[12] button  "Search"
[15] select  "Sort by" = "Relevance"
```

The AI responds with an action such as `type(7, "wireless mouse")` or `click(12)`, which the extension carries out.

---

## Planned features

- 🗣️ **Natural-language tasks**: describe what you want; no scripting needed
- 🧠 **Planning**: complex goals are broken into steps
- 🖱️ **Browser actions**: click, type, select, scroll, navigate, open and switch tabs
- ✅ **Verification**: checks each step worked, and retries or re-plans when it doesn't
- 📋 **Live step log**: see exactly what the agent is doing, in real time
- ⛔ **Stop anytime**: one click halts the agent
- 🔐 **Safety confirmations**: asks before payments, sending messages, deleting data or entering passwords
- 🛡️ **Prompt-injection protection**: page content is treated as data, never as instructions

---

## Tech stack

The project is built entirely on **free** tools and services.

### Browser extension
| Part | Choice |
|---|---|
| Language | Plain JavaScript |
| Platform | Chrome / Edge, Manifest V3 |
| UI | Side panel (HTML + CSS) |
| Page reading & actions | Content scripts, `chrome.tabs` |
| Connection to backend | WebSocket |

### Backend
| Part | Choice |
|---|---|
| Language | Python 3.12+ |
| Package manager | [uv](https://github.com/astral-sh/uv) |
| Web framework | [FastAPI](https://fastapi.tiangolo.com/) |
| Server | Uvicorn |
| Real-time link | FastAPI WebSocket |
| Data validation | Pydantic v2 |
| Config / secrets | python-dotenv + `.env` |
| Agent loop | Custom (plan → observe → act → verify) |
| Logging | Python `logging` to files |

### AI
| Part | Choice |
|---|---|
| Provider | [Groq](https://groq.com/) (free tier) |
| SDK | `groq` Python SDK |
| Model | A Groq-hosted model with strong tool calling (configurable) |
| Design | Provider adapter: models and providers can be swapped via config |
| Perception | Text-first element list; screenshots as an optional fallback |

### Development tools
| Part | Choice |
|---|---|
| Editor | VS Code |
| Version control | Git + GitHub |
| Lint / format | Ruff |
| Tests | pytest (Playwright for end-to-end agent tests later) |

---

## Project structure (planned)

```
krify/
├── backend/
│   ├── app/
│   │   ├── main.py             # FastAPI app + WebSocket endpoint
│   │   ├── config.py           # Settings loaded from .env
│   │   ├── agent/
│   │   │   ├── loop.py         # plan → observe → act → verify
│   │   │   ├── tools.py        # Browser action definitions for the AI
│   │   │   ├── prompts.py      # System prompts
│   │   │   └── safety.py       # Confirmations, limits
│   │   └── llm/
│   │       └── groq_client.py  # Groq adapter
│   ├── .env.example            # Required settings (no real values)
│   └── pyproject.toml
│
├── extension/
│   ├── manifest.json
│   ├── background.js           # WebSocket link, routes actions
│   ├── content.js              # Reads the page, performs actions
│   └── sidepanel/
│       ├── sidepanel.html
│       ├── sidepanel.css
│       └── sidepanel.js
│
└── README.md
```

---

## Roadmap

| Phase | Goal | Status |
|---|---|---|
| **1. Foundation** | Python WebSocket server, extension connects to it, side panel with task input and step log | ⏳ Next |
| **2. Eyes** | Page observation: numbered element list and page text | 🔜 Planned |
| **3. Hands** | Actions: click, type, scroll, select, navigate, tabs | 🔜 Planned |
| **4. Brain** | Agent loop: plan, act, observe, verify, finish | 🔜 Planned |
| **5. Safety** | Confirmation prompts, Stop button, step limits, injection defences | 🔜 Planned |
| **6. Polish** | Task history, smarter retries, multi-tab tasks, accounts, deployment | 🔜 Planned |

---

## Getting started

Setup instructions will be added in **Phase 1**. You will need:

- Python 3.12+ and [uv](https://github.com/astral-sh/uv)
- Google Chrome or Microsoft Edge
- A free Groq API key from [console.groq.com](https://console.groq.com/)

> 🔑 **Never commit your API key.** It belongs in `backend/.env`, which is excluded from Git.

---

## Safety note

Krify acts inside your real browser. While it is in development:

- Use test accounts where possible.
- Avoid banking, payment and other sensitive sites.
- Keep an eye on the live step log, and use **Stop** if anything looks wrong.
