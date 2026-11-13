"""G4: replay the real agent loop deterministically – scripted model replies, a fake browser built on
the page-model fixtures, scripted Allow/Block and plan-review answers – and print everything the loop
does: side-panel events, browser calls, what each AI request contained (tools, flags, a hash of the
whole transcript plus its newest messages), the final transcript and every Agent/TaskState field.

The output must be byte-identical before and after a refactor. Run via checks/run.py (it sets
PYTHONHASHSEED=0, DATABASE_URL=sqlite:// and runs once per STRUCTURED_VIEW value).
REPLAY_VERBOSE=1 prints the whole transcript at every AI request (for finding a difference)."""

import asyncio
import copy
import dataclasses
import hashlib
import itertools
import json
import os
import sys

from openai.types.chat import ChatCompletionMessage

import app.llm.client as client

VERBOSE = os.getenv("REPLAY_VERBOSE") == "1"
SCENARIO = None  # the Scenario being run


def norm(value):
    """JSON-safe and order-stable (sets sorted, tuples as lists)."""
    if isinstance(value, dict):
        return {str(k): norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [norm(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((norm(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True, default=str))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def dump(value):
    return json.dumps(norm(value), sort_keys=True, ensure_ascii=False)


def emit(kind, data=None):
    print(f"[{SCENARIO.name}] {kind}: {dump(data)}")


# ---------- fake model ----------

def text(content):
    return {"msg": ChatCompletionMessage(role="assistant", content=content)}


def empty():
    return text("")


def call(name, **args):
    return (name, json.dumps(args))


def raw(name, arguments):
    """A tool call whose arguments aren't valid JSON."""
    return (name, arguments)


def reply(*calls, content=None, model=None):
    ids = SCENARIO.ids
    tool_calls = [{"id": f"call_{next(ids)}", "type": "function", "function": {"name": n, "arguments": a}}
                  for n, a in calls]
    msg = ChatCompletionMessage.model_validate({"role": "assistant", "content": content, "tool_calls": tool_calls})
    return {"msg": msg, "model": model}


def tool(name, **args):
    return reply(call(name, **args))


async def fake_ask_ai(messages, tools, preferred, on_note=None, boost=False):
    sc = SCENARIO
    if tools is None and messages[0]["content"] == router.GATE_PROMPT:  # the cheap-model gate (router.py)
        emit("gate", {"model": preferred, "user": messages[1]["content"], "messages": len(messages)})
        if not sc.gates:
            raise RuntimeError("replay: the script ran out of gate replies")
        item = sc.gates.pop(0)
        if isinstance(item, Exception):
            raise item
        return ChatCompletionMessage(role="assistant", content=item), preferred, 20
    if tools is None:  # the planner (understand.py)
        emit("planner", {"model": preferred, "messages": messages})
        return ChatCompletionMessage(role="assistant", content=sc.plans.pop(0) if sc.plans else ""), preferred, 7
    whole = dump(messages)
    info = {"model": preferred, "tools": [t["function"]["name"] for t in tools],
            "boost": boost, "count": len(messages), "hash": hashlib.sha256(whole.encode()).hexdigest()[:16]}
    if VERBOSE:
        info["messages"] = messages
    else:
        info["newest"] = messages[sc.seen_messages:] if 0 <= sc.seen_messages < len(messages) else messages[-2:]
    sc.seen_messages = len(messages)
    emit("ask_ai", info)
    if not sc.replies:
        raise RuntimeError("replay: the script ran out of replies")
    item = sc.replies.pop(0)
    return item["msg"], item.get("model") or preferred, 100


def fake_skip_model(model, seconds=60):
    emit("skip_model", [model, seconds])


# Patch before anything imports these names (loop.py / understand.py bind them at import time).
client.ask_ai = fake_ask_ai
client.skip_model = fake_skip_model

from checks._find import mod  # noqa: E402

from app.agent.loop import Agent  # noqa: E402
from app.context import router  # noqa: E402

# The gate is off unless a scenario scripts its replies (gates=[...]): .env may hold real API keys.
router.cheap_model = lambda: "test:cheap" if SCENARIO.gates is not None else None

pageview = mod("app.context.pageview", "app.agent.pageview")
safety = mod("app.guardrails.safety", "app.agent.safety")


# ---------- fake browser ----------

def fixture(name, **changes):
    with open(f"fixtures/{name}.json", encoding="utf-8") as f:
        data = json.load(f)
    if "full" in changes:
        data["full"] = changes.pop("full")
    if "status" in changes:
        data["pages"][0]["status"] = changes.pop("status")
    return data


# A live region that is only information (task 309 on Amazon's cart): it must not bounce a finished task.
BANNER = "Your order is eligible for FREE Delivery. Choose FREE Delivery option at checkout. | Page 1 of 4 (below the screen)"
CSV = 'Name,Age,Fee\nRahul,21,"1,200"\nAsha,22,15%\nRavi,23,900\n'
SHOP_TEXT = ("Results for wireless mouse. Logitech M185 Wireless Mouse ₹699. HP X200 Wireless Mouse ₹549. "
             "Portronics Toad 23 ₹399. Apple iPhone 16e 256GB ₹69,900.")


def pages():
    return {
        "shop": {"url": "https://www.amazon.in/s?k=wireless+mouse", "title": "Amazon.in : wireless mouse",
                 "model": fixture("amazon_search"), "text": SHOP_TEXT, "tabs": "1. Amazon.in (this tab)"},
        "fullshop": {"url": "https://www.amazon.in/s?k=wireless+mouse", "title": "Amazon.in : wireless mouse",
                     "model": fixture("amazon_search", full=True), "text": SHOP_TEXT},
        "flip": {"url": "https://www.flipkart.com/search?q=mouse", "title": "Flipkart: mouse",
                 "model": fixture("amazon_search"), "text": "Flipkart results. HP X200 ₹549."},
        "list": {"url": "https://crm.example/opportunities", "title": "Opportunities", "model": fixture("clickable_rows"),
                 "text": "Opportunities. Acme Website revamp ₹2,40,000. Hooli Search ads ₹50,000."},
        "form": {"url": "https://crm.example/activities/new", "title": "Add Activity", "model": fixture("add_activity_form"),
                 "text": "Add Activity. Subject, Contact, Channel."},
        "formerr": {"url": "https://crm.example/activities/new", "title": "Add Activity",
                    "model": fixture("add_activity_form", status="Form error – Contact is required | Form error – Channel is required"),
                    "text": "Add Activity. Contact is required."},
        "login": {"url": "https://id.example.com/login", "title": "Welcome back", "model": fixture("login_dialog"),
                  "text": "Welcome back. Sign in. Enter a valid email address."},
        "cart": {"url": "https://www.amazon.in/gp/cart/view.html", "title": "Amazon.in Shopping Cart",
                 "model": fixture("amazon_search", status=BANNER), "text": "Shopping Cart. 1 item. Subtotal ₹699."},
        "cartwarn": {"url": "https://www.amazon.in/gp/cart/view.html", "title": "Amazon.in Shopping Cart",
                     "model": fixture("amazon_search", status=f"{BANNER} | Payment failed – try another card"),
                     "text": "Shopping Cart. 1 item. Subtotal ₹699."},
        "sheet": {"url": "https://docs.google.com/spreadsheets/d/abc/edit#gid=0", "title": "Students - Google Sheets",
                  "model": fixture("clickable_rows"), "text": "", "csv": CSV},
    }


class Browser:
    def __init__(self, start, moves=None, handlers=None):
        self.pages = pages()
        self.page = self.pages[start]
        self.moves = moves or {}  # (op, element or url) -> page name
        self.handlers = handlers or {}  # op -> value or fn(browser, args); an Exception value is raised
        self.cell = "A2"
        self.counts = {}

    def go(self, name):
        self.page = self.pages[name]

    def flat(self):
        return pageview.render(copy.deepcopy(self.page["model"])).flat

    async def __call__(self, name, args):
        emit("browser", [name, args])
        self.counts[name] = self.counts.get(name, 0) + 1
        handler = self.handlers.get(name)
        value = (handler(self, args) if callable(handler) else handler) if name in self.handlers else self.default(name, args)
        if isinstance(value, Exception):
            raise value
        return value

    def default(self, name, args):
        page = self.page
        match name:
            case "get_page_info":
                return {"url": page["url"], "title": page["title"], "tabs": page.get("tabs", "")}
            case "get_page_model":
                return copy.deepcopy(page["model"])
            case "get_elements":
                return self.flat()
            case "describe_element":
                return safety.element_line(self.flat(), args.get("element"))
            case "read_text":
                return page["text"] or "(no text on screen)"
            case "find_text":
                return f'Found "{args.get("query")}" – scrolled there.' if args.get("query", "") in page["text"] \
                    else f'(no text matched "{args.get("query")}")'
            case "find_elements":
                return ('[27] button "Add to cart" (near Logitech)\n[40] button "Add to cart" (near Dell)'
                        if args.get("query") == "cart" else f'(no elements matched "{args.get("query")}")')
            case "inspect":
                return f'region "Results"\n  [{args.get("ref")}] (zoomed in)' if args.get("ref") else "main\n  heading"
            case "sheet_csv":
                return {"csv": page.get("csv", ""), "title": page["title"], "tab": "Sheet1"}
            case "sheet_active_cell":
                return {"cell": self.cell, "formula": {"A2": "Rahul", "A3": "Asha"}.get(self.cell)}
            case "go_to_cell":
                self.cell = str(args.get("cell", "")).upper()
                return f"Selected {self.cell}."
        key = args.get("element", args.get("url"))
        if (target := self.moves.get((name, key))) is not None:
            self.go(target)
            return f"Done: {name}."
        return "OK." if name not in ("click", "type", "select_option") else f"Done: {name}."


# ---------- scenarios ----------

PLAN = '{"goal": "Do what the user asked", "steps": ["Open the site", "Find it", "Report back"], "done_when": "It is reported"}'


@dataclasses.dataclass
class Scenario:
    name: str
    start: str
    tasks: list  # task text, or a coroutine function fn(agent) for session operations
    replies: list
    plans: list = dataclasses.field(default_factory=list)
    confirms: list = dataclasses.field(default_factory=list)
    reviews: list = dataclasses.field(default_factory=list)
    moves: dict = dataclasses.field(default_factory=dict)
    handlers: dict = dataclasses.field(default_factory=dict)
    gates: list = None  # scripted replies of the cheap-model gate (None = gate off); an Exception is raised
    dev_mode: bool = False
    location: dict = None
    ids: object = None
    seen_messages: int = 0


SCENARIOS = []


def scenario(fn):
    SCENARIOS.append(fn)
    return fn


@scenario
def text_done_paths():
    # A text "done" runs ui_state → filters → facts but NOT claims (known gap, pinned here);
    # text replies also skip the plan-approval gate.
    return Scenario("text_done_paths", "login", ["check the login page status please"], plans=[PLAN], replies=[
        text("The page says hello"),
        text('done({"summary": "All good, 12345 users"})'),
        text('done({"summary": "All good, 12345 users"})'),
        text('done({"summary": "I clicked Continue and it shows Enter a valid email address"})'),
    ])


@scenario
def harmless_status():
    # A banner / pager in the page's status text is not an error: done goes out at once (no bounced call).
    return Scenario("harmless_status", "cart", ["check the cart page please"], plans=[""], replies=[
        tool("done", summary="The cart page is open and shows 1 item."),
    ])


@scenario
def problem_among_harmless_status():
    # The same banner plus a real problem: done is bounced once, quoting only the problem, then goes out.
    return Scenario("problem_among_harmless_status", "cartwarn", ["check the cart page please"], plans=[""], replies=[
        tool("done", summary="The cart page is open and shows 1 item."),
        tool("done", summary="The cart page is open and shows 1 item."),
    ])


@scenario
def done_tool_checks():
    return Scenario("done_tool_checks", "shop", ["find a pink unicorn mouse on amazon",
                                                 "now do the same on flipkart please"], plans=["sorry, no plan", PLAN], replies=[
        tool("done", summary="I typed 98765 into search and there is no pink unicorn"),
        tool("done", summary="There is no pink unicorn mouse here."),
        tool("done", summary="No pink unicorn; cheapest is 777"),
        tool("done", summary="No pink unicorn; cheapest is Portronics Toad 23 at 399"),
        # second task: has a plan that isn't approved – the done tool is gated, a text reply isn't
        tool("done", summary="Nothing on flipkart."),
        text('done({"summary": "Flipkart has none."})'),
        text('done({"summary": "Flipkart has none."})'),
    ])


@scenario
def ask_user_paths():
    return Scenario("ask_user_paths", "login", ["sign in to my account", "it is hunter2"], plans=[PLAN], reviews=[
        ("update", ["Type the email", "Type the password", "Click Continue"]), ("approve", None),
    ], replies=[
        tool("click", element=9, label="Continue"),  # gated: plan not approved
        tool("plan", steps=["Type the email", "Type the password"]),
        tool("plan", steps=["Type the email", "Type the password", "Click Continue"]),  # same plan
        tool("plan", steps=["Fill email", "Fill password", "Continue"], done_when="The account page is open"),
        tool("ask_user", question="I have entered your email, what is the password?"),
        tool("ask_user", question="Please sign in to the page and tell me when done"),
        tool("ask_user", question="What is your password?"),
        # the user's answer continues the same task
        reply(call("type", element=7, text="hunter2", label="Password"), call("click", element=9, label="Continue")),
        tool("done", summary="Signed in."),
        tool("done", summary="Signed in."),
    ])


@scenario
def plan_rejected():
    return Scenario("plan_rejected", "shop", ["compare mouse prices on two sites"], plans=[PLAN],
                    reviews=[("reject", None)], replies=[tool("plan", steps=["Open amazon", "Open flipkart", "Compare"])])


@scenario
def empty_replies():
    fail_once = {"n": 0}

    def page_info(browser, args):  # the first page view fails: "(can't read it: …)"
        fail_once["n"] += 1
        if fail_once["n"] == 1:
            return Exception("tab was closed")
        return browser.default("get_page_info", args)
    return Scenario("empty_replies", "shop", ["hi"], handlers={"get_page_info": page_info},
                    replies=[empty(), empty(), empty()])


@scenario
def garbled_text():
    return Scenario("garbled_text", "shop", ["hello there"], replies=[
        {**text("{broken json"), "model": "test:switched"},
        text("{still broken"),
    ])


@scenario
def reads_and_notes():
    return Scenario("reads_and_notes", "shop", ["price of iPhone 16e 256GB on amazon"], plans=['{"goal": "x"}'],
                    dev_mode=True, replies=[
        tool("note", text="iPhone 16 costs ₹69,900"),
        tool("note", text="iPhone 16 costs ₹69,900"),
        tool("read_text"),
        tool("read_text"),
        tool("find_text", query="Logitech"),
        tool("find_text", query="zzz"),
        tool("find_elements", query="cart"),
        tool("find_elements", query="zzz"),
        tool("inspect", ref=2),
        tool("inspect", ref=26),
        tool("inspect"),
        tool("get_elements"),
        tool("sheet_query", sql="SELECT 1"),
        tool("get_page_info"),
        tool("done", summary="Apple iPhone 16e 256GB costs ₹69,900"),
    ])


@scenario
def form_batch():
    return Scenario("form_batch", "form", ["add a dummy activity in crm"], plans=["not json"],
                    moves={("click", 16): "list"}, replies=[
        reply(call("type", element=2, text="Call Acme", label="Subject"),
              call("select_option", element=4, option="Anu", label="Select a contact"),
              call("click", element=16, label="Save Activity")),
        tool("click", element=16, label="Save Activity"),
        tool("click", element=4, label="Save Activity"),  # a stale number: [4] is a row now
        reply(call("click", element=5, label="More actions"), raw("wait", "{bad json"),
              call("done", summary="Saved.")),
        tool("done", summary="Saved the activity."),
    ])


@scenario
def leave_guard():
    return Scenario("leave_guard", "form", ["fill the activity subject please"], plans=[""],
                    handlers={"leave_check": {"form": True, "inside": False, "navigates": True}},
                    moves={("open_url", "https://example.com"): "shop"}, confirms=[False, True], replies=[
        tool("type", element=2, text="X", label="Subject"),
        tool("click", element=3, label="Add Activity"),
        tool("click", element=3, label="Add Activity"),
        tool("open_url", url="https://example.com"),
        tool("go_back"),
        tool("reload"),
        tool("open_url", url="https://example.com"),
        tool("click", element=26, label="Logitech M185 Wireless Mouse, 2.4GHz with USB Nano Receiver"),
        tool("done", summary="Left the form."),
    ])


@scenario
def batch_tool():
    def click(browser, args):
        if args.get("element") == 98:
            return Exception("boom")
        return browser.default("click", args)
    wait = {"op": "wait", "seconds": 0}
    return Scenario("batch_tool", "shop", ["add the logitech mouse to cart"], plans=[""], handlers={"click": click},
                    moves={("click", 10): "list"}, replies=[
        tool("batch", steps=[{"op": "click", "element": 27, "label": "Add to cart"}, wait, {"op": "note", "text": "x"}]),
        tool("batch", steps=[wait] * 7),
        tool("batch", steps=[]),
        tool("batch", steps=[{"op": "click", "element": 98}, wait]),
        tool("batch", steps=[{"op": "click", "element": 10, "label": "Best Sellers"},
                             {"op": "click", "element": 26, "label": "Logitech"}]),
        tool("batch", steps=[{"op": "click", "element": 4, "label": "Nope"}, wait, "not a dict"]),
        tool("batch", steps=[{"op": "scroll", "direction": "down"}]),
        tool("done", summary="Added to cart."),
    ])


@scenario
def guard_odds_and_ends():
    # product name missing from a priced answer; the wrong-element hint naming the right numbers;
    # a non-number element; a number that wasn't in the batch's view; a question that isn't a handoff.
    return Scenario("guard_odds_and_ends", "shop", ["price of iPhone 16e 256GB please"], plans=[""], replies=[
        tool("done", summary="iPhone 16 costs ₹69,900"),
        tool("click", element=26, label="Add to cart"),
        tool("click", element="abc", label="Something"),
        reply(call("click", element=26, label="Logitech M185 Wireless Mouse"), call("click", element=99, label="Ghost")),
        tool("ask_user", question="Which storage do you want?"),
    ])


@scenario
def blocked_and_skipped():
    return Scenario("blocked_and_skipped", "login", ["log me in"], confirms=[False], replies=[
        reply(call("type", element=7, text="x", label="Password"), call("click", element=9, label="Continue")),
        tool("done", summary="Could not sign in."),
        reply(call("done", summary="Could not sign in."), call("click", element=1, label="Close")),
    ])


@scenario
def sites_and_trace():
    # dev mode: the trace marks page changes; a 4th site's read drops the oldest kept read;
    # a scroll that changes the URL has no scroll record left to update.
    urls = {"flip": "https://www.flipkart.com/search?q=mouse", "list": "https://crm.example/opportunities",
            "login": "https://id.example.com/login"}
    return Scenario("sites_and_trace", "shop", ["read four sites"], dev_mode=True,
                    moves={**{("open_url", u): p for p, u in urls.items()}, ("scroll", None): "flip"}, replies=[
        tool("read_text"),
        tool("open_url", url=urls["flip"]), tool("read_text"),
        tool("open_url", url=urls["list"]), tool("read_text"),
        tool("open_url", url=urls["login"]), tool("read_text"),
        tool("scroll", direction="down"),
        tool("done", summary="Read them all."),
    ])


@scenario
def batch_calls_stop():
    # Several action calls in one reply: stopped by a failed step, by a page change, and by the 5-step limit.
    def click(browser, args):
        if args.get("element") == 98:
            return Exception("boom")
        return browser.default("click", args)
    w = call("wait", seconds=0)
    return Scenario("batch_calls_stop", "shop", ["try a few clicks at once"], plans=[""], handlers={"click": click},
                    moves={("click", 10): "list"}, replies=[
        reply(call("click", element=98, label="x"), w),
        reply(call("click", element=10, label="Best Sellers"), call("click", element=6, label="anu@acme.test")),
        reply(w, w, w, w, w, w, w),
        tool("done", summary="Clicked around."),
    ])


@scenario
def loop_detection():
    return Scenario("loop_detection", "list", ["check the acme contact details"], plans=[""],
                    moves={("click", 6): "form", ("click", 15): "list"}, replies=[
        tool("click", element=6, label="anu@acme.test"), tool("click", element=15, label="Cancel"),
        tool("click", element=6, label="anu@acme.test"), tool("click", element=15, label="Cancel"),
        tool("click", element=6, label="anu@acme.test"), tool("click", element=15, label="Cancel"),
        tool("click", element=6, label="anu@acme.test"), tool("done", summary="unreachable"),
    ])


@scenario
def short_plan_no_approval():
    """A 1-step look-up plan is shown, not held: the click runs at once, with no plan_review."""
    short = '{"goal": "Open the contact", "steps": ["Click the contact"], "done_when": "The contact is open"}'
    return Scenario("short_plan_no_approval", "list", ["open the acme contact please"], plans=[short],
                    moves={("click", 6): "form"}, replies=[
        tool("click", element=6, label="anu@acme.test"),
        tool("done", summary="Opened the contact."),
    ])


@scenario
def short_plan_with_side_effects():
    """A 2-step plan that sends something still waits for approval (the click is refused first)."""
    risky = '{"goal": "Send a message", "steps": ["Open the chat", "Send hai"], "done_when": "The message is sent"}'
    return Scenario("short_plan_with_side_effects", "list", ["send hai to anu in the chat"], plans=[risky],
                    reviews=[("approve", None)], moves={("click", 6): "form"}, replies=[
        tool("click", element=6, label="anu@acme.test"),
        tool("plan", steps=["Open the chat", "Send hai"]),
        tool("click", element=6, label="anu@acme.test"),
        tool("done", summary="Sent."),
    ])


@scenario
def agent_plan_tool_short():
    """The agent's own plan call: 2 harmless steps are saved without asking; 3 steps would ask."""
    return Scenario("agent_plan_tool_short", "list", ["check the acme contact details"], plans=[""], replies=[
        tool("plan", steps=["Open the contact", "Read it"]),
        tool("plan", steps=["Open the contact", "Read it"]),  # same plan, now approved: told not to repeat it
        tool("done", summary="Read."),
    ])


@scenario
def stuck_and_repeat():
    return Scenario("stuck_and_repeat", "list", ["look around the list"], plans=[""], replies=[
        tool("click", element=5, label="More actions"), tool("click", element=5, label="More actions"),
        tool("click", element=5, label="More actions"), tool("hover", element=5, label="More actions"),
        tool("hover", element=5, label="More actions"), tool("switch_tab", tab=2),
        tool("done", summary="Nothing changed."),
    ])


@scenario
def scroll_limits():
    down = call("scroll", direction="down")
    return Scenario("scroll_limits", "shop", ["scroll through results"], replies=[
        reply(down, down, down, down),
        tool("scroll", direction="down", element=25),
        tool("done", summary="Scrolled."),
    ])


@scenario
def scroll_total_cap():
    def scroll(browser, args):  # every scroll shows something new (the title moves on)
        browser.page = {**browser.page, "title": f"Feed {browser.counts['scroll']}"}
        return "Scrolled."
    down = call("scroll", direction="down")
    return Scenario("scroll_total_cap", "shop", ["scroll the whole feed"], plans=[""], handlers={"scroll": scroll}, replies=[
        reply(down, down, down, down, down), reply(down, down, down, down, down), reply(down, down, down, down, down),
        tool("done", summary="Scrolled a lot."),
    ])


@scenario
def step_limit():
    return Scenario("step_limit", "shop", ["wait around"], replies=[tool("wait", seconds=0) for _ in range(25)])


@scenario
def sheets():
    return Scenario("sheets", "sheet", ["read the student sheet please"], plans=[""], confirms=[True, False, True, True], replies=[
        tool("read_text"),
        tool("go_to_cell", cell="A5"),
        tool("type_here", text="Z"),  # an empty row: no overwrite prompt, but editing is asked once
        tool("go_to_cell", cell="A2"),
        tool("read_cells", cells="A2"),
        tool("read_cells", cells="B9"),
        tool("read_cells", cells="A1:B2"),
        tool("sheet_query", sql="SELECT Name FROM sheet"),
        tool("sheet_query", sql="DROP TABLE sheet"),
        tool("go_to_cell", cell="A3"),
        tool("type_here", text="X\tY"),
        tool("type_here", text="X\tY"),
        tool("press_key", key="Delete"),
        tool("done", summary="Read the sheet."),
    ])


@scenario
def pointer_and_keys():
    def describe(browser, args):  # [29] changed on the page since the AI looked
        return 'button "Remove"' if args.get("element") == 29 else browser.default("describe_element", args)
    return Scenario("pointer_and_keys", "shop", ["click the go button"], plans=[""],
                    handlers={"describe_element": describe,
                              "enter_target": lambda b, a: {"inForm": True, "submitter": 'input submit "Go"',
                                                            "field": 'input text "Search Amazon.in"'}}, replies=[
        tool("drag", from_element=26, to_element=28),
        tool("press_key", key="Enter"),
        tool("type", element=3, text="mouse", submit=True, label="Search Amazon.in"),
        tool("click", element=29, label="Add to cart"),
        tool("click", element=27, label="Add to cart", count=2),
        tool("done", summary="Clicked Go."),
        tool("done", summary="Clicked Go."),
    ])


@scenario
def open_shortcut():
    return Scenario("open_shortcut", "shop", ["open best sellers"], moves={("click", 10): "list"},
                    replies=[tool("click", element=10, label="Best Sellers")])


@scenario
def new_site_and_location():
    return Scenario("new_site_and_location", "shop", ["compare mouse prices on amazon and flipkart"], plans=[PLAN],
                    location={"country": "India", "place": "Hyderabad, Telangana, India"},
                    moves={("open_url", "https://www.flipkart.com/search?q=mouse"): "flip"}, replies=[
        tool("plan", steps=["Search amazon", "Search flipkart", "Compare"]),
        {**tool("open_url", url="https://www.flipkart.com/search?q=mouse"), "model": "test:other"},
        tool("note", text="Flipkart mouse ₹499"),
        tool("plan", steps=["✔ Search amazon", "✔ Search flipkart", "Compare"]),
        tool("done", summary="Amazon ₹699, Flipkart ₹499 at Flipkart Mega Store."),
        tool("done", summary="Amazon ₹699, Flipkart ₹499 at Flipkart Mega Store."),
    ])


@scenario
def unrestricted_page_text():
    return Scenario("unrestricted_page_text", "fullshop", ["what is on this page"], replies=[
        tool("read_text", scope="page"),
        tool("done", summary="Mice from ₹399 to ₹699."),
    ])


@scenario
def form_errors():
    return Scenario("form_errors", "formerr", ["save the activity form now"], replies=[
        tool("click", element=16, label="Save Activity"),
        tool("click", element=16, label="Save Activity"),
        tool("batch", steps=[{"op": "click", "element": 16, "label": "Save Activity"}, {"op": "wait"}]),
        tool("done", summary="Saved it."),
        tool("done", summary="It shows errors: Contact is required."),
    ])


async def session_ops(agent):
    """What main.py does besides run(): the AI view button, reset, Stop (close_open_calls + finish)."""
    view = await agent.look()
    emit("look", view)
    agent.messages.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "open_1", "type": "function", "function": {"name": "click", "arguments": "{}"}},
        {"id": "open_2", "type": "function", "function": {"name": "wait", "arguments": "{}"}}]})
    agent.messages.append({"role": "tool", "tool_call_id": "open_2", "content": "OK."})
    agent.close_open_calls()
    emit("after_close_open_calls", agent.messages[-3:])
    await agent.finish("stopped", "Stopped by you.")
    agent.task_id = None
    agent.reset()
    emit("after_reset", {"messages": len(agent.messages)})


@scenario
def session():
    return Scenario("session", "shop", ["show me the cheapest wireless mouse here", session_ops,
                                        "find the cheapest wireless mouse again"],
                    plans=[PLAN, PLAN], reviews=[("approve", None)], replies=[
        text('done({"summary": "Portronics Toad 23 Wireless Optical Mouse at ₹399."})'),
        tool("plan", steps=["Look", "Answer"]),
        tool("done", summary="Portronics Toad 23 Wireless Optical Mouse at ₹399."),
    ])


@scenario
def gate_answers():
    """Chat and general knowledge are answered by the gate: no page view, no planner, no agent call."""
    return Scenario("gate_answers", "shop", ["hai", "what is 17 times 23, in 2024?", "explain more"],
                    gates=['{"kind": "chat", "reply": "Hi! I can open sites and search for you."}',
                           '```json\n{"kind": "answer", "reply": "17 × 23 = 391."}\n```',
                           '{"kind": "answer", "reply": "17 × 20 = 340 and 17 × 3 = 51."}'], replies=[])


@scenario
def gate_passes_tasks():
    """Anything unclear or broken goes to the agent unchanged: task, junk, wrong kind, empty reply, error, no reply."""
    return Scenario("gate_passes_tasks", "shop",
                    ["price of iPhone 16e 256GB on amazon", "hello there friend", "what is the weather today",
                     "open a page about cats", "tell me a joke please", "anything else at all"],
                    gates=['{"kind": "task", "reply": ""}', "I think this is a greeting",
                           '{"kind": "answer", "reply": ""}', RuntimeError("model down"),
                           '{"kind": "weird", "reply": "hmm"}', '{"kind": "chat", "reply": "' + "x" * 1300 + '"}'],
                    plans=[""] * 5,  # the 3-word task skips the planner (MIN_WORDS)
                    replies=[tool("done", summary="Done 1."), tool("done", summary="Done 2."),
                             tool("done", summary="Done 3."), tool("done", summary="Done 4."),
                             tool("done", summary="Done 5."), tool("done", summary="Done 6.")])


@scenario
def gate_skipped_when_answering_ask():
    """A message answering ask_user continues the task: the gate must not see it ("ok" would look like chat)."""
    return Scenario("gate_skipped_when_answering_ask", "login", ["sign in to my account", "ok it is hunter2"],
                    gates=['{"kind": "task", "reply": ""}'], plans=[""], replies=[
        tool("ask_user", question="What is the password?"),
        tool("done", summary="Signed in."),
        tool("done", summary="Signed in."),  # the login page still shows an error: the done is sent back once
    ])


# ---------- running ----------

SKIP = {"browser", "ui", "confirm", "plan_review", "messages", "page_views", "screen_reads", "state"}


def fields(agent):
    """Every Agent field, with TaskState's fields (if any) flattened in – the same set either way."""
    out = {k: v for k, v in vars(agent).items() if k not in SKIP}
    if (state := getattr(agent, "state", None)) is not None:
        out.update({f.name: getattr(state, f.name) for f in dataclasses.fields(state)})
    out["_page_views"] = [t for _, t in agent.page_views]
    out["_screen_reads"] = list(agent.screen_reads)
    return out


async def run(make):
    global SCENARIO
    SCENARIO = Scenario("…", "shop", [], [])  # so building the replies can number the calls
    SCENARIO.ids = itertools.count(1)
    sc = make()
    sc.ids = SCENARIO.ids
    SCENARIO = sc
    browser = Browser(sc.start, sc.moves, sc.handlers)

    async def ui(kind, text):
        emit("ui", [kind, text])

    async def confirm(text):
        allowed = sc.confirms.pop(0) if sc.confirms else True
        emit("confirm", [text, allowed])
        return allowed

    async def plan_review(steps):
        decision, edited = sc.reviews.pop(0) if sc.reviews else ("approve", None)
        emit("plan_review", [steps, decision, edited])
        return decision, edited if edited is not None else steps

    agent = Agent(browser, ui, confirm, plan_review, client="replay")
    agent.dev_mode = sc.dev_mode
    for task in sc.tasks:
        if callable(task):
            await task(agent)
            continue
        sc.seen_messages = 0
        emit("task", task)
        try:
            await agent.run(task, "test:model", sc.location)
        except Exception as err:
            emit("run_error", f"{type(err).__name__}: {err}")
        emit("messages", agent.messages)
        emit("fields", fields(agent))
    emit("left_over", {"replies": len(sc.replies), "plans": len(sc.plans), "confirms": len(sc.confirms),
                       "reviews": len(sc.reviews), **({"gates": len(sc.gates)} if sc.gates is not None else {})})


async def main():
    only = set(sys.argv[1:])
    for make in SCENARIOS:
        if not only or make.__name__ in only:
            await run(make)


if __name__ == "__main__":
    asyncio.run(main())
