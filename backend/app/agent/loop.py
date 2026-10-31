"""The agent loop: ask the AI → run the operation in the browser → show it the new page → repeat."""

import json
import logging
import re

from app.agent import safety
from app.agent.prompt import SYSTEM_PROMPT
from app.agent.tools import TOOLS
from app.config import MAX_STEPS
from app.llm.groq_client import ask_ai

log = logging.getLogger("krify")

READ_OPS = {"get_page_info", "get_elements", "read_text"}
OLD_PAGE = "\n[older page view removed to save space]"


def clean_text_reply(text):
    """The model sometimes writes done({...}) / ask_user({...}) as plain text. Pull out the message.

    Returns (kind, message) where kind is "done" or "ask".
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
                    return "ask", str(args["question"])
                if "summary" in args:
                    return "done", str(args["summary"])
        except json.JSONDecodeError:
            pass
    return "done", text or "Done."


def describe(name, args):
    """Readable step text for the side panel, e.g. 'click [12]'."""
    match name:
        case "open_url": return f"🌐 Open {args.get('url')}"
        case "click": return f"🖱️ Click [{args.get('element')}]"
        case "type": return f"⌨️ Type \"{args.get('text')}\" into [{args.get('element')}]"
        case "press_key": return f"⌨️ Press {args.get('key')}"
        case "scroll": return f"↕️ Scroll {args.get('direction')}"
        case "select_option": return f"📋 Choose \"{args.get('option')}\" in [{args.get('element')}]"
        case "go_back": return "⬅️ Go back"
        case "reload": return "🔄 Reload"
        case "wait": return f"⏳ Wait {args.get('seconds')}s"
        case "get_page_info" | "get_elements": return "👀 Look at the page"
        case "read_text": return "📖 Read the page"
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
            await ui(kind, text)

        self.ui = ui_and_log  # async (type, text) -> None
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.page_views = []  # (message, text_before_page_view) – trimmed later to save tokens
        self.last_page = None
        self.tokens = 0

    def reset(self):
        self.messages = self.messages[:1]
        self.page_views = []
        self.last_page = None

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
            return f"[Current page] {info.get('title')} – {info.get('url')}\n{elements}"
        except Exception as err:
            return f"[Current page] (can't read it: {err})"

    def add_with_page(self, message, text, page):
        """Add a message that ends with a page view. Only the newest page view is kept in full."""
        if page == self.last_page:  # nothing changed: don't send the same list again
            message["content"] = f"{text}\n\n[Page unchanged since the last view.]"
            self.messages.append(message)
            return
        for old_msg, old_text in self.page_views:
            old_msg["content"] = old_text + OLD_PAGE
        message["content"] = f"{text}\n\n{page}"
        self.messages.append(message)
        self.page_views = [(message, text)]
        self.last_page = page

    async def finish(self, kind, text):
        """Send the final message, with how many tokens the task used."""
        log.info("Task used %d tokens", self.tokens)
        if kind == "done":
            text = f"{text}\n· used {self.tokens / 1000:.1f}k tokens"
        await self.ui(kind, text)

    async def run(self, task, model):
        self.recent_actions = []
        self.tokens = 0
        self.add_with_page({"role": "user"}, task, await self.page_view())

        for _ in range(MAX_STEPS):
            reply, used_model, tokens = await ask_ai(
                self.messages, TOOLS, model, on_note=lambda t: self.ui("step", t)
            )
            self.tokens += tokens
            if used_model != model:
                model = used_model  # keep using the model that still has free quota

            if not reply.tool_calls:  # plain text: treat as the final answer (or a question)
                self.messages.append({"role": "assistant", "content": reply.content or ""})
                await self.finish(*clean_text_reply(reply.content))
                return

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
                    self.messages.append({**tool_msg, "content": "Reported to the user."})
                    await self.finish("done", args.get("summary", "Done."))
                    finished = True
                elif name == "ask_user":
                    self.messages.append({**tool_msg, "content": "Question shown. The user's answer comes next."})
                    await self.finish("ask", args.get("question", "Can you tell me more?"))
                    finished = True
                elif name in READ_OPS:
                    await self.ui("step", describe(name, args))
                    result = await self.safe_run(name, args)
                    if name == "get_elements":
                        self.elements = str(result)
                        self.add_with_page(tool_msg, "Elements:", result)
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

                    if name == "type":
                        self.last_typed_line = safety.element_line(self.elements, args.get("element"))
                    result = await self.safe_run(name, args)
                    self.recent_actions = (self.recent_actions + [name])[-4:]
                    if self.recent_actions == ["scroll"] * 4:
                        result = (
                            f"{result}\nNote: you have scrolled 4 times in a row without progress. "
                            "Stop scrolling: look for another element that does the job, or call ask_user."
                        )
                    # Show the AI the page after every action, so it can check the result.
                    self.add_with_page(tool_msg, str(result), await self.page_view())

            if finished:
                return

        await self.finish("done", f"Stopped after {MAX_STEPS} steps (safety limit). Try a simpler or more specific task.")

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
