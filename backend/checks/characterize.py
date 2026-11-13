"""G3: pin the pure helpers' behaviour – `name(args) -> result` for a fixed table of inputs, plus
every constant the agent loop uses. Names are looked up wherever they live (checks/_find.py), so
the output must be identical before and after code moves between modules."""

import json
import re

from checks._find import find, mod


def fmt(value):
    if isinstance(value, re.Pattern):
        return f"re({value.pattern!r}, flags={int(value.flags)})"
    if isinstance(value, (set, frozenset)):
        return "{" + ", ".join(sorted(fmt(v) for v in value)) + "}"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{fmt(k)}: {fmt(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        inner = ", ".join(fmt(v) for v in value)
        return f"[{inner}]" if isinstance(value, list) else f"({inner})"
    if callable(value) and getattr(value, "__name__", "") == "<lambda>":
        return "<lambda>"
    if isinstance(value, type):
        return value.__name__
    return repr(value)


def show(name, *args, fn=None, **kwargs):
    func = fn or find(name)
    try:
        result = func(*args, **kwargs)
        if hasattr(result, "__next__"):
            result = list(result)
        out = fmt(result)
    except Exception as err:  # the exception itself is behaviour too
        out = f"raised {type(err).__name__}: {err}"
    shown = ", ".join([fmt(a) for a in args] + [f"{k}={fmt(v)}" for k, v in kwargs.items()])
    print(f"{name}({shown}) -> {out}")


# ---------- constants (moved verbatim: values must not change) ----------
CONSTANTS = """READ_OPS SHEET_OPS SHEET_ONLY_TOOLS OLD_PAGE OLD_SCREEN MAX_READ_SITES SHEET_HINT PAGE_TEXT ACTION_OPS
MAX_BATCH BATCH_LIMIT PAGE_MOVED STEP_FAILED OVERWRITE_BLOCKED BLOCKED DONE_TOO_EARLY PLAN_NOT_APPROVED _ELEMENT_KEY
SUBMIT_WORDS FORM_ERROR FORM_NOT_SENT NOT_IN_VIEW CHANGED_IN_BATCH BATCH_REFUSED WRONG_ELEMENT LEAVE_FORM ELEMENT_FIELDS
_CLAIMS _FIRST_PERSON _TYPE_OPS _CLICK_OPS CLAIM_REJECTED HANDOFF USER_ONLY HANDOFF_REJECTED FILTER_HINT FILTER_CHECK
NOT_FOUND _DROPDOWN_LINE _KIND_WORDS _ELEMENT_ID _STATUS_LINE _ELEMENT_LINE PROGRESS_OPS IDLE_LIMIT
MAX_IDLE_SCROLLS MAX_SCROLLS_PER_PAGE SCROLL_REFUSED WRAP_UP_AT WRAP_UP REPEAT_NOTE LOOP_TRAIL REQUIRED_EMPTY FILL_ANY
ASK_ONCE DUMMY LOOP_NOTE LOOP_ASK NEW_SITE SAME_READ WRONG_PRODUCT PRODUCT_REJECTED WRONG_PAGE SAME_PLAN STUCK_NOTE
UNNOTED_READ _NUMBER _TOKEN _SAFE_OPS _PHRASE PHRASE_STOPWORDS NUDGE""".split()
for name in CONSTANTS:
    print(f"{name} = {fmt(find(name))}")

# ---------- output: clean_text_reply ----------
for text in [
    'done({"summary": "Found it: ₹699"})',
    'ask_user({"question": "Which colour?"})',
    '{"summary": "bare object"}',
    '```json\n{"summary": "fenced"}\n```',
    '{"done": {"summary": "nested"}}',
    '{"name": "done", "arguments": "{\\"summary\\": \\"args as string\\"}"}',
    '{"name": "ask_user", "parameters": {"question": "params?"}}',
    '{"summary": "missing brace',
    '{"question": "esc \\"q\\" \\u20b9 here", "x": 1',
    '{"summary": "bad escape \\x"',
    "Just some free text.",
    "",
    None,
    "   ",
    "{not json at all",
    '{"other": 1}',
    'done(  {"summary": "spaces"}  )',
]:
    show("clean_text_reply", text)

# ---------- verification: facts ----------
SEEN = ["Logitech M185 Wireless Mouse ₹69,900 (M.R.P ₹99,999)", "12 pages, 20 rows each, 17 on the last page",
        "Year 2024. Total 1,200.50 items. Ratings 4.3"]
for text in ["It costs ₹69,900.", "It costs 69900", "900 left", "₹99,999 and 12345", "2024, 2025.", "1,200.50 total",
             "There are 257 users (12 × 20 + 17 = 257).", "12 × 20 + 17 = 257 users total.", "257 users", "Price 99 only",
             "(5 * 7 = 35) and 350", None]:
    show("unverified_numbers", text, SEEN)
    show("unexplained_numbers", text, SEEN)
for raw, text in [("257", "There are 257 users (12 × 20 + 17 = 257)."), ("257", "12 × 20 + 17 = 257 users total."),
                  ("257", "There are 257 users (12 × 21 + 5 = 257)."), ("240", "12 pages 20 rows = 240"),
                  ("1,200", "(1,200.50 - 0.50 = 1,200)"), ("5", "(10 / 2 = 5)")]:
    show("explained_by_calculation", raw, text, SEEN)
for expr in ["2**10", "5/0", "-3 + 4", "12 * 20 + 17", "(1,200.5 - 0.5)", "abs(3)", "", "1 2", "3 × 4", "7 // 2", "1e3"]:
    show("_safe_eval", expr)
for chunk in ["12 pages × 20 rows + 17 on the last page = 257", "no numbers here", "a 1,234.5 b (2) c"]:
    show("_tokens", chunk)
show("_candidate_expressions", "There are 257 users (12 × 20 + 17 = 257). Also 3 + 4 = 7.\nNext line 9")
show("_candidate_expressions", None)
show("joined_pages", ["₹69,900", "1,2,3", "a,b"])
for number, pages in [("900", "69900"), ("69900", "price 69900."), ("4.3", "rated 4.3"), ("3", "4.3"), ("12", "x12y")]:
    show("is_seen", number, pages)
for task, text in [("price of iPhone 16e 256GB", "iPhone 16 costs ₹69,900"), ("price of iPhone 16e 256GB", "Apple-iPhone-16e 256 GB"),
                   ("s24 ultra", "Galaxy S24 Ultra 12GB"), ("no model here", "anything")]:
    show("missing_words", task, text)
    show("missing_models", task, text)
show("missing_models", "iPhone 16e", "no price in this note")
show("missing_models", "iPhone 16e", "costs $5")
for text in ['He bought "Logitech M185" and "Unicorn Pro Max"', "Visit Amazon India Store today", "The Add Cart Page",
             "Logitech Wireless Mouse", "lower case only", None]:
    show("unverified_phrases", text, SEEN)

# ---------- verification: claims ----------
ACTIONS = [("type", {"element": 3, "text": "9381234567"}, "Typed."), ("click", {"element": 4}, "Clicked."),
           ("type", {"element": 5, "text": "hello", "submit": True}, "Typed.")]
for text in ["I have entered the number 9381234567.", "I have entered the number 9999999999.", "I've just typed \"hello\".",
             "I typed \"bye there\".", "I clicked Save. I have now successfully submitted it!", "Amazon has sent an OTP.",
             "I entered it and I clicked it.", "", None]:
    show("unbacked_claims", text, ACTIONS)
    show("unbacked_claims", text, [])
show("unbacked_claims", "I submitted the form.", [("type", {"element": 1, "text": "x"}, "Typed.")])
show("unbacked_claims", "I submitted the form.", [("type", {"element": 1, "text": "x", "submit": True}, "Typed.")])
for q in ["Please sign in to your account first.", "Could you click the Save button?", "May I proceed with the payment?",
          "Please enter the OTP you received.", "Which one do you prefer?", "Should I go ahead and submit?",
          "What is your password? Please sign in.", "Tell me the colour."]:
    print(f"HANDOFF.search({q!r}) -> {bool(find('HANDOFF').search(q))}; USER_ONLY -> {bool(find('USER_ONLY').search(q))}")

# ---------- verification: filters / changes ----------
for s in ["There are no tasks for Geetha.", "None found", "Found 3 items", "I couldn't find it", "zero results", "Nothing"]:
    print(f"NOT_FOUND.search({s!r}) -> {bool(find('NOT_FOUND').search(s))}")
print("_DROPDOWN_LINE ->", find("_DROPDOWN_LINE").findall('[2] select "Search in" = "All"\n[16] dropdown "My Activities"\n[3] link "x"'))
changes = mod("app.verification.changes", "app.agent.changes")
BEFORE = ("https://a.test/list", '[1] link "Home"\n[2] button "Filters"\n[3] link "Item A"\n[4] link "Item B"')
for after, clicked, title, open_list in [
    (("https://a.test/item/a", '[1] link "Home"\n[9] button "Edit"'), 'link "Item A"', "Item A", None),
    (("https://a.test/list", '[1] link "Home"\n[2] button "Filters"\n[3] link "Item A"\n[4] link "Item B"\n[5] option "Red"'), 'button "Filters"', "", None),
    (("https://a.test/list", '[1] link "Home"\n[2] button "Filters"'), 'button "Collapse"', "", None),
    (("https://a.test/list", BEFORE[1]), 'button "Filters"', "", None),
    (("https://a.test/list", '[1] link "Home"\n[2] dropdown "Red"'), 'option "Red"', "", ("Colour", ["Red", "Blue"])),
    (("https://a.test/list", '[1] link "Home"\n[2] dropdown "Select a colour"\n[5] option "Red"'), 'option "Red"', "", ("Colour", ["Red", "Blue"])),
]:
    show("click_result", BEFORE, after, clicked, title, open_list, fn=changes.click_result)
for task, clicked in [("open agents", 'link "Agents"'), ("open agents and create one", 'link "Agents"'),
                      ("go to the billing settings page", 'link "Billing settings"'), ("open agents", 'link "People"')]:
    show("opened_what_asked", task, clicked, fn=changes.opened_what_asked)
ui_state = mod("app.verification.ui_state", "app.agent.ui_state")
for status in [
    "",  # nothing showing
    "Your order is eligible for FREE Delivery. Choose FREE Delivery option at checkout. | Page 1 of 4 (below the screen)",
    "Learn to Code", "3 results found", "Your changes have been saved", "Added to cart",
    "Enter a valid email address", "Form error – Contact is required | Saved draft", "Something went wrong",
    "Invalid password", "Your session has expired. Please sign in again.", "Payment failed – try another card",
    "Your order is eligible for FREE Delivery. | Payment failed – try another card",
    "That item can’t be added", "Password is required", "Se ha producido un error", "पासवर्ड गलत है",
]:
    show("blocking_status", status, fn=ui_state.blocking_status)

# ---------- guardrails: element matching / forms / safety ----------
for label, names in [("Create Opportunity", ['link "Opportunities"']), ("Opportunities", ['link "Opportunities"']),
                     ("Save button", ['button "Save"']), ("", ['button "Save"']), ("the button", ['button "Save"']),
                     ("Opportunity Titles", ['input text "Opportunity Title"']), ("Created date field here", ['input "Create date"']),
                     ("Select a contact", ['clickable "Select a contact ▾"', '"Contact" ""']), ("Sav", ['button "Save"'])]:
    show("label_matches", label, names)
for word in ["Opportunity Titles", "Save button", "creates"]:
    show("_words_of", word)
for a, b in [("opportunity", "opportunitie"), ("create", "created"), ("cat", "car"), ("save", "save")]:
    show("_same_word", a, b)
for name, args, label, status in [("click", {}, "Save Activity", "Form error – Contact is required | Saved draft"),
                                  ("click", {}, "Cancel", "Form error – x"), ("press_key", {"key": "Enter"}, "", "Form error – Email"),
                                  ("type", {"submit": True}, "", "Form error – A | Form error – B"), ("click", {}, "Save", ""),
                                  ("type", {}, "", "Form error – x")]:
    show("form_not_sent", name, args, label, status)
safety = mod("app.guardrails.safety", "app.agent.safety")
ELEMENTS = ('[1] button "Buy Now"\n[2] input password "Password" (empty)\n[3] input text "Message"\n[4] row link "Order | Deleted | ₹5"\n'
            '[5] button "Delete"\n[6] input text "Search"\n[7] input text "Card number"\n[8] input text "Name Box"')
SHEET = "https://docs.google.com/spreadsheets/d/abc/edit"
for name, args, last, url in [
    ("click", {"element": 1}, "", ""), ("click", {"element": 4}, "", ""), ("click", {"element": 5}, "", ""),
    ("select_option", {"element": 4, "option": "Delete"}, "", ""),
    ("type", {"element": 2, "text": "x"}, "", ""), ("type", {"element": 7, "text": "4111"}, "", ""),
    ("type", {"element": 3, "text": "hi", "submit": True}, "", ""), ("type", {"element": 6, "text": "q", "submit": True}, "", ""),
    ("type", {"element": 6, "text": "q", "submit": True, "_enter": {"inForm": True, "submitter": 'button "Place order"'}}, "", ""),
    ("press_key", {"key": "Enter"}, 'input text "Message"', ""), ("press_key", {"key": "Enter", "_enter": {"inForm": True, "password": True}}, "", ""),
    ("press_key", {"key": "Delete"}, "", SHEET), ("press_key", {"key": "ctrl + x"}, "", SHEET), ("type_here", {"text": "x"}, "", SHEET),
    ("type", {"element": 8, "text": "A1"}, "", SHEET), ("drag", {}, "", SHEET), ("scroll", {}, "", SHEET), ("hover", {"element": 1}, "", ""),
]:
    show("safety.check", name, args, ELEMENTS, last, url, fn=safety.check)
for steps, task in [(["Click Contacts"], "navigate to contact page"), (["Open amazon", "Search mouse"], "price of a mouse"),
                    (["a", "b", "c"], "x"), (["Open the chat", "Send hai"], "talk to sofi"), ([], "send an email"),
                    (["Open the site"], "sign in to my account"), (["Open the site"], "who is the CEO"),
                    (["Click Sender list"], "open senders page")]:
    show("safety.plan_needs_approval", steps, task, fn=getattr(safety, "plan_needs_approval", None) or (lambda *a: "n/a"))
show("safety.element_line", ELEMENTS, 4, fn=safety.element_line)
show("safety.visible_text", 'input submit "Go" (value: "Go")', fn=safety.visible_text)

# ---------- context: element list helpers ----------
FLAT = '[1] link "Home"\n[2] button "Add to cart"\n[3] button "Add to cart"\n[4] input text "Email" (value: "x")\n[Status] Saved'
for eid in [2, "4", None, "x", 99]:
    show("element_label", FLAT, eid)
for line in ['input text "Email" (value: "x")', "no quotes", "", None]:
    show("_line_key", line)
show("layout", FLAT)
show("layout", None)
show("merge_elements", FLAT, '[3] button "Add to cart" (near Logitech)\n[9] link "New"\nnot a line')
show("merge_elements", FLAT, "")
show("ambiguity_hint", FLAT)
show("ambiguity_hint", '[1] link "Home"')
for url in ["https://www.amazon.in/s?k=x", "http://shop.example:8080/a", "chrome://newtab", "", None, "http://[bad"]:
    show("site_of", url)
for url in [SHEET, "https://docs.google.com/document/d/x", None]:
    show("is_sheet", url)
for args in [{"element": 0}, {"to_element": 3}, {"cell": "A1"}, {"element": None}]:
    show("uses_element", args)
show("preview", "  lots   of\n space  " + "x" * 200)
show("preview", None)

# ---------- tools: describe ----------
for name, args in [
    ("open_url", {"url": "https://a.test"}), ("click", {"element": 3}), ("click", {"cell": "B2", "count": 2}),
    ("click", {"element": 3, "button": "right"}), ("drag", {"from_element": 1, "to_element": 4}),
    ("hover", {"element": 7}), ("type", {"element": 2, "text": "hi", "submit": True}), ("type", {"element": 2, "text": "hi"}),
    ("press_key", {"key": "Tab", "repeat": 3}), ("press_key", {"key": "Enter", "repeat": 1}), ("press_key", {"key": "Esc"}),
    ("type_here", {"text": "x"}), ("go_to_cell", {"cell": "A1"}), ("read_cells", {"cells": "A1:B2"}),
    ("scroll", {"direction": "down"}), ("scroll", {"direction": "up", "element": 5}), ("select_option", {"element": 4, "option": "Red"}),
    ("new_tab", {"url": "https://b.test"}), ("switch_tab", {"tab": 2}), ("close_tab", {"tab": 2}), ("go_back", {}), ("reload", {}),
    ("wait", {"seconds": 2}), ("get_page_info", {}), ("get_elements", {}), ("find_elements", {"query": "cart"}),
    ("find_text", {"query": "price"}), ("read_text", {"scope": "page"}), ("read_text", {}), ("inspect", {"ref": 4}),
    ("inspect", {}), ("sheet_query", {"sql": "SELECT 1"}), ("note", {"text": "x"}),
]:
    show("describe", name, args)

# ---------- context: pageview helpers ----------
pageview = mod("app.context.pageview", "app.agent.pageview")
with open("fixtures/add_activity_form.json", encoding="utf-8") as f:
    form_view = pageview.render(json.load(f))
show("pageview.missing_required", form_view.form, fn=pageview.missing_required)
show("pageview.dupes_warning", [("Add to cart", ["2", "3"])], fn=pageview.dupes_warning)
print("form_view.form ->", fmt(form_view.form))
print("form_view.open_list ->", fmt(form_view.open_list), "full ->", form_view.full, "status ->", repr(form_view.status))

# ---------- tools: sheets ----------
sheets = mod("app.tools.sheets", "app.sheets")
CSV ='Name,Age,Fee\nRahul,21,"1,200"\nAsha,22,15%\nRavi,23,900\n'
show("sheets.summary", CSV, "Students", "Sheet1", fn=sheets.summary)
for ref in ["A1:C2", "B3", "Z9", "bad", "A2:B4"]:
    show("sheets.cells", CSV, ref, fn=sheets.cells)
for sql in ["SELECT Name, Fee FROM sheet ORDER BY Age DESC", "SELECT count(*) FROM sheet", "DROP TABLE sheet",
            "SELECT * FROM read_csv('x')", "SELECT Name FROM sheet ORDER BY Age LIMIT 1"]:
    show("sheets.query", CSV, sql, fn=sheets.query)
show("sheets.first_empty_row", CSV, fn=sheets.first_empty_row)
show("sheets.filled_cells", CSV, "A3", "X\tY", fn=sheets.filled_cells)
show("sheets.filled_cells", CSV, "A9", "X\tY", fn=sheets.filled_cells)

# ---------- context: router (the cheap-model gate) ----------
router = mod("app.context.router", "app.context.router")
print("GATE_PROMPT ->", fmt(router.GATE_PROMPT))
print("router limits ->", fmt([router.GATE_TIMEOUT, router.MAX_REPLY, router.TAIL_CHARS, router.CHEAP_MODELS]))
for text in [
    '{"kind": "chat", "reply": "Hi!"}',
    '```json\n{"kind": "answer", "reply": "391"}\n```',
    'Sure: {"kind": "Answer", "reply": " 391 "} done',
    '{"kind": "task", "reply": ""}',
    '{"kind": "task", "reply": "ignored text"}',
    '{"kind": "chat", "reply": ""}',
    '{"kind": "chat"}',
    '{"kind": "weird", "reply": "hmm"}',
    '{"kind": "chat", "reply": 5}',
    '{"kind": "chat", "reply": "' + "x" * 1201 + '"}',
    '{"kind": "chat", "reply": "{\\"name\\": \\"done\\"}"}',
    '{"kind": "chat", "reply": "```code```"}',
    '["chat"]', "not json at all", "", None,
]:
    show("router.parse", text, fn=router.parse)
show("router.tail_of", "hi", "hello", fn=router.tail_of)
show("router.tail_of", "q" * 400, "a" * 50, fn=router.tail_of)

# ---------- prompt topics: which extra rule blocks a task / a page calls for ----------
prompt = mod("app.context.prompt")
for task in ["search for wireless mouse", "cheapest iphone 16 on amazon", "go to the cart", "open the contact page",
             "what is the price of ₹5", "buy shoes", "order status", "fill the form", "", None]:
    show("task_topics", task, fn=prompt.task_topics)
for view in ['[1] link "Logitech M185" ₹699', "total $ 25", "price: 12", "no prices here", "€5 off", "", None]:
    show("page_topics", view, fn=prompt.page_topics)
