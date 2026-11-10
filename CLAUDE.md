# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Krify is a browser AI agent: a Chrome extension (Manifest V3) drives a tab via DOM scripting / the
`chrome.debugger` protocol, and a local Python FastAPI server runs the agent loop that decides what
to do, one operation at a time, using free-tier LLMs (Groq, Gemini, OpenRouter). The two halves talk
over a WebSocket on `127.0.0.1:8765`.

## Commands

Backend (from `backend/`):
```bash
cp .env.example .env        # then add GROQ_API_KEY (required) and optionally GEMINI_API_KEY / OPENROUTER_API_KEY
uv sync                     # install deps
uv run python -m app.main   # start the server (ws://127.0.0.1:8765/ws)
uv run python hello_groq.py # sanity-check the Groq key alone
uv run python try_nlu.py                       # chat-mode test of the agent's tool-calling against a fake page, no browser needed
uv run python try_nlu.py "search for wireless mouse"  # same, one sentence
```
There is no lint/format/test tooling configured (no ruff/pytest/etc. in `pyproject.toml`) and no
test suite — verify backend changes with `try_nlu.py` or by running the real extension against the
server.

Extension (no build step — plain JS, ES modules):
1. `chrome://extensions` → enable Developer mode → **Load unpacked** → select `extension/`.
2. After editing extension files, click the reload icon on the extension card (content script
   changes also need the target page reloaded).
3. The side panel must show **● Connected** (i.e. the backend is running) to do anything.

Using one backend from other PCs on the network: set `HOST=0.0.0.0` in `backend/.env` (default
`127.0.0.1` = this PC only; the server logs the address to use on startup), allow TCP 8765 from the LAN
in the firewall, and on each other PC load `extension/` and enter the server's IP under the side
panel's **Server** section (saved in `chrome.storage.local` as `serverUrl`). There's no access key:
only the `chrome-extension://` Origin check, so use it on trusted networks only. Each install sends a
random `?client=` id on connect; task history is per client (`Task.client`), but only one task runs at a
time across everyone (`running` in `main.py`) and AI quotas are shared.

## Architecture

### Two processes, one WebSocket protocol

The side panel (`extension/sidepanel/sidepanel.js`) opens `ws://127.0.0.1:8765/ws`. The server
(`backend/app/main.py`) only accepts connections whose `Origin` starts with `chrome-extension://`.
Full message shapes are documented in the module docstring of `main.py`. The important thing to
understand: the server never touches the DOM itself — every browser action is a round trip where
the server sends `{"type": "run", "id", "name", "args"}` and *waits* on a `Future` keyed by `id`
until the extension replies with `{"type": "result", "id", "ok", "value"|"error"}`. `confirm`/
`confirm_result` (Allow/Block prompts) work the same way with a separate id space.

### The agent loop (`backend/app/agent/loop.py`)

One `Agent` instance per side-panel connection (so follow-up tasks share chat history). `Agent.run()`
is the core loop: build a page view → call `ask_ai()` with the running `messages` list and `TOOLS` →
the model replies with one or more tool calls → each call is dispatched (browser action, `sheet_query`,
`plan`, `note`, `done`, `ask_user`, …) → the result (plus a fresh page view) is appended back into
`messages` → repeat, up to `MAX_STEPS` (`config.py`, default 25).

Key mechanics worth knowing before touching this file:
- **Batching**: the model can send several `ACTION_OPS` tool calls in one reply (or use the explicit
  `batch` tool), up to `MAX_BATCH` (5). The loop stops a batch early if the page's *layout* changed
  (`layout()` strips typed values before comparing) and a later queued step references an element
  number — because numbers may have shifted.
- **Token trimming**: only the newest page view / screen read / screenshot is kept in full in
  `messages`; older ones are replaced with a short placeholder (`OLD_PAGE`, `OLD_SCREEN`,
  `OLD_SCREENSHOT`). `self.notes` (from the `note` tool) are what survive long-term and get
  re-injected via `header()` on every page view.
- **Stuck/repeat detection**: `check_progress` (5 non-progress steps → nudge) and `check_repeat`
  (same op+args 3× in 6 steps with no page change → nudge) inject warning text into the tool result
  rather than failing — the model is expected to self-correct. `check_loop` keeps a short trail of
  page keys (URL + `layout()`): landing back on a page from a few actions ago isn't progress for
  either detector, a repeating cycle (A→B→A→B, A→B→C→A→B→C) gets `LOOP_NOTE`, and a second cycle
  in the same task ends it as an `ask` (`stop_looping()`) instead of burning steps.
- **Element numbers are re-assigned every page view**, so actions on an element are checked before they
  run (`do_action`): the model sends `label` (the element's quoted text) with each number and
  `wrong_element()` refuses the step if [n] is something else now; later steps of a batch must
  refer to numbers that were in the view the batch was sent from (`seen`); and once Krify has
  typed/chosen in a form, `leave_guard()` refuses (then asks Allow/Block for) a link/nav click,
  `open_url`, back or reload that would leave it unsaved (`leaveCheck` in `content.js`).
- **Dropdowns**: `select_option` handles native `<select>` and custom dropdowns alike
  (`pickInCustomDropdown` in `content.js`): opens the field, finds its list (aria-controls, the open
  list, or whatever opening it added – portals too), types into its search box / scrolls a lazy list
  if needed, clicks the match and checks the field shows it. A miss returns the real option names.
  `describe()` calls a button/clickable with a down-chevron icon (or role=combobox /
  aria-haspopup=listbox) `dropdown "…"`: list filters like "My Activities" only show their current
  choice. `filter_dropdowns()` in `loop.py` uses that to hint after an empty `find_text`/`find_elements`
  and to send a "not found" answer back once (`check_filters`) if no filter was changed. A native
  `<select>` line always lists its options. A custom dropdown's options are usually only in the DOM
  while it's open, so `describe()` asks `extension/pagehook.js` – injected next to `content.js` in the
  page's own JS world (`world: "MAIN"`, `injectKrify()` in `operations.js`) – over a synchronous
  `krify:options` / `krify:options-result` event pair; it reads the owning React component's
  `options`/`items` prop (current fiber, ≤3 components up) without opening anything. Non-React sites
  fall back to the options `pickInCustomDropdown` saw when it last opened that dropdown (`seenOptions`).
  Either way the line reads `dropdown "…" (options: a, b, …, +N more)`.
- **Answer format**: the prompt and the `done` tool ask for a one-line answer, then 3+ items as
  `- item` / `1. item` lines; `formatText()` in `sidepanel.js` (escape first) renders those as lists.
- **Fact check**: before a `done`, `check_facts()` rejects the summary once if it contains a 3+ digit
  number that never appeared in anything read this task (`self.seen`); a second rejection is allowed
  through with a caveat appended.
- **Safety is enforced in code, not just prompted**: `agent/safety.py#check()` inspects the action and
  the *element's own label text* (never arbitrary page content) to decide if it's risky (money,
  sending, deleting, account actions, private-data fields) and forces an Allow/Block round trip via
  `Agent.ask_permission()`. Google Sheets/Docs edits get an additional overwrite guard
  (`cells_in_the_way`) that downloads the sheet and reports exactly which cells would be clobbered
  before typing.

### Adding a new agent operation

Three places need to agree: `agent/tools.py` (the JSON-schema tool definition sent to the LLM),
`agent/loop.py` (how the server-side loop handles/describes that tool name — see `describe()` and the
big dispatch in `run()`), and `extension/sidepanel/operations.js` (the actual browser implementation,
registered in the `operations` object and looked up by `runOperation()`). Risky new actions should
also be taught to `agent/safety.py#check()`.

### Model routing (`backend/app/llm/client.py`, `backend/app/config.py`)

`config.py` lists all known models with a `tier` (`strong` vs `backup`) and whether they support
`vision`; a provider is silently dropped if its API key env var isn't set. `ask_ai()` prefers the
caller's chosen model, falls back through other `strong` models, waits briefly for a model that's
only short-term rate-limited, and only reaches for `backup` models once strong ones are out for a
while. `_blocked_until`/`_blocked_why` (in-memory, per-process) track cooldowns learned from
provider errors; `llm/usage.py` persists daily token/request counts and learned daily limits to the
database so the side panel's model picker can show "64% used" next to each model.

### Google Sheets (read path vs. edit path)

Sheets' grid is a canvas, not readable DOM text, so reading goes: extension downloads the open tab as
CSV via authenticated `fetch` (`sidepanel/sheets.js`) → server loads it into an in-memory DuckDB table
`sheet` with file/network access disabled (`backend/app/sheets.py`) → the model writes a `sheet_query`
SQL `SELECT`, or `read_cells` for exact values → only the answer rows (capped at `MAX_CHARS`) go back
to the model, never the whole sheet. `sheets.py` also adds a synthetic `_row` column and reports each
SQL result column's real sheet letter, so the model can turn a query result into a cell address
without counting columns.

Editing a Sheet is a different, real-input path (see below): `go_to_cell` + `type_here`, checked
before and after.

### Real keyboard/mouse via `chrome.debugger` (`extension/sidepanel/realinput.js`)

JS-synthesized key/mouse events are flagged "untrusted" and ignored by Google Sheets/Docs. For those
origins (`needsRealInput()`), and for anything needing right-click/double-click/drag, the extension
attaches `chrome.debugger` to *only Krify's own tab* and dispatches real `Input.dispatchKeyEvent` /
`Input.dispatchMouseEvent` CDP commands (Chrome shows the yellow "started debugging" bar while
attached). This is detached automatically when a task ends (`setWorking(false)` → `detachAll()`), and
a user clicking "Cancel" on the yellow bar permanently blocks real input for the rest of that task
(`cancelled` flag in `realinput.js`).

### Content script as "eyes" (`extension/content.js`)

Injected on demand into the active page. Walks the DOM for a fixed `SELECTOR` list of interactive
elements, filters to what's currently visible/on-screen, assigns each a stable `[n]` number via
`data-krify-id`, and returns a compact text description (`describe()`), capped at `MAX_ELEMENTS`/
`MAX_TEXT`/`MAX_SCREEN_TEXT` to keep requests small for free-tier token limits. Open popups/menus are
listed first so they don't get pushed past the element cap on busy pages. Besides `SELECTOR`,
`pointerTargets()` adds React elements that only show a pointer cursor, and table rows that open a record
when clicked (`isClickableRow()`: own pointer cursor or a tabindex, not a header row, first cell not
already a link). Such a row is one `row link "cell | cell | …"` element. Its own buttons/links stay listed,
a click on it aims at a cell with nothing clickable in it (`rowSpots()`), and `safety.py` doesn't read
its cell text as a risky label. The same script also owns
the shadow-DOM overlay used for the visible cursor, the "Krify is working" glowing border, and the
element-number labels drawn on screenshots.

**Structured page view (default, `STRUCTURED_VIEW=1`).** Instead of the flat list, each step the
extension's `get_page_model` returns the *whole* page as JSON (`buildPageModel()` in `content.js`):
sections (dialog, header, nav, main › heading, form, footer, embedded pages), cards (repeated items
such as product tiles or table rows, with title/price/rating/badges) and elements (with accessibility
states such as disabled/required/invalid/expanded, `aria-describedby` hints, table column). On-screen
elements are collected first, then the rest, up to a cap and a time limit. `backend/app/agent/pageview.py`
decides what the AI sees: restricted mode shows what's on screen plus any open dialog and the focused form,
with long nav/sidebar lists cut and the footer collapsed. Full mode (the side panel's Unrestricted switch)
shows everything and marks off-screen parts. `View.flat` keeps every collected element in the old
`[n] role "label"` format, because `safety.py`, the fact check and `merge_elements` depend on it, so they
always see the whole page. `find_elements`/`inspect` repeat the same numbering (`numbering` in `content.js`)
so their ids match the view. To try the renderer without a browser:
`uv run python -m app.agent.pageview fixtures/amazon_search.json [--full] [--flat]`.

The page view (flat or structured) is the only page data sent automatically every step. Plain text (paragraphs, a
table with nothing clickable) otherwise needs `read_text`. In Unrestricted mode (structured view, not on Sheets),
`look()` adds the whole page's `read_text` to the view under `[Page text]`, uncut, and it is trimmed along with the
view. When it doesn't show which
element goes with which (e.g. several identical "Add to cart" buttons in a list), the model can call
the `inspect` tool: an indented accessibility-tree view (`getAccessibilityTree()`) built from the same
numbered elements plus, with `filter="all"`, non-interactive structural context (headings, regions,
nearby text). `ref=<element number>` zooms into that element's surrounding card/section
(`containerOf()`) instead of walking the whole page. Past `MAX_TREE_CHARS` it returns an error asking
for a smaller `depth` or a narrower `ref` rather than silently truncating.

### Krify only touches its own tabs

`operations.js` tracks `krifyTabs`/`agentTabId`/`openedByKrify` per side-panel session: a task starts
in whatever tab is currently active, new tabs opened by Krify (or `target="_blank"` links followed
from its tab) are added to the set, and `close_tab` refuses to close a tab Krify didn't open itself.

### Database (`backend/app/db.py`)

Single SQLite file `backend/krify.db` (gitignored, created automatically) via SQLModel: `Task`/`Step`
tables back task history (`history.py`), `Usage`/`Limit` tables back per-model-per-day quota tracking
(`llm/usage.py`, which also one-time-imports a legacy `usage.json` if present). Swapping to Postgres
later is meant to be just changing `DATABASE_URL`.
