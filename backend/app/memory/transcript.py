"""Token trimming: what the chat sent to the AI holds is bounded – only the newest page view, the newest
screen read per site and the newest few fact results stay in full; older tool results shrink to one line,
and earlier tasks to a short digest."""

from app.config import VIEW_MODE
from app.context.delta import MAX_DELTAS, PAGE_TEXT_FULL, REVERTED, UNCHANGED, same_view, view_delta
from app.context.header import header
from app.context.sites import site_of

HYBRID = VIEW_MODE == "hybrid"  # context/delta.py: a small page change is sent as its changed lines

OLD_PAGE = "\n[older page view removed to save space]"
OLD_SCREEN = "[older screen text removed to save space – use your notes]"
OLD_DELTA = " […] [newer changes below]"  # an earlier delta, replaced by the next one (which also holds its lines)
MAX_READ_SITES = 3  # newest page text is kept for this many sites
BRIEF_CHARS = 160  # an older tool result keeps its first line, at most this long
TRIMMED = " […]"  # marks a result that was cut short
MAX_KEPT = 3  # newest fact results (find_text hit, sheet answer, list records) kept in full
MAX_PAST = 5  # earlier tasks of this chat named in the digest
PAST_TASK_CHARS = 200
PAST_SUMMARY_CHARS = 200  # an earlier task's answer in the digest …
LAST_SUMMARY_CHARS = 1200  # … and the newest one's, kept longer so "open the second one" still works
PAST_HEAD = ("[Earlier in this chat – oldest first. Their page views and tool results are no longer kept: "
             "look at the current page, or read it again, if you need details.]")
PAST_STATUS = {"done": "done", "stopped": "stopped by the user", "error": "failed"}

NEW_SITE = ("\n[New site: {}] Check your plan still fits this site. If not, call plan with the fixed "
            "steps (keep ✔ on finished ones).")

UNNOTED_READ = (
    "\n(The text you read before this was dropped – you didn't note anything from it. If you "
    "still need it, note facts right after reading them next time.)"
)


def brief(text):
    """The first line of an older tool result (at most BRIEF_CHARS), marked when something was cut."""
    text = str(text or "")
    if text.endswith(TRIMMED):
        return text
    line = text.split("\n", 1)[0]
    if len(line) > BRIEF_CHARS:
        line = line[:BRIEF_CHARS] + "…"
    return line if line == text else line + TRIMMED


def _one_line(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def digest(past):
    """The earlier tasks of this chat as one short message: (task, status, summary) tuples, oldest first."""
    lines = [PAST_HEAD]
    for n, (task, status, summary) in enumerate(past, 1):
        if n == len(past):  # the newest answer: longer, and its list lines kept
            summary = str(summary or "").strip()
            if len(summary) > LAST_SUMMARY_CHARS:
                summary = summary[:LAST_SUMMARY_CHARS] + "…"
        else:
            summary = _one_line(summary, PAST_SUMMARY_CHARS)
        lines.append(f'{n}. "{_one_line(task, PAST_TASK_CHARS)}" → {PAST_STATUS.get(status, status)}: {summary}')
    return "\n".join(lines)


class MemoryMixin:
    """The chat sent to the AI, kept small: only the newest page view, the newest screen read per site
    (MAX_READ_SITES) and the newest fact results (MAX_KEPT) stay in full; older tool results keep one
    line, and a new task starts from a digest of the earlier ones (forget_old_tasks).

    Mixed into Agent (agent/loop.py): uses its connection (browser, ui, confirm, messages, …), its
    TaskState (self.state) and the other layers' methods through self."""

    def reset(self):
        self.messages = self.messages[:1]
        self.page_views = []
        self.last_page = None
        self.screen_reads = {}
        self.last_task = self.last_status = self.gate_tail = ""
        self.notes_at_last_read = 0
        self.kept = []
        self.view_at = None
        self.past = []
        self.forget_view()

    def forget_view(self):
        """Nothing sent so far counts as the AI's last full view: the next page view goes out in full."""
        self.view_base = None  # the last FULL page view sent: what a delta is the difference from
        self.view_url = ""
        self.deltas = 0  # views since then sent as a delta
        self.delta_msgs = []  # the messages that carried them
        self.last_header = None  # the header the AI last got (hybrid sends it again only when it changed)
        self.pending_seen = None  # (page, [texts]) from the last page_view(), counted as read once it is sent

    def forget_old_tasks(self):
        """A new task: the earlier tasks' messages become one digest message, so a request costs what this
        task needs, not what the chat has piled up. (Not for a message that answers ask_user.)"""
        self.messages = self.messages[:1]
        if self.past:
            self.messages.append({"role": "user", "content": digest(self.past)})
        self.page_views = []
        self.last_page = None  # the first page view of the task is always sent in full
        self.forget_view()
        self.screen_reads = {}
        self.kept = []
        self.view_at = None
        self.notes_at_last_read = 0

    def remember_task(self, status, summary):
        """A task that reached the agent ended (not with a question): one line for the next tasks' digest."""
        self.past = (self.past + [(self.state.task_text, status, summary)])[-MAX_PAST:]

    def keep_fact(self, message, text=None):
        """A result holding facts the AI asked for (a find_text hit, a sheet answer, list records): the
        newest MAX_KEPT stay in full, an older one keeps its first line."""
        self.kept.append((message, message["content"] if text is None else text))
        while len(self.kept) > MAX_KEPT:
            old, old_text = self.kept.pop(0)
            if self.page_views and self.page_views[0][0] is old:
                self.page_views = [(old, brief(old_text))]  # still the newest page view: cut when replaced
            elif old["content"].endswith(OLD_PAGE):
                old["content"] = brief(old_text) + OLD_PAGE
            else:
                old["content"] = brief(old_text)

    def shrink_old_results(self):
        """A new page view is coming: the tool results since the last one have done their job (the AI has
        replied to them) – each keeps its first line. Screen reads and kept facts follow their own rules."""
        spared = {id(m) for m in self.screen_reads.values()} | {id(m) for m, _ in self.kept}
        for msg in self.messages[(self.view_at if self.view_at is not None else 0) + 1:]:
            if msg.get("role") == "tool" and id(msg) not in spared and isinstance(msg.get("content"), str):
                msg["content"] = brief(msg["content"])

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

    def add_with_page(self, message, text, page, full=False):
        """Add a message that ends with a page view. Only the newest page view is kept in full.
        VIEW_MODE=hybrid: a page that differs from the last full view in a few lines is sent as those
        lines (context/delta.py) – unless `full` (a failed step, an explicit refresh), the address changed,
        or MAX_DELTAS views in a row went out as deltas."""
        if not HYBRID:
            return self._add_page(message, text, page)
        header_now = header(self.state)
        resend = header_now != self.last_header  # the AI still holds the one it was sent last
        delta = None
        if self.last_page is not None and same_view(page, self.last_page):
            delta = UNCHANGED
        elif (not full and self.view_base is not None and self.deltas < MAX_DELTAS
                and self.state.current_url == self.view_url):
            delta = view_delta(self.view_base, page)
            if delta == UNCHANGED:  # back to the full view the AI holds, after changes it was sent since
                delta = REVERTED
        if delta is None:
            return self._add_page(message, text, page)
        head = f"\n\n{header_now}" if resend else ""
        self.last_header = header_now
        message["content"] = f"{text}{head}\n{delta}"
        if delta != UNCHANGED:
            spared = {id(m) for m, _ in self.kept}
            for old in self.delta_msgs:  # a new delta holds the earlier ones' lines too (same view), a revert voids them
                if id(old) not in spared and not old["content"].endswith(OLD_DELTA):
                    old["content"] = brief(old["content"]).removesuffix(TRIMMED) + OLD_DELTA
            self.last_page = page
            if delta == REVERTED:
                self.delta_msgs = []
            else:
                self.delta_msgs.append(message)
                self.deltas += 1
                self.count_seen(page, delta)
        self.messages.append(message)

    def count_seen(self, page, sent):
        """Hybrid: the fact check counts as read what the AI was sent – the full view (what page_view() collected,
        its flat element list included), or just the delta's lines. A page view that was never sent
        (a step in the middle of a batch) counts for nothing."""
        pending, self.pending_seen = self.pending_seen, None
        if sent is None:
            if pending and pending[0] == page:
                self.state.seen.extend(pending[1])
        else:
            self.state.seen.append(sent)

    def _add_page(self, message, text, page):
        """The whole page view goes out: older ones shrink, and this one becomes the one a delta is against."""
        if page == self.last_page:  # nothing changed: don't send the same list again
            message["content"] = f"{text}\n\n{header(self.state)}\n[Page unchanged since the last view.]"
            self.messages.append(message)
            return
        site = site_of(self.state.current_url)
        if site and site not in self.state.sites:
            if self.state.plan and self.state.sites:  # the first site was known when planning
                text += NEW_SITE.format(site)
            self.state.sites.add(site)
        self.shrink_old_results()
        spared = {id(m) for m, _ in self.kept}
        for old_msg, old_text in self.page_views:
            # The user's own message and a kept fact stay whole; an action's result keeps its first line.
            whole = old_msg.get("role") != "tool" or id(old_msg) in spared
            old_msg["content"] = (old_text if whole else brief(old_text)) + OLD_PAGE
        message["content"] = f"{text}\n\n{header(self.state)}\n{page}"
        self.messages.append(message)
        self.view_at = len(self.messages) - 1
        self.page_views = [(message, text)]
        self.last_page = page
        if HYBRID:
            self.view_base, self.view_url, self.deltas, self.delta_msgs = page, self.state.current_url, 0, []
            self.last_header = header(self.state)
            self.count_seen(page, None)
            if PAGE_TEXT_FULL in page:  # this page's text went out with the view: later views of it only say so
                self.state.text_urls.add(self.state.current_url)
