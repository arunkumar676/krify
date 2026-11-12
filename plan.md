I didn't change any code. Besides the code, I analysed the real session log in [test.md](test.md): 46 tasks, 213 agent calls and about **5.0M input tokens**, averaging 23k per call. Three numbers from it change how I'd rank your list:

| Finding from the log | What it means for your list |
|---|---|
| Requests grew from about **12.4k tokens** (first 5 calls) to about **40k** (last 20 calls). Late in the session, "open google.com" cost 60k tokens. | Carried chat history is the biggest token cost, so **#10 is your top lever** |
| The planner used **18.7k tokens in total**, about 0.4% of the session | **#2 saves almost nothing** as stated |
| About **10 tasks** ended with "plan not approved". In one, you typed "approved" 5 times and were still blocked. | A **bug that isn't on your list**, and it's the first thing to fix (see "What you missed") |

## Overview

| # | Idea | Value | Effort | Risk | Verdict |
|---|---|---|---|---|---|
| 1 | Send only what changed | High | High | Medium–high | Do, but only after element numbers are stable |
| 2 | Cheaper planner model | Low | Low | Medium | Change it to "plan only when needed" |
| 3 | pytest suite | High (makes the rest safe) | Low–medium | Low | Do early |
| 4 | Retry in code | Medium | Low–medium | Medium | Do, with strict conditions |
| 5 | Prompt rules → code checks | High | Medium | Medium | Do; start with problems the log proves |
| 6 | Layered backend | Medium (long term) | High | High before a demo | After the demo, except two small pieces |
| 7 | Header / footer / side nav | Medium | Medium | Medium–high | Merge into #1 and #9 |
| 8 | Tools by need | Low–medium | Low | Low if decided by state | Decide from state, not from the prompt |
| 9 | AI View structure | High | Medium–high | Medium | Drive it from real failure cases |
| 10 | Context memory | **Highest** | Low–medium | Medium | Do first among the token work |

---

## 1. Send what changed after an action

**Already in the code:** [loop.py:978](backend/app/agent/loop.py#L978) sends "[Page unchanged since the last view.]" when the page is exactly the same. In the log those steps add only about 60 tokens. [changes.py](backend/app/agent/changes.py) `click_result()` already works out what appeared and disappeared after a click. What's missing is the **partial change** case: a dropdown opens or a toast appears, and the model gets the full view (about 7k tokens on the CRM) again.

**Advantages**
- Large saving per step on forms and filter flows, where each action changes a small part of the page.
- The model sees the effect of its action directly, so it judges "did it work?" better. Your own TODO.md lists "What changed" as the #1 pick.

**Disadvantages**
- **Numbers aren't stable.** `numberElements()` clears and reassigns every number on every view ([content.js:906](extension/content.js#L906)). One new toast at the top shifts every number below it, so a "diff" can be nearly the whole page, or worse, wrong.
- **It conflicts with trimming.** `add_with_page()` keeps only the newest page view in full. If the newest view is a diff, the full view it builds on gets replaced with `OLD_PAGE`.
- **Small models struggle to combine a base view and a diff.** Mistakes are caught by `wrong_element()`, but every catch costs a step.

**Things to consider**
- **Make numbering stable first:** a node keeps its number while it exists, and only new nodes get new numbers. Don't ship diffs without this.
- **Send a full view when:** the URL changes, a dialog opens or closes, more than about 30% of the lines changed, after a scroll, after a batch stops early, and every N steps.
- **Trimming must treat "full view + following diffs" as one unit.**
- **The server always keeps the full flat list** (`self.elements`, `self.seen`) for safety, the fact check and `wrong_element`. Only what the model sees changes.
- **Possible format:** `+ [41] option "Sudha"` / `− [7] button "Apply"` / `~ [12] dropdown "All Assignees" → "Sudha"`. Reuse the `Counter` diff in `changes.py`.

## 2. Use a cheaper model for planning

**Evidence:** 23 planner calls, each about 700 tokens in and 150 out, about 1 s each, 0.4% of the session.

**Advantages**
- Uses one fewer request per task from the strong model's daily request limit.
- The first plan appears slightly faster.
- Easy to build.

**Disadvantages**
- The token saving is almost nothing.
- **Plan quality affects the whole task**, and the user approves the plan. A weaker plan means more edits and more steps, which costs far more than the planner call saved. Casual and Hinglish requests are where small models are weakest.
- `ask_ai()` only reaches backup models once strong ones are out, so this needs a new "prefer backup" path in [client.py](backend/app/llm/client.py).

**Things to consider: reframe it as "plan only when needed"**
- The only rule today is `MIN_WORDS = 4` ([understand.py:16](backend/app/agent/understand.py#L16)). "open google.com and search for krify" got a **one-step plan that needed approval**, which then hit the deadlock.
- Skip planning, or auto-approve, when a plan has 2 steps or fewer and none of the risky words `safety.py` already checks for.
- If you still switch models: put a `PLANNER_MODEL` setting in `config.py` and fall back to the task's model when the JSON doesn't parse. Today a parse failure silently means no plan.

## 3. Small pytest suite for the pure functions

**Advantages**
- A safety net for #1, #4, #5 and #6, which all change loop behaviour.
- Runs in seconds with no browser and no API keys.
- Documents what the tricky patterns are meant to match (`SUBMIT_WORDS`, `HANDOFF`, `USER_ONLY`, `NOT_FOUND`).

**Disadvantages**
- **The bugs in the log are in `Agent.run()`'s orchestration**, not in pure functions, so pure-function tests won't catch them.
- `pageview.render` reference outputs need updating every time #9 changes the format on purpose.
- `content.js` (2,771 lines) stays untested.

**Things to consider**
- **Importing `app` changes your real database.** [db.py:60-75](backend/app/db.py#L60-L75) creates tables and alters `krify.db` at import, and `usage.py` imports `usage.json` at import. Set `DATABASE_URL` to a temporary SQLite file in `conftest.py` before any import.
- `uv add --dev pytest pytest-asyncio`.
- **Add scenario tests for `run()` as well:** a fake browser plus a scripted fake `ask_ai` that returns fixed tool calls. For example: "an action before plan approval opens the review", "a plain-text done goes through `check_claims`". These catch orchestration bugs.
- **Test the DuckDB sandbox in `sheets.py`.** Check that `read_csv('/etc/passwd')`, `COPY` and `ATTACH` are refused. That's security-critical.
- **Use real-page fixtures.** The 4 fixtures render to 150–450 tokens; the real CRM page was about 7k. Save real page models (CRM lists, the lookup page, Amazon) to use for #1, #7 and #9.

## 4. Retry in code instead of telling the model to retry

**Advantages**
- Saves a full LLM round trip (15–40k input tokens in this log) every time a click silently does nothing.
- Works the same on every model, and removes a prompt rule.

**Disadvantages**
- **Risk of doing an action twice.** "Nothing changed" is a guess. If a network update arrives after the settle wait (2.5 s at most), the page looks unchanged, and a retry could add a second item to the cart, submit twice, or toggle something back.
- **`real=true` attaches `chrome.debugger`**, which shows the yellow debugging bar. If the user clicks Cancel on it, real input is blocked for the rest of the task.
- Unless it's reported, the retry is invisible in traces, which makes debugging harder.

**Things to consider**
- **Retry only when all of these hold:** the page is identical, `click_result()` is empty, the URL is the same, and the element is still there with the same label.
- **Never retry on:** submit or risky labels (`SUBMIT_WORDS`, any `safety.check` category), toggles (checkbox, switch, `aria-pressed` / `aria-expanded`), or anything that needed Allow/Block.
- Wait one more settle period before deciding.
- Say it in the tool result: "Clicked (retried with the real mouse: the normal click did nothing)".
- Remember for the rest of the task that a site needs real clicks.
- **Other rules that could move to code the same way:** the hover `real=true` retry, synonym retries for `find_elements`, and pressing submit on search boxes.

## 5. Move prompt rules into code checks

**Evidence:**
- 5 of 15 `ask_user` questions were "Shall I…? / Do you want me to…?" for safe steps, even though the prompt forbids it.
- 4 of 46 answers were empty or garbled. [test.md:79](test.md#L79) is a `done` made only of zero-width spaces.

**Advantages**
- Reliable on weak models, and the system prompt (about 2.4k tokens per request) gets shorter.
- Easy to measure: count how often each check fires.
- The pattern is already proven in your code: `check_facts`, `check_claims`, `wrong_element`, `leave_guard`.

**Disadvantages**
- **Every rejection costs another LLM request** (15–40k tokens here). A check with false positives can cost more than it saves.
- Several checks firing one after another can trap the model in a loop.
- Regex checks are language-sensitive (Hinglish) and can misfire.
- Rules spread through the code are harder to see than a prompt.

**Things to consider**
- **Start with what the log proves:**
  - Reject permission questions for steps that aren't risky.
  - Reject empty or invisible-character answers (strip U+200B and similar first).
  - Reject `ask_user` questions asking for plan approval, and open the plan review instead.
- **Run the same checks on both reply paths.** Today the plain-text path skips some ([loop.py:1097-1121](backend/app/agent/loop.py#L1097-L1121)):
  - A plain-text "done" skips `check_claims`.
  - A plain-text question skips `check_handoff` and `check_claims`.
  - Plain text also bypasses the plan gate.
- Keep the "reject once, let the second try through with a caveat" approach, and add a per-task cap on total rejections.
- Remove a prompt rule only after its code check has proven itself.

## 6. Layered backend

**Advantages**
- `loop.py` (1,709 lines, about 40 per-task fields) gets clear ownership, and new teammates can find where the guardrails live.
- **An explicit, ordered list of checks would fix a real bug structurally:** the two reply paths run different checks (see #5).
- Each layer can be tested on its own (#3).

**Disadvantages**
- A big diff with no behaviour gain by itself. It can break the order of checks, and it will conflict with #1, #4 and #5 if done in parallel.
- **The layers share `Agent` state.** New folders without splitting that state just add indirection and risk circular imports.
- Five folders may be too much structure for about 3,600 lines.
- CLAUDE.md, `try_nlu.py` and the `python -m app.agent.pageview` command all need updating.

**Things to consider**
- **Do these two pieces now, before the demo:**
  - A `TaskState` dataclass, so one line resets all per-task state.
  - A guard interface, `check(state, call) -> rejection | None`, run from one ordered list.
- **Possible mapping:**
  - **context/:** prompt, pageview, header, planner
  - **tools/:** schemas, plus one handler per tool, replacing the long `if/elif` chain in `run()`; also `sheets.py`
  - **guardrails/:** safety, `wrong_element`, `leave_guard`, required fields, overwrite guard, plan gate
  - **verification/:** the fact, claims, filters, UI-state and handoff checks; `changes.py`
  - **memory/:** notes, trimming, history compaction
  - **Persistence** (`db.py`, `history.py`, `usage.py`) stays separate from memory.
- Make the move with no behaviour change, after the tests exist, and after the demo.

## 7. Separate header, footer and side nav; send only when needed

**Already in the code:** nav is capped at 8 elements, sidebar at 6, and the footer is collapsed ([pageview.py:21-23](backend/app/agent/pageview.py#L21-L23)). The header has no cap.

**Advantages**
- On app-style pages (the CRM with its sidebar), the nav repeats on every step and is rarely used, so the saving is steady.
- Less noise around the main content.

**Disadvantages**
- **Sections are found only through landmarks** (`<header>`, `role=banner`, `<nav>`, `<footer>`; [content.js:1064-1066](extension/content.js#L1064-L1066)). Many React admin apps use only `<div>`s, so they get no saving, or the wrong part gets hidden.
- **The header holds the most-used controls:** the search box (Amazon, Google), cart and account. Hiding them breaks the most common task.
- **Navigation tasks need the nav.** In the log: "click Opportunities in the sidebar". One extra "show nav" request costs more than the nav lines saved.
- Cached sections have the same stale-number problem as #1.

**Things to consider**
- Build it inside #1 and #9 as a one-line summary, e.g. `## Nav (unchanged: Opportunities, Contacts, …; use find_elements)`. Names without numbers are enough for the model to know they're there.
- **Always keep from the header:** search inputs, forms, current or selected items, and badges.
- **Expand automatically** when the task or a plan step names a nav item.
- **For sites without landmarks:** treat a fixed top bar as the header and a fixed left column of links as the sidebar.

## 8. Send only the tools the task needs

**Measured:** all tools are about 2.7k tokens per request. The Sheets-only tools are about 220, and they're already hidden until a Sheet opens.

**Advantages**
- About 0.8–1.2k tokens saved per request if rarely used tools are hidden.
- Fewer wrong tool picks by small models.

**Disadvantages**
- Hiding a tool the model needs leads to stuck or repeat loops.
- A tool list that changes every step defeats provider prompt caching, because tools sit at the start of the request.
- **Prompt rules and rejection messages name tools** ("use find_elements", "screenshot"), so the model can be told to use something it no longer has.
- The `batch` tool's list of allowed ops includes `go_to_cell` and `type_here`, so it must stay in sync.

**Things to consider: decide from state, not from the prompt**
- `click_at` / drag by coordinates: only after a screenshot exists.
- `screenshot`: only when `has_vision_model()` is true. Today it's listed and then answers "not available".
- `switch_tab` / `close_tab`: only when more than one tab is open.
- Sheets tools and the Sheets prompt lines (about 200 tokens): only on a Sheet. Also fix the sticky `self.sheet_tools` ([loop.py:1072](backend/app/agent/loop.py#L1072)): once true, it stays true for the whole session.
- Check that each provider accepts a history containing calls to tools that aren't in the current list.

## 9. Clearer AI View structure

**Advantages**
- Fewer misreads means fewer steps, which saves more than any trimming.
- It builds on a structured view that already works.

**Disadvantages**
- Open-ended: it's easy to spend days on it with no measurable gain.
- Every new marker costs tokens on every step, plus a line in `PAGE_FORMAT`.
- **What helps GPT-OSS 120B may confuse the backup models**, and the switch to a backup model happens automatically in the middle of a task.
- `View.flat` must keep its format, because the safety check, the fact check and `changes.py` parse it with regexes.

**Things to consider**
- **Start from the expensive failures in the log:**
  - The RocketReach / Find Company tasks: 367k, 347k and 450k tokens. They ended with "not found" or hit the 25-step limit.
  - The "Find a Company" tab was there, and you had to point it out.
- **Ideas:**
  - Show tab and filter states clearly, e.g. `tab "Find a Company" (not selected)`.
  - Write table column names once.
  - Show pagination: "page 1 of 19, 20 per page, 373 total".
  - Mark elements that are new since the last action.
- **Keep the indented text format, not JSON.** JSON braces and quotes cost tokens.
- Update `PAGE_FORMAT` in the same commit as each format change.

## 10. Context memory optimisation

**Evidence:**
- Follow-up tasks keep every earlier message; only page views, screen reads and screenshots are shortened.
- Average requests went from 12.4k to 40k tokens over the session.
- **Rough estimate:** if requests had stayed near the first task's size, the session would have used about 40% fewer input tokens. The exact figure depends on the pages visited.
- You typed "please ignore all previous context, start a fresh session". A **New chat** button already exists ([sidepanel.js:376](extension/sidepanel/sidepanel.js#L376)), but nothing points users to it.

**Advantages**
- The largest saving available in long sessions.
- **Stops old failures from repeating.** Earlier "plan not approved" exchanges stayed in the history, and the model kept repeating that failure in new tasks.
- Fewer "request too large" model switches on free tiers.

**Disadvantages**
- **Follow-ups need some earlier context.** "now check Krishna Reddy" depends on the previous task.
- LLM summaries cost a call and can drop numbers the fact check relies on.
- **Editing the message list can break requests.** Every assistant tool call must keep its tool result; `close_open_calls()` exists for exactly this reason.

**Things to consider**
- **Compact without an LLM call.** When a new (not continuing) task starts, replace each earlier task's messages with one line: `Earlier: <task> → <status>: <summary>`. That data already exists in `finish()` and the `Task` table.
- Keep full messages only for the current task, or the task being continued after `ask_user`.
- **Add a token budget before each request** (estimate as chars ÷ 4). Over the limit, drop the oldest large tool results first: `inspect` trees, `find_elements` output, `sheet_query` results.
- Keep `self.seen` on the server for the fact check, even after compaction.
- Suggest **New chat** in the UI once a chat gets long.

---

## What you missed

**A. The plan-approval deadlock. Fix this first: it's a bug, and it will show in a demo.**
- **What happens:** the planner sets `self.plan`, but the review UI opens only when the model calls the `plan` tool ([loop.py:1189](backend/app/agent/loop.py#L1189)).
- When the model acts directly instead, it gets `PLAN_NOT_APPROVED`: "waiting for the user to approve your plan" ([loop.py:49](backend/app/agent/loop.py#L49)). But nothing is actually waiting, because no review was ever shown.
- The model then gives up in plain text (that path has no plan gate) or asks "Do you approve?". Your typed "approved" never sets `plan_approved`. See [test.md:604-616](test.md#L604-L616) and [test.md:880-919](test.md#L880-L919).
- **Fix ideas:**
  - When an action hits the gate, open the review from code.
  - Auto-approve plans of 1–2 safe steps.
  - Treat a typed approval as approval while a plan is pending.

**B. Answer validation.** Reject `done` or `ask_user` text that is empty or made of invisible characters (4 of 46 tasks in the log).

**C. One check pipeline for both reply paths** (tool call and plain text). Part of #5 and #6.

**D. A baseline you can compare against.** test.md already is one: 213 calls, 5.0M tokens, 23.3k average per call. Add a per-request split (system, tools, page, history), then replay the same task script after each change.

**E. A navigation guard.** Still open from earlier: `open_url` and `new_tab` aren't checked by [safety.py:47](backend/app/agent/safety.py#L47), so a page could steer the model to an outside URL carrying the user's data.

**F. A feature flag per change** (`DIFF_VIEW`, `COMPACT_HISTORY`, `AUTO_RETRY`…), like the existing `STRUCTURED_VIEW`, so you can switch any of them off during the demo.

**G. An extension/backend version check.** #1, #7 and #9 change both sides. A PC on the LAN with an old extension would break without any error, so send a protocol version when connecting.

**H. Keep CLAUDE.md and `PAGE_FORMAT` up to date** as these land.

## Suggested order

1. **A + B + C** (bug fixes, small, the biggest demo win). Fold #2 in here as "plan only when needed".
2. **#3 tests** (including scenario tests for A) and **D** (measurement).
3. **#10** history compaction: the biggest token win for a small change.
4. **#5 + #4**: rules into code, safe retries.
5. **#8**: tools and prompt sections by state.
6. **#1** with stable numbering, then **#7 + #9** on top. These are one workstream with one owner, because all three change what the model sees.
7. **#6** restructure after the demo. Only `TaskState` and the check pipeline come earlier.

I can update the proposal page with this version if you want.