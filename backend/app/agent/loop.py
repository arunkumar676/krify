"""The agent loop: ask the AI → run the operation in the browser → show it the new page → repeat."""

import json
import re

from app.agent.prompt import SYSTEM_PROMPT
from app.agent.tools import TOOLS
from app.config import MAX_STEPS
from app.llm.groq_client import ask_ai

READ_OPS = {"get_page_info", "get_elements", "read_text"}
OLD_PAGE = "\n[older page view removed to save space]"


def clean_text_reply(text):
    """The model sometimes writes done({...}) / ask_user({...}) as plain text. Pull out the message.

    Returns (kind, message) where kind is "done" or "ask".
    """
    match = re.search(r"\b(done|ask_user)\s*\(\s*(\{.*\})\s*\)", text or "", re.DOTALL)
    if match:
        try:
            args = json.loads(match.group(2))
            if match.group(1) == "ask_user":
                return "ask", args.get("question", text)
            return "done", args.get("summary", text)
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

    def __init__(self, browser, ui):
        self.browser = browser  # async (name, args) -> str, raises on failure
        self.ui = ui  # async (type, text) -> None
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.page_views = []  # (message, text_before_page_view) – trimmed later to save tokens
        self.stopped = False

    def reset(self):
        self.messages = self.messages[:1]
        self.page_views = []

    async def page_view(self):
        """Current URL, title and numbered elements."""
        try:
            info = await self.browser("get_page_info", {})
            elements = await self.browser("get_elements", {})
            return f"[Current page] {info.get('title')} – {info.get('url')}\n{elements}"
        except Exception as err:
            return f"[Current page] (can't read it: {err})"

    def add_with_page(self, message, text, page):
        """Add a message that ends with a page view; shorten older page views."""
        for old_msg, old_text in self.page_views:
            old_msg["content"] = old_text + OLD_PAGE
        message["content"] = f"{text}\n\n{page}"
        self.messages.append(message)
        self.page_views = [(message, text)]

    async def run(self, task):
        self.stopped = False
        self.add_with_page({"role": "user"}, task, await self.page_view())

        for _ in range(MAX_STEPS):
            if self.stopped:
                await self.ui("stopped", "Stopped.")
                return

            reply = await ask_ai(self.messages, TOOLS, on_wait=lambda t: self.ui("step", t))

            if not reply.tool_calls:  # plain text: treat as the final answer (or a question)
                self.messages.append({"role": "assistant", "content": reply.content or ""})
                await self.ui(*clean_text_reply(reply.content))
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

                if finished or self.stopped:
                    self.messages.append({**tool_msg, "content": "Skipped."})
                elif name == "done":
                    self.messages.append({**tool_msg, "content": "Reported to the user."})
                    await self.ui("done", args.get("summary", "Done."))
                    finished = True
                elif name == "ask_user":
                    self.messages.append({**tool_msg, "content": "Question shown. The user's answer comes next."})
                    await self.ui("ask", args.get("question", "Can you tell me more?"))
                    finished = True
                elif name in READ_OPS:
                    await self.ui("step", describe(name, args))
                    result = await self.safe_run(name, args)
                    if name == "get_elements":
                        self.add_with_page(tool_msg, "Elements:", result)
                    else:
                        self.messages.append({**tool_msg, "content": str(result)})
                else:
                    await self.ui("step", describe(name, args))
                    result = await self.safe_run(name, args)
                    # Show the AI the page after every action, so it can check the result.
                    self.add_with_page(tool_msg, str(result), await self.page_view())

            if finished:
                return

        await self.ui("done", f"Stopped after {MAX_STEPS} steps (safety limit). Try a simpler or more specific task.")

    async def safe_run(self, name, args):
        try:
            return await self.browser(name, args)
        except Exception as err:
            await self.ui("step", f"⚠️ {err}")
            return f"Error: {err}"
