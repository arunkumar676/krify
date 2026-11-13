"""Token trimming: only the newest page view and screen read stay in the chat in full."""

from app.context.header import header
from app.context.sites import site_of

OLD_PAGE = "\n[older page view removed to save space]"
OLD_SCREEN = "[older screen text removed to save space – use your notes]"
MAX_READ_SITES = 3  # newest page text is kept for this many sites

NEW_SITE = ("\n[New site: {}] Check your plan still fits this site. If not, call plan with the fixed "
            "steps (keep ✔ on finished ones).")

UNNOTED_READ = (
    "\n(The text you read before this was dropped – you didn't note anything from it. If you "
    "still need it, note facts right after reading them next time.)"
)


class MemoryMixin:
    """The chat sent to the AI, kept small: only the newest page view, the newest screen read per site
    (MAX_READ_SITES) stay in full; older ones become short placeholders.

    Mixed into Agent (agent/loop.py): uses its connection (browser, ui, confirm, messages, …), its
    TaskState (self.state) and the other layers' methods through self."""

    def reset(self):
        self.messages = self.messages[:1]
        self.page_views = []
        self.last_page = None
        self.screen_reads = {}
        self.last_task = self.last_status = self.gate_tail = ""
        self.notes_at_last_read = 0

    def add_screen_read(self, message, text):
        """Keep the newest screen text of each site in full (at most MAX_READ_SITES sites), so a
        price read on one site is still there while on the next – the AI often forgets to note it."""
        site = site_of(self.state.current_url) or "this page"
        if self.screen_reads and len(self.state.notes) == self.notes_at_last_read:
            text = f"{text}{UNNOTED_READ}"  # the previous read is about to be dropped, unnoted
        old = self.screen_reads.pop(site, None)
        if old:
            old["content"] = OLD_SCREEN
        while len(self.screen_reads) >= MAX_READ_SITES:  # dicts keep order: drop the oldest site
            self.screen_reads.pop(next(iter(self.screen_reads)))["content"] = OLD_SCREEN
        message["content"] = f"[Read from {site}]\n{text}"
        self.messages.append(message)
        self.screen_reads[site] = message
        self.notes_at_last_read = len(self.state.notes)

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

    def add_with_page(self, message, text, page):
        """Add a message that ends with a page view. Only the newest page view is kept in full."""
        if page == self.last_page:  # nothing changed: don't send the same list again
            message["content"] = f"{text}\n\n{header(self.state)}\n[Page unchanged since the last view.]"
            self.messages.append(message)
            return
        site = site_of(self.state.current_url)
        if site and site not in self.state.sites:
            if self.state.plan and self.state.sites:  # the first site was known when planning
                text += NEW_SITE.format(site)
            self.state.sites.add(site)
        for old_msg, old_text in self.page_views:
            old_msg["content"] = old_text + OLD_PAGE
        message["content"] = f"{text}\n\n{header(self.state)}\n{page}"
        self.messages.append(message)
        self.page_views = [(message, text)]
        self.last_page = page
