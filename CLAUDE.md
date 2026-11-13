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
server. For changes that must NOT change behaviour (refactors, moving code between layers) there is a
deterministic safety net, no LLM/browser/network needed:
```bash
uv run python checks/run.py --record   # on known-good code first: records checks/golden/ (gitignored)
uv run python checks/run.py            # after the change: everything must be identical
```
It runs static checks (compile, `ruff --select F`, every module importable on its own, empty layer
`__init__.py`s, every `self.X`/`state.X` a real attribute), the pageview CLI on every fixture + the
system prompt + `TOOLS` (golden text), the pure helpers on a fixed input table (`checks/characterize.py`),
and `checks/replay.py`: real `Agent.run()` with scripted model replies and a fake browser built on
`fixtures/`, recording every side-panel event, browser call, AI request and state field.

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

### Backend layers (`backend/app/`)

The agent is split by what each part does; extension files mirror the same names where they apply.

| Folder | What it holds |
|---|---|
| `agent/` | `loop.py`: `Agent` (`__init__`, `run()`'s dispatch, `stop_looping`); `state.py`: `TaskState` |
| `context/` | what the AI sees: `prompt.py`, `pageview.py`, `understand.py` (planner), `router.py` (cheap-model gate), `view.py` (`look`/`page_view`/`make_plan`), `header.py`, `element_list.py`, `sites.py` |
| `tools/` | `schemas.py` (`TOOLS`), `ops.py` (op-name sets, batch rules), `describe.py`, `actions.py` (`do_action`, `run_batch`, sheet reads), `sheets.py` (CSV → DuckDB) |
| `guardrails/` | before a step runs: `safety.py`, `element_match.py` (`wrong_element`), `forms.py` (`required_empty`, `track_form_edit`, `form_not_sent`), `permission.py` (`ask_permission`, `leave_guard`, `cells_in_the_way`), `progress.py` (stuck/repeat/loop detection, scroll limits) |
| `verification/` | before an answer goes out: `facts.py`, `claims.py` (claims + handoff), `filters.py`, `ui_state.py`, `changes.py` (did a click work) |
| `memory/` | `transcript.py`: token trimming of the chat (`add_with_page`, `add_screen_read`, `reset`, …) |
| `output/` | `answer.py` (`finish`, `end_task`), `text_reply.py` (`clean_text_reply`) |

`main.py`, `config.py`, `db.py`, `history.py` (task history for the side panel, never fed to the model) and
`llm/` sit outside the layers.

### LLM report (`backend/app/llm/report.py`)

Every real request to an AI model – including rate-limit/broken-reply retries and switches to another
model – is saved as an `LlmCall` row by `_try_model` in `llm/client.py`: the exact messages sent
(any image data redacted to `[image ~N KB]`), tool names, the reply (text + tool calls), the provider's
prompt/completion/total/reasoning token counts, duration and status. Which task/step a call belongs to
comes from a `ContextVar` (`report.set_task` in `Agent.run()`, `set_step` per loop step), so `ask_ai`'s
signature must stay `(messages, tools, preferred, on_note, boost)` (`checks/replay.py`
fakes it). `task_report()`/`task_report_markdown()` feed the side panel's **📊 Report** view
(`llm_report` message; per-task button in History; .md/.json download). Full request/response text is
kept for the newest 50 tasks (`report.prune`), numbers forever; deleting a task deletes its calls. The report's **🗑 Clear** button (`llm_report_clear` → `report.clear`) deletes
this install's recorded calls only (History rows and their saved totals stay; the running task keeps its calls).
`checks/llm_report_check.py` (run by `checks/run.py`) asserts the numbers, redaction and ownership.

How the pieces fit:
- **`TaskState`** (`agent/state.py`) holds everything about the task in progress and the page it's on
  (`task_text`, `plan`, `notes`, `seen`, `actions`, the `*_check_failed` flags, `elements`, `form`, …).
  `Agent.state` is created once and reset **in place** by `state.start(task, continuing)` at the start of
  each `run()` – never replaced, because a message answering `ask_user` continues the same task (goal,
  plan, notes, `seen`, `actions` kept). Connection-level things stay on `Agent`: callbacks (`browser`,
  `ui`, `confirm`, `plan_review`), the chat (`messages`, `page_views`, `screen_reads`),
  `sheet_tools`, `dev_mode`, `trace`, `tokens`, `task_id`, `model`, `last_task`, `last_status`.
- **Sync checks are plain functions over the state**: `check_facts(state, summary)`,
  `wrong_element(state, n, label)`, `check_progress(state, name, changed)`, … – testable without an Agent.
- **Methods that need the browser, the user or the chat are mixins** (`ToolsMixin`, `GuardrailsMixin`,
  `ContextMixin`, `MemoryMixin`, `OutputMixin`), combined in `class Agent(...)`; they call each other through
  `self`. Method names must be unique across them (checked by `checks/static.py`).
- **Imports point one way**: layers never import `app.agent.loop`; layer `__init__.py` files stay empty
  (a re-export can create an import cycle); `loop.py` must not `from app import tools` (`run()` has a
  local `tools`).

### The agent loop (`backend/app/agent/loop.py`)

One `Agent` instance per side-panel connection (so follow-up tasks share chat history). `Agent.run()`
is the core loop: build a page view → call `ask_ai()` with the running `messages` list and `TOOLS` →
the model replies with one or more tool calls → each call is dispatched (browser action, `sheet_query`,
`plan`, `note`, `done`, `ask_user`, …) → the result (plus a fresh page view) is appended back into
`messages` → repeat, up to `MAX_STEPS` (`config.py`, default 25).

Before any of that, `run()` calls `try_gate()` (`output/answer.py`, logic in `context/router.py`) for every
new message (not one answering `ask_user`): one tool-less request to a cheap model (`ROUTER_MODEL`, default
`groq:openai/gpt-oss-20b`, else `gemini-flash-lite`) that replies `chat` / `answer` / `task`. `chat` and `answer`
(greetings, "who are you", timeless general knowledge) are answered by the cheap model itself and the task ends
there – no page view, planner or agent call; `task` hands the unchanged message to the loop. It is fail-open:
no cheap model/key, cooling down, error, 8 s timeout, unreadable, empty or >1200-char reply all mean `task`.
Gate calls are labelled `router` in the LLM report. `ROUTER=0` turns it off. `checks/replay.py` turns it off
unless a scenario scripts `gates=[...]` (`.env` may hold real keys).

Key mechanics worth knowing before touching the loop (they live in the layer modules named):
- **Batching**: the model can send several `ACTION_OPS` tool calls in one reply (or use the explicit
  `batch` tool, `run_batch` in `tools/actions.py`), up to `MAX_BATCH` (5). The loop stops a batch early if
  the page's *layout* changed (`layout()` strips typed values before comparing) and a later queued step
  references an element number — because numbers may have shifted.
- **Token trimming** (`memory/transcript.py`): only the newest page view / screen read is
  kept in full in `messages`; older ones are replaced with a short placeholder (`OLD_PAGE`, `OLD_SCREEN`). `state.notes` (from the `note` tool) are what survive long-term and get
  re-injected via `header()` (`context/header.py`) on every page view.
- **Stuck/repeat detection** (`guardrails/progress.py`): `check_progress` (5 non-progress steps → nudge) and `check_repeat`
  (same op+args 3× in 6 steps with no page change → nudge) inject warning text into the tool result
  rather than failing — the model is expected to self-correct. `check_loop` keeps a short trail of
  page keys (URL + `layout()`): landing back on a page from a few actions ago isn't progress for
  either detector, a repeating cycle (A→B→A→B, A→B→C→A→B→C) gets `LOOP_NOTE`, and a second cycle
  in the same task ends it as an `ask` (`stop_looping()`) instead of burning steps.
- **Element numbers are re-assigned every page view**, so actions on an element are checked before they
  run (`do_action` in `tools/actions.py`): the model sends `label` (the element's quoted text) with each
  number and `wrong_element()` (`guardrails/element_match.py`) refuses the step if [n] is something else
  now; later steps of a batch must refer to numbers that were in the view the batch was sent from
  (`seen`); and once Krify has typed/chosen in a form, `leave_guard()` (`guardrails/permission.py`)
  refuses (then asks Allow/Block for) a link/nav click, `open_url`, back or reload that would leave it
  unsaved (`leaveCheck` in `content.js`).
- **Dropdowns**: `select_option` handles native `<select>` and custom dropdowns alike
  (`pickInCustomDropdown` in `content.js`): opens the field, finds its list (aria-controls, the open
  list, or whatever opening it added – portals too), types into its search box / scrolls a lazy list
  if needed, clicks the match and checks the field shows it. A miss returns the real option names.
  `describe()` calls a button/clickable with a down-chevron icon (or role=combobox /
  aria-haspopup=listbox) `dropdown "…"`: list filters like "My Activities" only show their current
  choice. `filter_dropdowns()` in `verification/filters.py` uses that to hint after an empty
  `find_text`/`find_elements` and to send a "not found" answer back once (`check_filters`) if no filter
  was changed. A native `<select>` line always lists its options. A custom dropdown's options are usually
  only in the DOM while it's open, so `describe()` asks `extension/context/pagehook.js` – injected next to
  `content.js` in the page's own JS world (`world: "MAIN"`, `injectKrify()` in `extension/tools/operations.js`) – over a synchronous
  `krify:options` / `krify:options-result` event pair; it reads the owning React component's
  `options`/`items` prop (current fiber, ≤3 components up) without opening anything. Non-React sites
  fall back to the options `pickInCustomDropdown` saw when it last opened that dropdown (`seenOptions`).
  Either way the line reads `dropdown "…" (options: a, b, …, +N more)`.
- **Answer format**: the prompt and the `done` tool ask for a one-line answer, then 3+ items as
  `- item` / `1. item` lines; `formatText()` in `sidepanel.js` (escape first) renders those as lists.
- **Plan approval**: the planner (`make_plan`) always writes a plan, but the user only has to approve it when
  `plan_needs_approval()` (`guardrails/safety.py`) says so: 3+ steps, or the task/steps mention something with side
  effects (send/post/delete/pay/sign in – `RISKY_WORDS` – or message/edit/update/add/save/fill/type…). A 1–2 step
  look-up or "go to this page" plan is shown but pre-approved (`state.plan_approved = True`), so the agent acts at
  once; the agent's own `plan` call gets the same rule. Until a plan is approved, actions are refused with
  `PLAN_NOT_APPROVED`, which tells the model to call `plan`; `SAME_PLAN` ("you already have this plan") only applies
  to an approved plan, so re-sending the planner's steps still reaches the approval prompt.
- **Fact check**: before a `done`, `check_facts()` (`verification/facts.py`) rejects the summary once if
  it contains a 3+ digit number that never appeared in anything read this task (`state.seen`); a second
  rejection is allowed through with a caveat appended.
- **Known gap (kept as-is, pinned by `checks/replay.py`)**: a plain-text `done({...})` reply runs
  ui_state → filters → facts but not `check_claims`, and plain-text replies skip the plan-approval gate;
  the `done` tool runs ui_state → claims → filters → facts. Each check rejects only once, so their order
  matters.
- **Safety is enforced in code, not just prompted**: `guardrails/safety.py#check()` inspects the action and
  the *element's own label text* (never arbitrary page content) to decide if it's risky (money,
  sending, deleting, account actions, private-data fields) and forces an Allow/Block round trip via
  `ask_permission()` (`guardrails/permission.py`). Google Sheets/Docs edits get an additional overwrite guard
  (`cells_in_the_way`) that downloads the sheet and reports exactly which cells would be clobbered
  before typing.

### Adding a new agent operation

Three places need to agree: `tools/schemas.py` (the JSON-schema tool definition sent to the LLM),
the server side (how the loop handles that tool name — the big dispatch in `run()` in `agent/loop.py`,
`ACTION_OPS`/`READ_OPS` in `tools/ops.py`, and its side-panel text in `tools/describe.py`), and
`extension/tools/operations.js` (the actual browser implementation, registered in the `operations`
object and looked up by `runOperation()`). Risky new actions should also be taught to
`guardrails/safety.py#check()`.

### Model routing (`backend/app/llm/client.py`, `backend/app/config.py`)

`config.py` lists all known models with a `tier` (`strong` vs `backup`); a provider is silently dropped if its API key env var isn't set. `ask_ai()` prefers the
caller's chosen model, falls back through other `strong` models, waits briefly for a model that's
only short-term rate-limited, and only reaches for `backup` models once strong ones are out for a
while. `_blocked_until`/`_blocked_why` (in-memory, per-process) track cooldowns learned from
provider errors; `llm/usage.py` persists daily token/request counts and learned daily limits to the
database so the side panel's model picker can show "64% used" next to each model.

### Google Sheets (read path vs. edit path)

Sheets' grid is a canvas, not readable DOM text, so reading goes: extension downloads the open tab as
CSV via authenticated `fetch` (`extension/tools/sheets.js`) → server loads it into an in-memory DuckDB table
`sheet` with file/network access disabled (`backend/app/tools/sheets.py`) → the model writes a `sheet_query`
SQL `SELECT`, or `read_cells` for exact values → only the answer rows (capped at `MAX_CHARS`) go back
to the model, never the whole sheet. `sheets.py` also adds a synthetic `_row` column and reports each
SQL result column's real sheet letter, so the model can turn a query result into a cell address
without counting columns.

Editing a Sheet is a different, real-input path (see below): `go_to_cell` + `type_here`, checked
before and after.

### Real keyboard/mouse via `chrome.debugger` (`extension/tools/realinput.js`)

JS-synthesized key/mouse events are flagged "untrusted" and ignored by Google Sheets/Docs. For those
origins (`needsRealInput()`), and for anything needing right-click/double-click/drag, the extension
attaches `chrome.debugger` to *only Krify's own tab* and dispatches real `Input.dispatchKeyEvent` /
`Input.dispatchMouseEvent` CDP commands (Chrome shows the yellow "started debugging" bar while
attached). This is detached automatically when a task ends (`setWorking(false)` → `detachAll()`), and
a user clicking "Cancel" on the yellow bar permanently blocks real input for the rest of that task
(`cancelled` flag in `realinput.js`).

### Content script as "eyes" (`extension/context/content.js`)

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
the shadow-DOM overlay used for the visible cursor and the "Krify is working" glowing border.

**Structured page view (default, `STRUCTURED_VIEW=1`).** Instead of the flat list, each step the
extension's `get_page_model` returns the *whole* page as JSON (`buildPageModel()` in `content.js`):
sections (dialog, header, nav, main › heading, form, footer, embedded pages), cards (repeated items
such as product tiles or table rows, with title/price/rating/badges) and elements (with accessibility
states such as disabled/required/invalid/expanded, `aria-describedby` hints, table column). On-screen
elements are collected first, then the rest, up to a cap and a time limit. `backend/app/context/pageview.py`
decides what the AI sees, in design order: open pop-ups first (🔲; behind a modal the page is one line per
part), then the page's parts as laid out (┌ TOP BAR, NAV, LEFT SIDEBAR/SIDEBAR, MAIN, RIGHT COLUMN, EMBEDDED PAGE,
FOOTER – `_region()`, from landmarks plus each section's `box` and the page `width` sent by content.js), a
`⚠ ALERTS` line for the site's own messages, and every element in one shape, `[n] role "label" = "value" (states)`
(`_tidy()`/`_states()`); `clean_page_text()` drops what the view already says from Unrestricted mode's
`[Page text]`. Restricted mode shows what's on screen plus any open dialog and the focused form,
with long nav/sidebar lists cut and the footer collapsed. Full mode (the side panel's Unrestricted switch)
shows everything and marks off-screen parts. `View.flat` keeps every collected element in the old
`[n] role "label"` format, because `safety.py`, the fact check and `merge_elements` depend on it, so they
always see the whole page. `find_elements`/`inspect` repeat the same numbering (`numbering` in `content.js`)
so their ids match the view. To try the renderer without a browser:
`uv run python -m app.context.pageview fixtures/amazon_search.json [--full] [--flat]`.

The page view (flat or structured) is the only page data sent automatically every step. Plain text (paragraphs, a
table with nothing clickable) otherwise needs `read_text`. In Unrestricted mode (structured view, not on Sheets),
`look()` (`context/view.py`) adds the whole page's `read_text` to the view under `[Page text]`, uncut, and it is trimmed along with the
view. When it doesn't show which
element goes with which (e.g. several identical "Add to cart" buttons in a list), the model can call
the `inspect` tool: an indented accessibility-tree view (`getAccessibilityTree()`) built from the same
numbered elements plus, with `filter="all"`, non-interactive structural context (headings, regions,
nearby text). `ref=<element number>` zooms into that element's surrounding card/section
(`containerOf()`) instead of walking the whole page. Past `MAX_TREE_CHARS` it returns an error asking
for a smaller `depth` or a narrower `ref` rather than silently truncating.

### Krify only touches its own tabs

`extension/tools/operations.js` tracks `krifyTabs`/`agentTabId`/`openedByKrify` per side-panel session: a task starts
in whatever tab is currently active, new tabs opened by Krify (or `target="_blank"` links followed
from its tab) are added to the set, and `close_tab` refuses to close a tab Krify didn't open itself.

### Database (`backend/app/db.py`)

Single SQLite file `backend/krify.db` (gitignored, created automatically) via SQLModel: `Task`/`Step`
tables back task history (`history.py`), `Usage`/`Limit` tables back per-model-per-day quota tracking
(`llm/usage.py`, which also one-time-imports a legacy `usage.json` if present). Swapping to Postgres
later is meant to be just changing `DATABASE_URL`.
