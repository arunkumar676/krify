"""The agent loop: ask the AI → run the operation in the browser → show it the new page → repeat."""

import json
import logging
import re

from app import history
from app.agent import safety
from app.agent.prompt import SYSTEM_PROMPT
from app.agent.tools import TOOLS
from app.config import MAX_STEPS
from app.llm.client import ask_ai

log = logging.getLogger("krify")

READ_OPS = {"get_page_info", "get_elements", "read_text"}
OLD_PAGE = "\n[older page view removed to save space]"
OLD_SCREEN = "[older screen text removed to save space – use your notes]"

# Stuck detector: these always count as progress; clicks/keys count only if the page changed.
PROGRESS_OPS = {"read_text", "note", "type", "select_option", "open_url", "new_tab"}
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
        case "click": return f"🖱️ Click [{args.get('element')}]"
        case "type":
            enter = " + Enter" if args.get("submit") else ""
            return f"⌨️ Type \"{args.get('text')}\" into [{args.get('element')}]{enter}"
        case "press_key": return f"⌨️ Press {args.get('key')}"
        case "scroll": return f"↕️ Scroll {args.get('direction')}"
        case "select_option": return f"📋 Choose \"{args.get('option')}\" in [{args.get('element')}]"
        case "new_tab": return f"🗂️ New tab: {args.get('url')}"
        case "switch_tab": return f"🗂️ Switch to tab {args.get('tab')}"
        case "close_tab": return f"🗂️ Close tab {args.get('tab')}"
        case "go_back": return "⬅️ Go back"
        case "reload": return "🔄 Reload"
        case "wait": return f"⏳ Wait {args.get('seconds')}s"
        case "get_page_info" | "get_elements": return "👀 Look at the page"
        case "read_text": return "📖 Read the whole page" if args.get("scope") == "page" else "📖 Read the screen"
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
        self.model = None  # model the task is currently using

        self.ui = ui_and_log  # async (type, text) -> None
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.page_views = []  # (message, text_before_page_view) – trimmed later to save tokens
        self.last_page = None
        self.screen_reads = []  # read_text results – only the newest is kept in full
        self.tokens = 0
        # Per task: shown to the AI at every step so it keeps the goal in view.
        self.task_text = ""
        self.plan = []
        self.notes = []
        self.seen = []  # everything read from pages this task – used by the fact check
        self.fact_check_failed = False

    def header(self):
        """'[Your task] … [Your plan] … [Your notes] …' – sent with every page view."""
        lines = [f"[Your task] {self.task_text}"]
        if self.plan:
            lines.append("[Your plan] " + " | ".join(f"{i}. {s}" for i, s in enumerate(self.plan, 1)))
        if self.notes:
            lines.append("[Your notes] " + " | ".join(self.notes))
        return "\n".join(lines)

    def check_facts(self, summary):
        """Fact check before 'done': every number (3+ digits) in the answer must have appeared on a
        page Krify read this task. Returns a rejection message, or None if the answer may go out."""
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
        self.screen_reads = []

    def add_screen_read(self, message, text):
        """Keep only the newest screen text in full. Facts worth keeping go into notes,
        which stay in the chat (as the note calls themselves)."""
        for old in self.screen_reads:
            old["content"] = OLD_SCREEN
        message["content"] = text
        self.messages.append(message)
        self.screen_reads = [message]

    def check_progress(self, name, page_changed):
        """Stuck detector: count steps that don't move the task forward."""
        progress = name in PROGRESS_OPS or (name in ("click", "press_key", "go_back") and page_changed)
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
            tabs = f"[Tabs] {info['tabs']}\n" if info.get("tabs") else ""
            return f"{tabs}[Current page] {info.get('title')} – {info.get('url')}\n{elements}"
        except Exception as err:
            return f"[Current page] (can't read it: {err})"

    def add_with_page(self, message, text, page):
        """Add a message that ends with a page view. Only the newest page view is kept in full."""
        if page == self.last_page:  # nothing changed: don't send the same list again
            message["content"] = f"{text}\n\n{self.header()}\n[Page unchanged since the last view.]"
            self.messages.append(message)
            return
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
        if self.task_id:
            history.finish_task(self.task_id, status, summary, self.tokens, self.model)

    async def run(self, task, model):
        self.idle_steps = 0
        self.tokens = 0
        self.model = model
        self.task_id = history.start_task(task, model)
        self.task_text, self.plan, self.notes = task, [], []
        self.seen = [task]  # numbers the user gave count as known
        self.fact_check_failed = False
        self.current_url = ""
        self.scrolls = {}  # url -> times scrolled on that page
        nudged = False
        self.add_with_page({"role": "user"}, task, await self.page_view())

        for step in range(MAX_STEPS):
            if MAX_STEPS - step == WRAP_UP_AT:
                await self.ui("step", "⏰ Few steps left – asking Krify to wrap up")
                self.messages.append({"role": "user", "content": WRAP_UP})
            reply, used_model, tokens = await ask_ai(
                self.messages, TOOLS, model, on_note=lambda t: self.ui("step", t)
            )
            self.tokens += tokens
            if used_model != model:
                model = used_model  # keep using the model that still has free quota
                self.model = model

            if not reply.tool_calls:  # plain text instead of an operation
                self.messages.append({"role": "assistant", "content": reply.content or ""})
                kind, text, clear = clean_text_reply(reply.content)
                if not clear and not nudged:
                    # Might be nonsense (weak models do this). Ask once for a proper operation.
                    self.messages.append({"role": "user", "content": NUDGE})
                    nudged = True
                    continue
                if kind == "done" and (rejection := self.check_facts(text)):
                    await self.ui("step", "🔍 Fact check: answer has numbers not seen on any page – checking again")
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
            for call in reply.tool_calls:
                name = call.function.name
                tool_msg = {"role": "tool", "tool_call_id": call.id}
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                if finished:
                    self.messages.append({**tool_msg, "content": "Skipped."})
                elif name == "done":
                    summary = args.get("summary", "Done.")
                    if rejection := self.check_facts(summary):
                        await self.ui("step", "🔍 Fact check: answer has numbers not seen on any page – checking again")
                        self.messages.append({**tool_msg, "content": rejection})
                        continue
                    self.messages.append({**tool_msg, "content": "Reported to the user."})
                    await self.finish("done", summary)
                    finished = True
                elif name == "plan":
                    self.plan = [str(s) for s in (args.get("steps") or [])][:8]
                    await self.ui("step", "📋 Plan:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(self.plan, 1)))
                    self.messages.append(
                        {**tool_msg, "content": "Plan saved – you'll see it at every step. Call plan again to "
                                                "update it; start finished steps with ✔."}
                    )
                    self.check_progress(name, False)
                elif name == "ask_user":
                    self.messages.append({**tool_msg, "content": "Question shown. The user's answer comes next."})
                    await self.finish("ask", args.get("question", "Can you tell me more?"))
                    finished = True
                elif name == "note":
                    await self.ui("step", f"📝 {args.get('text', '')}")
                    self.notes.append(str(args.get("text", ""))[:200])
                    self.messages.append({**tool_msg, "content": "Saved."})
                    self.check_progress(name, False)
                elif name in READ_OPS:
                    await self.ui("step", describe(name, args))
                    result = await self.safe_run(name, args)
                    self.check_progress(name, False)
                    self.seen.append(str(result))
                    if name == "get_elements":
                        self.elements = str(result)
                        self.add_with_page(tool_msg, "Elements:", result)
                    elif name == "read_text":
                        self.add_screen_read(tool_msg, f"{result}\n(Save facts you'll need later with note.)")
                    else:
                        self.messages.append({**tool_msg, "content": str(result)})
                else:
                    await self.ui("step", describe(name, args))

                    # Safety check runs in code: risky actions always need the user's OK.
                    reason = safety.check(name, args, self.elements, self.last_typed_line)
                    if reason and not await self.ask_permission(reason, name, args):
                        self.messages.append(
                            {
                                **tool_msg,
                                "content": "BLOCKED by the user – this action was NOT done. "
                                "Don't try it again. Tell the user what's left, or call ask_user.",
                            }
                        )
                        continue

                    if name == "scroll":
                        count = self.scrolls.get(self.current_url, 0)
                        if count >= MAX_SCROLLS_PER_PAGE:
                            await self.ui("step", "🛑 Scroll limit for this page – deciding with what was found")
                            self.messages.append({**tool_msg, "content": SCROLL_REFUSED})
                            self.check_progress(name, False)
                            continue
                        self.scrolls[self.current_url] = count + 1

                    if name == "type":
                        self.last_typed_line = safety.element_line(self.elements, args.get("element"))
                    result = await self.safe_run(name, args)
                    # Show the AI the page after every action, so it can check the result.
                    page = await self.page_view()
                    result = f"{result}{self.check_progress(name, page != self.last_page)}"
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

    async def ask_permission(self, reason, name, args):
        """Show Allow / Block in the side panel. Returns True only if the user allows."""
        line = safety.element_line(self.elements, args.get("element"))
        what = describe(name, args) + (f" – {line}" if line else "")
        allowed = await self.confirm(f"{reason}: {what}")
        await self.ui("step", "✅ Allowed by you" if allowed else "⛔ Blocked")
        return allowed

    async def safe_run(self, name, args):
        try:
            return await self.browser(name, args)
        except Exception as err:
            await self.ui("step", f"⚠️ {err}")
            return f"Error: {err}"
