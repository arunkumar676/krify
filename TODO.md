# TODO

## 0. Setup
- [x] Get Groq API key
- [x] Install Python + uv
- [x] Create `backend/` and `extension/` folders

## 1. Understand natural language
- [ ] Connect to Groq
- [ ] Sentence in → operation out
- [ ] Test: different wordings, vague requests, follow-ups

## 2. See the page
- [x] `get_page_info` – URL + title
- [x] `get_elements` – numbered buttons, links, inputs
- [x] `read_text` – page text

## 3. Navigate
- [x] `open_url`
- [x] `go_back`
- [x] `reload`

## 4. Interact
- [x] `click`
- [x] `type`
- [x] `press_key`
- [x] `scroll`
- [x] `select_option`

## 5. Control
- [x] `wait`
- [x] `done`
- [x] `ask_user`

## 6. Connect all
- [x] Extension ↔ backend
- [x] Agent loop
- [x] Side panel: task box + step log

## 7. Test
- [x] Google search
- [x] Google search → open first result
- [x] Wikipedia question
- [x] Fill a form
- [x] Sort by price

## 8. Extras
- [x] Visible cursor (on/off switch)
- [x] Safety prompts: Allow / Block before risky actions
- [x] Model picker + auto-switch when a free limit runs out
- [x] Stop works instantly
- [x] Fewer tokens per task

- [x] More AI providers: Gemini + OpenRouter (Cerebras skipped: needs a card)
- [x] Glowing border while working
- [x] Tabs
- [x] Read visible screen + notes
- [x] Usage per model in the picker
- [x] Task history (SQLite database)

## 9. Reliability (now)
- [x] Stop extra clicks after the task is done
- [x] Stop wandering on long tasks (plan, scroll limit, wrap-up warning)
- [x] iPhone 16 128GB price compare (amazon.in vs flipkart.com) works – prices match the sites
- [x] Fact check: numbers in answers must come from pages Krify read
- [x] Fewer tokens on long tasks (price compare: 94k → 43k)

## Later
- [x] Hover (works on Amazon "Account & Lists"; CSS-only menus may need the real mouse)
- [x] Screenshots (with element numbers drawn on; auto-switch to a model that can see images)

## 10. Quick wins (no new permissions)
- [x] Read Google Sheets, private too (download as CSV → DuckDB answers the AI's SQL; ties kept)
- [ ] Zoom into a small part of the screen
- [ ] Find an element by description ("the search bar")
- [x] Batch: several actions in one AI call (httpbin form: 7 → 4 calls, 14.1k → 6.7k tokens)
- [ ] File upload
- [ ] Website tables (`<table>` → DuckDB → SQL, e.g. Wikipedia lists)

## 11. Real keyboard and mouse (`chrome.debugger`)
- [x] Turn on the debugger permission (Chrome shows a yellow bar; only when needed)
- [x] Real keys and shortcuts (Tab, Enter, Ctrl+Z, Ctrl+C/V, Ctrl+End)
- [x] Drag and drop (both kinds; tested on the-internet drag_and_drop)
- [x] Right, double, triple click (Sheets cells by address: `click(cell="B22")`)
- [x] Click by position (x, y) from a screenshot (spots outside the picture are refused)

## 12. Google Sheets editing
- [x] Jump to a cell with the Name box (`go_to_cell`, checked before typing)
- [x] Type a row: value + Tab (one batch, 8.6k tokens; overwrite guard asks before replacing data)
- [x] Check the cells (`read_cells`: formula bar or after Google saves), undo with Ctrl+Z
- [x] Ask Allow / Block before editing a sheet (once per task)
- [ ] Apps Script for big jobs (user clicks Allow)

## 13. Safety
- [ ] Ask before downloads, accepting terms, changing settings
- [ ] Never: passwords, card numbers, CAPTCHAs, money transfers, permanent deletes
- [ ] Ignore instructions written inside web pages

## 14. Extras (later)
- [ ] Run JavaScript in the page
- [ ] Read console errors and network requests
- [ ] Record a GIF
- [ ] Resize the window (phone layout)

## 15. New data to feed the AI

### About the page
- [ ] **What changed**: a short before/after note after each action ("popup opened", "URL changed")
- [ ] **Element state**: checked/unchecked, open/closed, disabled, selected (`[22] checkbox (checked)`)
- [ ] **What's covering the page**: "A popup is open on top. Close it first."
- [ ] **Loading status**: "Page still loading" or "spinner visible", so the AI waits instead of clicking too early
- [ ] **Error messages on the page**: red text like "Invalid email" or "Field required", shown as a separate item
- [ ] **Scroll position**: "You're at 30% of the page, more below", so it knows whether to scroll
- [ ] **Form summary**: every field, what's filled and what's empty (`Name: filled, Email: empty (required)`)

### About the browser
- [ ] **Open tabs**: titles of the tabs Krify is using, so it can switch on its own
- [ ] **Downloads**: "file report.csv finished downloading"
- [ ] **Network errors**: "Page failed to load (404)" or "No internet"
- [ ] **Console errors**: a broken script on the site that explains why a click does nothing

### About you and the task
- [ ] **Your preferences**: "prefer cheapest", "use my work email", "Amazon.in, not .com"
- [ ] **Today's date and time**: for tasks like "book for tomorrow"
- [ ] **Clipboard text**: what you copied, only if you allow it
- [ ] **Your selection**: text you highlighted on the page ("summarize this")

### About past experience
- [ ] **Site tips**: lessons learned on each site ("Asana: close the welcome popup first")
- [ ] **Past successful tasks**: "Last time, 'open Agents' worked by clicking [15] and then [20]."
- [ ] **Past failures**: "Last time, clicking 'Agents' did nothing, so use a real click."

### My top 5 picks
1. **What changed**: the AI knows whether its action worked
2. **What's covering the page**: stops clicks hitting a popup instead of the button
3. **Loading status**: stops the AI acting before the page is ready
4. **Error messages**: lets it fix form mistakes on its own
5. **Site tips**: Krify gets better on each site over time
