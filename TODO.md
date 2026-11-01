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
- [ ] Hover
- [ ] Screenshots
