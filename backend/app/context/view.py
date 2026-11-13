"""The page view the AI reads at every step."""

import re

from app.config import STRUCTURED_VIEW
from app.context import pageview
from app.context.element_list import ambiguity_hint
from app.context.sites import is_sheet
from app.context.understand import understand
from app.guardrails.safety import plan_needs_approval

SHEET_HINT = "[Google Sheet] Call read_text first to see the columns, then sheet_query with SQL."
# Unrestricted mode: the whole page's text rides along with every page view (it's trimmed with it).
PAGE_TEXT = ("\n[Page text] Unrestricted mode: all text on the page, top to bottom (paragraphs, tables, "
             "details) – no need to call read_text for this page.\n{}")

_STATUS_LINE = re.compile(r"^\[Status\] (.*)$", re.MULTILINE)


class ContextMixin:
    """What the AI sees: the page read into a view, and the first plan.

    Mixed into Agent (agent/loop.py): uses its connection (browser, ui, confirm, messages, …), its
    TaskState (self.state) and the other layers' methods through self."""

    async def look(self):
        """Read the page and render the view the AI gets, WITHOUT touching any agent state – so the
        side panel's "AI view" button can show it any time. → (view text, page info, flat element
        list, status line, hint, the bare view without hint/header lines, the open dropdown list
        (field name, [option labels]) or None, the form checklist or None, Unrestricted on?).
        Structured (sections → items → elements, see pageview.py) or the old flat element list,
        depending on STRUCTURED_VIEW."""
        info = await self.browser("get_page_info", {})
        open_list = form = None
        full = inline_text = False
        if STRUCTURED_VIEW:
            view = pageview.render(await self.browser("get_page_model", {}), focus=self.state.task_text)
            self.state.records = view.records or {}
            elements, shown, status, hint = view.flat, view.text, view.status, view.hint
            open_list, form, full, inline_text = view.open_list, view.form, view.full, view.inline_text
        else:
            elements = shown = await self.browser("get_elements", {})
            status = match.group(1) if (match := _STATUS_LINE.search(elements)) else ""
            hint = ambiguity_hint(elements)
        # The page view lists only elements (and item titles); plain text – paragraphs, a table with
        # nothing clickable, a record's detail fields – is otherwise only read with read_text. In
        # Unrestricted mode it's sent too, uncut. Not on Sheets: their text is a canvas, read as CSV.
        text = f"{shown}\n{hint}" if hint else shown
        # (Not needed when the extension already sent the text among the elements – inline_text.)
        if full and not inline_text and not is_sheet(info.get("url", "")):
            page_text = await self.browser_quiet("read_text", {"scope": "page"})
            if isinstance(page_text, str) and not page_text.startswith("(no text"):
                page_text = pageview.clean_page_text(page_text, shown)  # not what the view already says
            if isinstance(page_text, str) and page_text.strip() and not page_text.startswith("(no text"):
                shown += PAGE_TEXT.format(page_text)  # counts as read for the fact check
                text += PAGE_TEXT.format(page_text)
        if is_sheet(info.get("url", "")):
            text = f"{SHEET_HINT}\n{text}"
        tabs = f"[Tabs] {info['tabs']}\n" if info.get("tabs") else ""
        text = f"{tabs}[Current page] {info.get('title')} – {info.get('url')}\n{text}"
        return text, info, elements, status, hint, shown, open_list, form, full

    async def page_view(self):
        """The page view for the next message, recording what the safety/fact checks need."""
        try:
            (text, info, elements, self.state.page_status, hint, shown, self.state.open_list, self.state.form,
             self.state.unrestricted) = await self.look()
        except Exception as err:
            return f"[Current page] (can't read it: {err})"
        # The safety and fact checks get the flat list of EVERY collected element, never just the
        # shortened view; item lines (titles, prices) count as read for the fact check.
        if STRUCTURED_VIEW:
            self.state.seen.append(shown)
        self.state.elements = elements
        self.state.view_text = text
        self.state.seen.append(elements)
        self.state.current_url = info.get("url", "")
        self.state.current_title = str(info.get("title") or "")
        if hint:
            self.state.boost_next = True
        return text

    async def make_plan(self, task, page, model):
        """Plan from the AI's own knowledge before the first action. It never asks the user anything:
        questions come later from the agent, once it has seen the real options on a site."""
        result, tokens = await understand(task, page, self.last_task, model, self.location, on_note=lambda t: self.ui("step", t))
        self.tokens += tokens
        if not result:
            return
        self.state.goal, self.state.plan, self.state.done_when = result["goal"], result["steps"], result["done_when"]
        # A short, harmless plan (go to a page, look something up) needs no sign-off: the agent can act at once.
        self.state.plan_approved = not plan_needs_approval(self.state.plan, task)
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(self.state.plan, 1))
        await self.ui("step", f"🎯 Goal: {self.state.goal}\n📋 Plan:\n{steps}")
