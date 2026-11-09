"""The agent loop: ask the AI → run the operation in the browser → show it the new page → repeat."""

import base64
import json
import logging
import re
from urllib.parse import urlparse

from app import history, sheets
from app.agent import safety
from app.agent.changes import click_result, repeated_only, what_changed
from app.agent.prompt import SYSTEM_PROMPT, where_line
from app.agent.tools import TOOLS
from app.agent.understand import understand
from app.config import MAX_STEPS
from app.llm.client import ask_ai, has_vision_model, skip_model

log = logging.getLogger("krify")

READ_OPS = {"get_page_info", "get_elements", "read_text", "sheet_query", "read_cells"}
SHEET_OPS = {"sheet_query", "read_cells"}
OLD_PAGE = "\n[older page view removed to save space]"
OLD_SCREEN = "[older screen text removed to save space – use your notes]"
MAX_READ_SITES = 3  # newest page text is kept for this many sites
SHEET_HINT = "[Google Sheet] Call read_text first to see the columns, then sheet_query with SQL."


# Batches: actions the AI may send several of in one reply.
ACTION_OPS = {"open_url", "go_back", "reload", "new_tab", "switch_tab", "close_tab", "click", "hover", "type",
              "type_here", "go_to_cell", "press_key", "scroll", "select_option", "wait", "click_at", "drag"}
MAX_BATCH = 5
BATCH_LIMIT = "Skipped: at most 5 steps at once. Look at the page and continue."
PAGE_MOVED = ("Skipped: the page changed after an earlier step, so element numbers may be different. "
              "Look at the new page and decide again.")
OVERWRITE_BLOCKED = (
    "NOT typed: those cells already have data ({}). The user blocked overwriting them. To add a new "
    "row, go_to_cell the first EMPTY row: {}."
)
BLOCKED = ("BLOCKED by the user – this action was NOT done. "
           "Don't try it again. Tell the user what's left, or call ask_user.")
DONE_TOO_EARLY ="Not sent: check the page after your steps first, then call done."
_VALUES = re.compile(r' \((?:value: ".*?"|empty|checked|unchecked|shows on hover)\)| = ".*?"')


def image_size(data_url):
    """(width, height) of a JPEG data URL, read from its header; None if unknown."""
    try:
        data = base64.b64decode(data_url.split(",", 1)[1])
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            if data[i + 1] in (0xC0, 0xC1, 0xC2):  # "start of frame": holds the size
                return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
            i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    except Exception:
        pass
    return None


def uses_element(args):
    """Does this step point at an element number? Only those can hit the wrong thing when the page
    changes; steps by cell (go_to_cell, type_here, click cell=) or keys can't."""
    return args.get("element") is not None or args.get("from_element") is not None or args.get("to_element") is not None


def _args_of(call):
    try:
        return json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        return {}


def layout(page):
    """The page without typed values or ticks, to see if element numbers could have moved."""
    return _VALUES.sub("", page or "")


def site_of(url):
    """'https://www.amazon.in/s?k=x' -> 'amazon.in' ('' for new tabs and browser pages)."""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    host = parsed.hostname or ""
    return host[4:] if host.startswith("www.") else host


def is_sheet(url):
    return "docs.google.com/spreadsheets/" in (url or "")

# Stuck detector: these always count as progress; clicks/keys count only if the page changed.
PROGRESS_OPS = {"read_text", "sheet_query", "read_cells", "go_to_cell", "type_here", "note", "type", "select_option", "open_url", "new_tab"}
OLD_SCREENSHOT = "[older screenshot removed to save space]"
IDLE_LIMIT = 5
MAX_SCROLLS_PER_PAGE = 4
SCROLL_REFUSED = (
    "NOT scrolled: you already scrolled this page 4 times. Decide with what you've found: save it with note, "
    "or note that it's not on this site, then continue with your plan (or finish with done)."
)
WRAP_UP_AT = 5  # steps left
WRAP_UP = (
    "⚠️ Only 5 steps left. Finish now: call done with what you found so far (from your notes), "
    "and say clearly what you couldn't find."
)
REPEAT_NOTE = (
    "\nWarning: you've done this same step 3 times and nothing changed. Don't do it again: try a "
    "different way (another element, read_text, a fresh screenshot), or call ask_user."
)
NEW_SITE = ("\n[New site: {}] Check your plan still fits this site. If not, call plan with the fixed "
            "steps (keep ✔ on finished ones).")
SAME_READ = ("You already read this – the text is exactly the same as last time. Don't read it again: "
             "save what you need with note, or if it's not there, note \"not found\" and move on with your plan.")
WRONG_PRODUCT = (
    "NOT saved: the user asked for {} but your note doesn't say it, so this may be a different product "
    "(iPhone 16 is not iPhone 16e; Sponsored results are often other models). Check the product name on "
    "the page. Find the right product, or if this really is it, call note again with its full name."
)
PRODUCT_REJECTED = (
    "REJECTED – not sent to the user. The user asked for {} but your answer doesn't say it. Check each "
    "price is for exactly that product (e.g. iPhone 17e or 16 is not 16e), then call done again naming "
    "the exact product (model + storage) for each price."
)
WRONG_PAGE = ("\n⚠️ This page's title doesn't mention {} – it may be a different product than the user "
              "asked for. Check the product name before using any price from it.")
SAME_PLAN = "That's the same plan you already have – no need to repeat it. Continue with the next step."
STUCK_NOTE = (
    "\nWarning: the last 5 steps made no progress (only scrolling, switching or clicking without "
    "the page changing). Stop wandering: read_text to see what's on screen, try a different "
    "element, or call ask_user."
)


def clean_text_reply(text):
    """The model sometimes writes done({...}) / ask_user({...}) as plain text. Pull out the message.

    Returns (kind, message, clear) where kind is "done" or "ask", and clear is False when the text
    is just free text (could be nonsense from a weak model).
    """
    text = (text or "").strip()
    # Forms seen: done({"summary": ...}), ask_user({"question": ...}), or a bare {"summary": ...}
    match = re.search(r"\b(?:done|ask_user)\s*\(\s*(\{.*\})\s*\)", text, re.DOTALL)
    candidate = match.group(1) if match else text
    if candidate.startswith("{"):
        try:
            args = json.loads(candidate)
            if isinstance(args, dict):
                if "question" in args:
                    return "ask", str(args["question"]), True
                if "summary" in args:
                    return "done", str(args["summary"]), True
        except json.JSONDecodeError:
            pass
    return "done", text or "Done.", False


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def missing_words(task, text):
    """Model words from the task (letters + digits: 16e, s24, 256gb) that the text doesn't mention."""
    flat = re.sub(r"[^a-z0-9]", "", text.lower())  # "256 GB", "Apple-iPhone-17e" -> "256gb", "appleiphone17e"
    words = {w for w in re.findall(r"[a-z0-9]+", task.lower()) if re.search(r"\d", w) and re.search(r"[a-z]", w)}
    return sorted(w for w in words if w not in flat)


def missing_models(task, note):
    """Model words missing from a note or answer that has a price – a sign the price is for a
    different product."""
    return missing_words(task, note) if re.search(r"\d{3}|₹|\$", note) else []


def unverified_numbers(text, seen):
    """Numbers with 3+ digits in `text` (prices, years…) that appear nowhere in `seen`.
    Thousands separators are ignored, so ₹69,900 matches 69900."""
    pages = re.sub(r"(?<=\d),(?=\d)", "", "\n".join(seen))
    missing = []
    for raw in _NUMBER.findall(text or ""):
        number = raw.replace(",", "").rstrip(".")
        if sum(c.isdigit() for c in number) < 3 or raw in missing:
            continue
        if not re.search(rf"(?<![\d.]){re.escape(number)}(?![\d])", pages):
            missing.append(raw.rstrip(".,"))
    return missing


NUDGE = (
    "Reminder: reply by calling one operation. If the task is finished, call done with a summary; "
    "if you need something from the user, call ask_user."
)


def describe(name, args):
    """Readable step text for the side panel, e.g. 'click [12]'."""
    match name:
        case "open_url": return f"🌐 Open {args.get('url')}"
        case "click" | "click_at":
            where = (f"cell {args['cell']}" if args.get("cell") else f"[{args.get('element')}]") if name == "click" \
                else f"at ({args.get('x')}, {args.get('y')})"
            count = str(args.get("count") or 1)
            word = "Right-click" if args.get("button") == "right" else {"2": "Double-click", "3": "Triple-click"}.get(count, "Click")
            return f"🖱️ {word} {where}"
        case "drag":
            a = f"[{args['from_element']}]" if args.get("from_element") is not None else f"({args.get('from_x')}, {args.get('from_y')})"
            b = f"[{args['to_element']}]" if args.get("to_element") is not None else f"({args.get('to_x')}, {args.get('to_y')})"
            return f"✋ Drag {a} → {b}"
        case "hover": return f"👆 Hover over [{args.get('element')}]"
        case "type":
            enter = " + Enter" if args.get("submit") else ""
            return f"⌨️ Type \"{args.get('text')}\" into [{args.get('element')}]{enter}"
        case "press_key":
            times = f" × {args.get('repeat')}" if str(args.get("repeat") or 1) not in ("1", "0") else ""
            return f"⌨️ Press {args.get('key')}{times}"
        case "type_here": return f"⌨️ Type \"{args.get('text')}\" where the cursor is"
        case "go_to_cell": return f"📍 Go to cell {args.get('cell')}"
        case "read_cells": return f"📊 Read cells {args.get('cells')}"
        case "scroll":
            where = f" the box at [{args['element']}]" if args.get("element") is not None else ""
            return f"↕️ Scroll{where} {args.get('direction')}"
        case "select_option": return f"📋 Choose \"{args.get('option')}\" in [{args.get('element')}]"
        case "new_tab": return f"🗂️ New tab: {args.get('url')}"
        case "switch_tab": return f"🗂️ Switch to tab {args.get('tab')}"
        case "close_tab": return f"🗂️ Close tab {args.get('tab')}"
        case "go_back": return "⬅️ Go back"
        case "reload": return "🔄 Reload"
        case "wait": return f"⏳ Wait {args.get('seconds')}s"
        case "get_page_info" | "get_elements": return "👀 Look at the page"
        case "read_text": return "📖 Read the whole page" if args.get("scope") == "page" else "📖 Read the screen"
        case "sheet_query": return f"📊 Ask the sheet: {args.get('sql')}"
        case _: return f"{name} {args}"


class Agent:
    """One agent per side-panel connection. Keeps the chat so follow-ups work."""

    def __init__(self, browser, ui, confirm):
        self.browser = browser  # async (name, args) -> str, raises on failure
        self.confirm = confirm  # async (text) -> bool: ask the user Allow / Block
        self.elements = ""  # latest numbered element list, used by the safety check
        self.last_typed_line = ""

        async def ui_and_log(kind, text):
            log.info("[%s] %s", kind, text)
            if self.task_id:
                history.add_step(self.task_id, kind, text)
            await ui(kind, text)

        self.task_id = None  # database id of the running task
        self.product_text = ""  # the user's own words, for the wrong-product checks
        self.current_title = ""
        self.location = None  # where the user is, sent by the side panel with each task
        self.model = None  # model the task is currently using

        self.ui = ui_and_log  # async (type, text) -> None
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.page_views = []  # (message, text_before_page_view) – trimmed later to save tokens
        self.last_page = None
        self.acted_on = ""  # label of the element the last action used, e.g. 'button "Contact sales"'
        self.screen_reads = {}  # site -> its newest read_text result (older ones are trimmed)
        self.screenshot_msgs = []  # messages holding a screenshot – dropped when the page changes
        self.tokens = 0
        # Per task: shown to the AI at every step so it keeps the goal in view.
        self.task_text = ""
        self.goal = ""
        self.done_when = ""
        self.plan = []
        self.sites = set()  # sites seen this task – a new one triggers a plan check
        self.last_task = ""  # previous task, so the planner understands follow-ups
        self.last_status = ""  # how the previous task ended ("ask" = the next message is an answer)
        self.notes = []
        self.seen = []  # everything read from pages this task – used by the fact check
        self.fact_check_failed = False

    def header(self):
        """'[Your task] … [Your plan] … [Your notes] …' – sent with every page view."""
        lines = [f"[Your task] {self.task_text}"]
        if self.goal:
            lines.append(f"[Goal] {self.goal}")
        if self.plan:
            lines.append("[Your plan] " + " | ".join(f"{i}. {s}" for i, s in enumerate(self.plan, 1)))
        if self.notes:
            lines.append("[Your notes] " + " | ".join(self.notes))
        if self.done_when:
            lines.append(f"[Done when] {self.done_when}")
        return "\n".join(lines)

    def check_facts(self, summary):
        """Fact check before 'done': every number (3+ digits) in the answer must have appeared on a
        page Krify read this task, and a priced answer must name the product asked for (16e, 256gb).
        Returns a rejection message, or None if the answer may go out."""
        if (models := missing_models(self.product_text, summary)) and not self.product_check_failed:
            self.product_check_failed = True
            return PRODUCT_REJECTED.format(", ".join(models))
        missing = unverified_numbers(summary, self.seen)
        if not missing:
            return None
        if self.fact_check_failed:  # second time: let it through, but tell the user honestly
            return None
        self.fact_check_failed = True
        return (
            "REJECTED – not sent to the user. These numbers in your answer were not seen on any page you read: "
            f"{', '.join(missing)}. Use read_text to find the real values, report only numbers you actually "
            "saw (no estimates or your own calculations), then call done again."
        )

    def reset(self):
        self.messages = self.messages[:1]
        self.page_views = []
        self.last_page = None
        self.screen_reads = {}
        self.screenshot_msgs = []
        self.last_task = self.last_status = ""

    def add_screen_read(self, message, text):
        """Keep the newest screen text of each site in full (at most MAX_READ_SITES sites), so a
        price read on one site is still there while on the next – the AI often forgets to note it."""
        site = site_of(self.current_url) or "this page"
        old = self.screen_reads.pop(site, None)
        if old:
            old["content"] = OLD_SCREEN
        while len(self.screen_reads) >= MAX_READ_SITES:  # dicts keep order: drop the oldest site
            self.screen_reads.pop(next(iter(self.screen_reads)))["content"] = OLD_SCREEN
        message["content"] = f"[Read from {site}]\n{text}"
        self.messages.append(message)
        self.screen_reads[site] = message

    def check_repeat(self, name, args, page_changed):
        """Repeat detector: the same action 3 times within the last 6 steps, with the page never
        changing (e.g. clicking one spot over and over). Positions within 20px count as the same."""
        clean = {k: (round(v / 20) if k in ("x", "y", "from_x", "from_y", "to_x", "to_y") and isinstance(v, (int, float))
                     else v) for k, v in args.items() if not k.startswith("_")}
        # Element numbers are given out fresh on every look, so the same button can be [32] then
        # [41]: compare what it is (its label), not its number.
        if "element" in clean and self.acted_on:
            clean["element"] = self.acted_on
        sig = f"{name} {json.dumps(clean, sort_keys=True, default=str)}"
        if page_changed:
            self.recent_steps = []
        self.recent_steps = (self.recent_steps + [sig])[-6:]
        if self.recent_steps.count(sig) >= 3:
            self.recent_steps = []
            return REPEAT_NOTE
        return ""

    def check_progress(self, name, page_changed):
        """Stuck detector: count steps that don't move the task forward."""
        progress = name in PROGRESS_OPS or (
            name in ("click", "click_at", "drag", "hover", "press_key", "go_back", "screenshot") and page_changed
        )
        self.idle_steps = 0 if progress else self.idle_steps + 1
        if self.idle_steps >= IDLE_LIMIT:
            self.idle_steps = 0
            return STUCK_NOTE
        return ""

    def close_open_calls(self):
        """After Stop, answer any tool calls left open, so the next request is valid."""
        answered = set()
        for msg in reversed(self.messages):
            if msg.get("role") == "tool":
                answered.add(msg.get("tool_call_id"))
            elif msg.get("role") == "assistant":
                for call in msg.get("tool_calls") or []:
                    if call["id"] not in answered:
                        self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": "Stopped by the user."})
                break

    async def page_view(self):
        """Current URL, title and numbered elements."""
        try:
            info = await self.browser("get_page_info", {})
            elements = await self.browser("get_elements", {})
            self.elements = elements
            self.seen.append(elements)
            self.current_url = info.get("url", "")
            self.current_title = str(info.get("title") or "")
            tabs = f"[Tabs] {info['tabs']}\n" if info.get("tabs") else ""
            if is_sheet(self.current_url):
                elements = f"{SHEET_HINT}\n{elements}"
            return f"{tabs}[Current page] {info.get('title')} – {info.get('url')}\n{elements}"
        except Exception as err:
            return f"[Current page] (can't read it: {err})"

    def drop_screenshots(self):
        """Replace older screenshots with a short note: they cost tokens and need a vision model."""
        for msg in self.screenshot_msgs:
            msg["content"] = OLD_SCREENSHOT
        self.screenshot_msgs = []

    async def add_screenshot(self, tool_msg):
        """Take a screenshot (with element numbers drawn on it) and attach it for the AI."""
        if not has_vision_model():
            self.messages.append({**tool_msg, "content": "Screenshots aren't available (no model that can see images)."})
            return
        result = await self.safe_run("screenshot", {})
        if not str(result).startswith("data:image/"):
            self.messages.append({**tool_msg, "content": f"Couldn't take a screenshot: {result}"})
            return
        self.drop_screenshots()
        self.shot_size = image_size(result)
        size = "{}×{}".format(*self.shot_size) if self.shot_size else "1024 wide"
        # Tool results must be text for most providers, so the image goes in a user message after it.
        self.messages.append({**tool_msg, "content": "Screenshot taken – it's in the next message."})
        image_msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Screenshot of the current screen, {size} pixels (click_at uses these "
                                         "pixels, x from the left, y from the top). Numbers in boxes = element numbers."},
                {"type": "image_url", "image_url": {"url": result}},
            ],
        }
        self.messages.append(image_msg)
        self.screenshot_msgs.append(image_msg)

    def add_with_page(self, message, text, page):
        """Add a message that ends with a page view. Only the newest page view is kept in full."""
        if page != self.last_page:
            self.drop_screenshots()  # the screen changed, so the old picture is out of date
        if page == self.last_page:  # nothing changed: don't send the same list again
            message["content"] = f"{text}\n\n{self.header()}\n[Page unchanged since the last view.]"
            self.messages.append(message)
            return
        site = site_of(self.current_url)
        if site and site not in self.sites:
            if self.plan and self.sites:  # the first site was known when planning
                text += NEW_SITE.format(site)
            self.sites.add(site)
        for old_msg, old_text in self.page_views:
            old_msg["content"] = old_text + OLD_PAGE
        message["content"] = f"{text}\n\n{self.header()}\n{page}"
        self.messages.append(message)
        self.page_views = [(message, text)]
        self.last_page = page

    async def finish(self, kind, text):
        """Send the final message, with how many tokens the task used, and save the result."""
        log.info("Task used %d tokens", self.tokens)
        if kind == "done" and (missing := unverified_numbers(text, self.seen)):
            text = f"{text}\n⚠️ Krify couldn't confirm these numbers on the pages it read: {', '.join(missing)}"
        self.end_task(kind, text)
        if kind == "done":
            text = f"{text}\n· used {self.tokens / 1000:.1f}k tokens"
        await self.ui(kind, text)

    def end_task(self, status, summary):
        """Save how the task ended (done / ask / stopped / error) in the history."""
        self.last_status = status
        if self.task_id:
            history.finish_task(self.task_id, status, summary, self.tokens, self.model)

    async def run(self, task, model, location=None):
        self.location = location
        where = where_line(location)
        self.messages[0]["content"] = f"{SYSTEM_PROMPT}\n\n{where}" if where else SYSTEM_PROMPT
        # The user's message may answer the agent's ask_user: then carry on with the same task.
        continuing = self.last_status == "ask"
        if continuing:  # same task: keep the goal, plan, notes and what was read so far
            self.product_text = f"{self.product_text} {task}"
            self.task_text = f"{self.task_text}\n(You asked the user; they answered: {task})"
            self.seen.append(task)
        else:
            self.product_text = task
            self.task_text, self.notes, self.sites = task, [], set()
            self.goal, self.plan, self.done_when = "", [], ""
            self.seen = [task]  # numbers the user gave count as known
        self.idle_steps = 0
        self.tokens = 0
        self.model = model
        self.task_id = history.start_task(task, model)
        self.last_read = ""  # newest read_text result, to catch reading the same text again
        self.note_warnings = set()  # wrong-product warnings already given (the 2nd try is saved)
        self.product_check_failed = False  # final answer sent back once for not naming the product
        self.fact_check_failed = False
        self.current_url = ""
        self.scrolls = {}  # url -> times scrolled on that page
        self.edit_allowed = False  # the user allowed editing a Google Sheet/Doc in this task
        self.recent_steps = []  # for the repeat detector
        self.shot_size = None  # (width, height) of the last screenshot
        self.shot_page = None  # the page when the last screenshot was taken
        self.last_cell = ""
        nudged = False
        empty_replies = 0
        page = await self.page_view()
        if not continuing:
            await self.make_plan(task, page, model)
        self.last_task = self.task_text
        self.add_with_page({"role": "user"}, task, page)

        for step in range(MAX_STEPS):
            if MAX_STEPS - step == WRAP_UP_AT:
                await self.ui("step", "⏰ Few steps left – asking Krify to wrap up")
                self.messages.append({"role": "user", "content": WRAP_UP})
            reply, used_model, tokens = await ask_ai(
                self.messages, TOOLS, model, on_note=lambda t: self.ui("step", t),
                need_vision=bool(self.screenshot_msgs),
            )
            self.tokens += tokens
            if used_model != model:
                model = used_model  # keep using the model that still has free quota
                self.model = model

            if not reply.tool_calls and not (reply.content or "").strip():
                # An empty reply is never "done": ask again, and after that use another model.
                empty_replies += 1
                if empty_replies >= 3:
                    await self.finish("error", "The AI models kept sending empty replies. Please try again in a minute.")
                    return
                if empty_replies == 2:
                    skip_model(model)
                    await self.ui("step", "🔁 Empty reply from the AI – switching model")
                self.messages.append({"role": "user", "content": NUDGE})
                continue
            empty_replies = 0

            if not reply.tool_calls:  # plain text instead of an operation
                self.messages.append({"role": "assistant", "content": reply.content or ""})
                kind, text, clear = clean_text_reply(reply.content)
                if not clear and not nudged:
                    # Might be nonsense (weak models do this). Ask once for a proper operation.
                    self.messages.append({"role": "user", "content": NUDGE})
                    nudged = True
                    continue
                if kind == "done" and (rejection := self.check_facts(text)):
                    await self.ui("step", "🔍 Fact check: answer sent back to be checked")
                    self.messages.append({"role": "user", "content": rejection})
                    continue
                await self.finish(kind, text)
                return
            nudged = False

            self.messages.append(
                {
                    "role": "assistant",
                    "content": reply.content or "",
                    "tool_calls": [c.model_dump(exclude_none=True) for c in reply.tool_calls],
                }
            )

            finished = False
            # Batch: several actions in one reply (e.g. filling a form). The page is shown after the
            # last one; the batch stops early if the page changed (element numbers may have moved).
            calls = reply.tool_calls
            action_count = sum(c.function.name in ACTION_OPS | {"batch"} for c in calls)
            if action_count > 1:
                await self.ui("step", f"📦 {min(action_count, MAX_BATCH)} steps at once")
            actions_done = 0
            batch_stopped = ""  # why the rest of the batch is skipped
            prev_page = self.last_page
            for i, call in enumerate(calls):
                name = call.function.name
                tool_msg = {"role": "tool", "tool_call_id": call.id}
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                if finished:
                    self.messages.append({**tool_msg, "content": "Skipped."})
                elif name in ACTION_OPS and batch_stopped:
                    self.messages.append({**tool_msg, "content": batch_stopped})
                elif name == "done" and actions_done:
                    self.messages.append({**tool_msg, "content": DONE_TOO_EARLY})
                elif name == "done":
                    summary = args.get("summary", "Done.")
                    if rejection := self.check_facts(summary):
                        await self.ui("step", "🔍 Fact check: answer sent back to be checked")
                        self.messages.append({**tool_msg, "content": rejection})
                        continue
                    self.messages.append({**tool_msg, "content": "Reported to the user."})
                    await self.finish("done", summary)
                    finished = True
                elif name == "plan" and [str(s) for s in (args.get("steps") or [])][:8] == self.plan:
                    self.messages.append({**tool_msg, "content": SAME_PLAN})
                elif name == "plan":
                    title = "📋 Plan updated:" if self.plan else "📋 Plan:"
                    self.plan = [str(s) for s in (args.get("steps") or [])][:8]
                    await self.ui("step", f"{title}\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(self.plan, 1)))
                    self.messages.append(
                        {**tool_msg, "content": "Plan saved – you'll see it at every step. Call plan again to "
                                                "update it; start finished steps with ✔."}
                    )
                    self.check_progress(name, False)
                elif name == "ask_user":
                    self.messages.append({**tool_msg, "content": "Question shown. The user's answer comes next."})
                    await self.finish("ask", args.get("question", "Can you tell me more?"))
                    finished = True
                elif name == "screenshot":
                    await self.ui("step", "📸 Screenshot")
                    await self.add_screenshot(tool_msg)
                    self.check_progress(name, self.last_page != self.shot_page)
                    self.shot_page = self.last_page
                elif name == "note":
                    text = str(args.get("text", ""))
                    missing = missing_models(self.product_text, text)
                    if missing and tuple(missing) not in self.note_warnings:
                        # e.g. the task says "iPhone 16e" but the note says "iPhone 16" (a sponsored result).
                        self.note_warnings.add(tuple(missing))
                        await self.ui("step", f"🔍 Note doesn't mention {', '.join(missing)} – checking the product")
                        self.messages.append({**tool_msg, "content": WRONG_PRODUCT.format(", ".join(missing))})
                    else:
                        await self.ui("step", f"📝 {text}")
                        self.notes.append(text[:200])
                        self.messages.append({**tool_msg, "content": "Saved."})
                    self.check_progress(name, False)
                elif name in READ_OPS:
                    on_sheet = name in SHEET_OPS | {"read_text"} and is_sheet(self.current_url)
                    label = "📊 Read the sheet" if on_sheet and name == "read_text" else describe(name, args)
                    await self.ui("step", label)
                    if on_sheet:
                        result = await self.sheet_op(name, args)
                    elif name in SHEET_OPS:
                        result = f"Error: {name} only works on a Google Sheets page."
                    else:
                        result = await self.safe_run(name, args)
                    same_read = name == "read_text" and str(result) == self.last_read
                    stuck = self.check_progress("same_read" if same_read else name, False)
                    self.seen.append(str(result))
                    if name == "get_elements":
                        self.elements = str(result)
                        self.add_with_page(tool_msg, "Elements:", result)
                    elif same_read:
                        # Reading the same unchanged text again (seen 6× in a row on Amazon): say so.
                        self.messages.append({**tool_msg, "content": SAME_READ + stuck})
                    elif name == "read_text":
                        self.last_read = str(result)
                        if not on_sheet and (models := missing_words(self.product_text, f"{self.current_title} {self.current_url}")):
                            result = f"{result}{WRONG_PAGE.format(', '.join(models))}"
                        self.add_screen_read(tool_msg, f"{result}\n(Save facts you'll need later with note.)")
                    else:
                        self.messages.append({**tool_msg, "content": str(result)})
                elif name == "batch":
                    actions_done += await self.run_batch(tool_msg, args.get("steps"))
                else:
                    result, status = await self.do_action(name, args)
                    if status != "ok":
                        self.messages.append({**tool_msg, "content": result})
                        self.check_progress(name, False)
                        if status == "blocked":
                            batch_stopped = "Skipped: an earlier step was blocked by the user."
                        continue
                    actions_done += 1
                    page = await self.page_view()
                    # Extra copies of what was already there (a form opened twice) isn't progress.
                    changed = page != prev_page and not repeated_only(prev_page, page)
                    # Say plainly what the action changed, so the model knows if it worked.
                    if diff := what_changed(prev_page, page):
                        result = f"{result}\n{diff}"
                    # Say plainly whether a click worked (it opened something / hid something).
                    # Not after a failed click ("Element [20] not found"): the page changing then isn't its doing.
                    clicked = name in ("click", "click_at") and not result.startswith("Error")
                    if clicked and (said := click_result(prev_page, page, self.acted_on, self.current_title)):
                        result = f"{result}\n{said}"
                        log.info("  told the AI: %s", said)
                    result = f"{result}{self.check_progress(name, changed)}{self.check_repeat(name, args, changed)}"
                    more = any(c.function.name in ACTION_OPS for c in calls[i + 1:])
                    later = [_args_of(c) for c in calls[i + 1:] if c.function.name in ACTION_OPS]
                    moved = layout(page) != layout(prev_page) and any(uses_element(a) for a in later)
                    prev_page = page
                    if more and not moved and actions_done < MAX_BATCH:
                        self.messages.append({**tool_msg, "content": f"{result} (next step follows)"})
                        continue
                    if more:
                        batch_stopped = BATCH_LIMIT if actions_done >= MAX_BATCH else PAGE_MOVED
                    # Show the AI the page after the last action, so it can check the result.
                    self.add_with_page(tool_msg, result, page)

            if finished:
                return

        # Never end empty-handed: report what was found so far.
        found = "\n".join(f"• {n}" for n in self.notes) or "• nothing confirmed yet"
        await self.finish(
            "done",
            f"Stopped after {MAX_STEPS} steps (safety limit). What Krify found so far:\n{found}\n"
            "Try a simpler or more specific task for the rest.",
        )

    async def make_plan(self, task, page, model):
        """Plan from the AI's own knowledge before the first action. It never asks the user anything:
        questions come later from the agent, once it has seen the real options on a site."""
        result, tokens = await understand(task, page, self.last_task, model, self.location, on_note=lambda t: self.ui("step", t))
        self.tokens += tokens
        if not result:
            return
        self.goal, self.plan, self.done_when = result["goal"], result["steps"], result["done_when"]
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(self.plan, 1))
        await self.ui("step", f"🎯 Goal: {self.goal}\n📋 Plan:\n{steps}")

    async def ask_permission(self, reason, name, args):
        """Show Allow / Block in the side panel. Returns True only if the user allows."""
        line = safety.element_line(self.elements, args.get("element"))
        what = describe(name, args) + (f" – {line}" if line else "")
        if name == "type_here" and self.last_cell:
            what += f" (cell {self.last_cell})"
        allowed = await self.confirm(f"{reason}: {what}")
        await self.ui("step", "✅ Allowed by you" if allowed else "⛔ Blocked")
        return allowed

    async def do_action(self, name, args):
        """One browser action, with the safety check and the scroll limit.
        Returns (result text, status): status "ok", "blocked" (by the user) or "refused" (scroll limit)."""
        await self.ui("step", describe(name, args))

        # Typing into a sheet over cells that already hold data: always ask, showing what would be lost.
        if name == "type_here" and is_sheet(self.current_url):
            in_the_way, empty_row = await self.cells_in_the_way(str(args.get("text", "")))
            if in_the_way:
                keep = f" Block to keep them (Krify will use the first empty row, {empty_row}, instead)." if empty_row else ""
                allowed = await self.confirm(f"⚠️ Overwriting: {in_the_way}.{keep}")
                await self.ui("step", "✅ Allowed by you" if allowed else "⛔ Blocked")
                if not allowed:
                    return OVERWRITE_BLOCKED.format(in_the_way, empty_row or "last _row + 1"), "blocked"
                self.edit_allowed = True  # they saw exactly what changes

        # Safety check runs in code: risky actions always need the user's OK.
        if name == "click_at" and self.shot_size:  # a spot outside the picture is a misread position
            w, h = self.shot_size
            try:
                x, y = float(args.get("x")), float(args.get("y"))
            except (TypeError, ValueError):
                x = y = -1
            if not (0 <= x <= w and 0 <= y <= h):
                return (f"NOT clicked: ({args.get('x')}, {args.get('y')}) is outside the screenshot, which is "
                        f"{w}×{h} pixels. Use pixel positions on the picture."), "refused"
        if name == "click_at":  # no element number: check what's under the spot
            under = await self.safe_run("element_at", {"x": args.get("x"), "y": args.get("y")})
            if str(under).startswith("Error"):
                return str(under), "refused"
            args = {**args, "_under": str(under)}
        reason = safety.check(name, args, self.elements, self.last_typed_line, self.current_url)
        if reason == "Editing" and self.edit_allowed:
            reason = None  # editing this document was already allowed in this task
        if reason and not await self.ask_permission(reason, name, args):
            return BLOCKED, "blocked"
        if reason == "Editing":
            self.edit_allowed = True
        if name == "go_to_cell":
            self.last_cell = str(args.get("cell", ""))

        if name == "scroll":
            # Scrolling a box (embedded page, chat list) has its own limit, apart from the page's.
            key = self.current_url + (" box" if args.get("element") is not None else "")
            count = self.scrolls.get(key, 0)
            if count >= MAX_SCROLLS_PER_PAGE:
                await self.ui("step", "🛑 Scroll limit for this page – deciding with what was found")
                return SCROLL_REFUSED, "refused"
            self.scrolls[key] = count + 1

        if name == "type":
            self.last_typed_line = safety.element_line(self.elements, args.get("element"))
        line = safety.element_line(self.elements, args.get("element")) if args.get("element") is not None else ""
        self.acted_on = _VALUES.sub("", line)  # typed values don't make it a different element
        return str(await self.safe_run(name, args)), "ok"

    async def run_batch(self, tool_msg, steps):
        """The batch tool: several actions in a row, the page shown once at the end. Stops at a
        blocked or failed step, or when the page changes (element numbers may have moved).
        Returns how many actions ran."""
        steps = [s for s in (steps or []) if isinstance(s, dict)]
        count = min(len(steps), MAX_BATCH)
        await self.ui("step", f"📦 {count} step{'s' if count != 1 else ''} at once")
        report, page, prev, ran = [], None, self.last_page, 0
        for n, step in enumerate(steps[:MAX_BATCH], 1):
            op = step.get("op")
            args = {k: v for k, v in step.items() if k != "op"}
            if op not in ACTION_OPS:
                report.append(f"{n}. '{op}' can't be used in batch – stopped here.")
                break
            result, status = await self.do_action(op, args)
            if status != "ok":
                report.append(f"{n}. {result}")
                break
            ran += 1
            page = await self.page_view()
            self.check_progress(op, page != prev and not repeated_only(prev, page))
            if result.startswith("Error"):
                report.append(f"{n}. {describe(op, args)} FAILED: {result} – stopped here.")
                break
            diff = what_changed(prev, page, limit=3)
            report.append(f"{n}. {result} ✓" + (f" – {diff}" if diff else ""))
            moved = layout(page) != layout(prev) and any(uses_element(s) for s in steps[n:MAX_BATCH])
            prev = page
            if moved and n < len(steps):
                report.append("Stopped: the page changed, so the remaining steps were skipped.")
                break
        else:
            if len(steps) > MAX_BATCH:
                report.append("Only the first 5 steps were done.")
        if not steps:
            report.append("No steps given.")
        self.add_with_page(tool_msg, "\n".join(report), page or await self.page_view())
        return ran

    async def cells_in_the_way(self, typed):
        """What typing `typed` at the selected cell would overwrite, as text for the user
        ('A125 = 'STU2026124', B125 = 'Rahul', …', or '' if all those cells are empty), and the
        sheet's first empty row (None if unknown)."""
        try:
            live = await self.browser("sheet_active_cell", {})
            start = str(live.get("cell", "")).upper().split(":")[0]
            data = await self.browser("sheet_csv", {})
            found = sheets.filled_cells(data["csv"], start, typed)
            empty_row = sheets.first_empty_row(data["csv"])
        except Exception as err:
            log.warning("Couldn't check the cells before typing: %s", err)
            return "couldn't check whether these cells are empty – they may hold data", None
        if not found:
            return "", empty_row
        shown = ", ".join(f"{ref} = {value!r}" for ref, value in found[:6])
        return shown + (f" (+{len(found) - 6} more cells)" if len(found) > 6 else ""), empty_row

    async def live_cell(self, ref):
        """One cell that's selected right now: read it from the formula bar – instant, and right
        even before Google has saved. None if it can't be read that way (then the sheet is downloaded)."""
        ref = ref.strip().upper().replace("$", "")
        if not ref or ":" in ref:
            return None
        try:
            live = await self.browser("sheet_active_cell", {})
        except Exception:
            return None
        formula = live.get("formula") if isinstance(live, dict) else None
        if formula is None or str(live.get("cell", "")).upper() != ref or formula.startswith("="):
            return None  # another cell is selected, or a formula (its result is in the download)
        return f"{ref}: {formula!r} (live, from the formula bar)" if formula else f"{ref}: (empty) (live)"

    async def sheet_op(self, name, args):
        """Google Sheets: the extension downloads the sheet as CSV, DuckDB answers here."""
        if name == "read_cells" and (live := await self.live_cell(str(args.get("cells", "")))):
            return live
        data = await self.safe_run("sheet_csv", {})
        if not isinstance(data, dict):
            return data  # an "Error: …" text
        try:
            if name == "read_text":
                return sheets.summary(data.get("csv", ""), data.get("title", ""), data.get("tab", ""))
            if name == "read_cells":
                return sheets.cells(data.get("csv", ""), str(args.get("cells", "")))
            return sheets.query(data.get("csv", ""), args.get("sql", ""))
        except Exception as err:
            await self.ui("step", f"⚠️ {err}")
            return f"Error: {err}"

    async def safe_run(self, name, args):
        try:
            return await self.browser(name, args)
        except Exception as err:
            await self.ui("step", f"⚠️ {err}")
            return f"Error: {err}"
