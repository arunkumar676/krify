"""Running browser actions for the AI: one step with its guards, a batch of steps, Google Sheets reads."""

import logging

from app.context.element_list import _line_key, element_label, layout
from app.context.sites import is_sheet
from app.guardrails import safety
from app.guardrails.element_match import CHANGED_IN_BATCH, NOT_IN_VIEW, wrong_element
from app.guardrails.forms import form_not_sent, required_empty, track_form_edit
from app.guardrails.permission import BLOCKED, OVERWRITE_BLOCKED
from app.guardrails.progress import (
    MAX_IDLE_SCROLLS,
    MAX_SCROLLS_PER_PAGE,
    SCROLL_REFUSED,
    check_loop,
    check_progress,
    scroll_key,
    scrolled,
)
from app.tools import sheets
from app.tools.describe import describe
from app.tools.ops import ACTION_OPS, ELEMENT_FIELDS, MAX_BATCH, uses_element
from app.verification.changes import click_result

log = logging.getLogger("krify")


class ToolsMixin:
    """Running the AI's browser actions: one step (checked by the guardrails first), the batch tool,
    Google Sheets reads (CSV + DuckDB) and the low-level browser call wrappers.

    Mixed into Agent (agent/loop.py): uses its connection (browser, ui, confirm, messages, …), its
    TaskState (self.state) and the other layers' methods through self."""

    async def do_action(self, name, args, seen=None):
        """One browser action, with the safety check and the scroll limit.
        seen: for the 2nd+ step of a batch, the element list the AI sent the batch from (its numbers
        are checked against that view, not just the newest one).
        Returns (result text, status): status "ok", "blocked" (by the user) or "refused" (scroll limit)."""
        label = describe(name, args)
        # Say WHAT was clicked, not just its number: "Click [24] · link "People"" (numbers change per page).
        target = next((args[f] for f in ELEMENT_FIELDS if args.get(f) is not None), None)
        if target is not None and (line := safety.element_line(self.state.elements, target)):
            label += f" · {line if len(line) <= 70 else line[:70] + '…'}"
        await self.ui("step", label)
        if self.dev_mode:
            self.trace.append(label)

        # Typing into a sheet over cells that already hold data: always ask, showing what would be lost.
        if name == "type_here" and is_sheet(self.state.current_url):
            in_the_way, empty_row = await self.cells_in_the_way(str(args.get("text", "")))
            if in_the_way:
                keep = f" Block to keep them (Krify will use the first empty row, {empty_row}, instead)." if empty_row else ""
                allowed = await self.confirm(f"⚠️ Overwriting: {in_the_way}.{keep}")
                await self.ui("step", "✅ Allowed by you" if allowed else "⛔ Blocked")
                if not allowed:
                    return OVERWRITE_BLOCKED.format(in_the_way, empty_row or "last _row + 1"), "blocked"
                self.state.edit_allowed = True  # they saw exactly what changes

        for field in ELEMENT_FIELDS:  # the page may have changed since the model looked – check first
            element_id = args.get(field)
            if element_id is None:
                continue
            expected = safety.element_line(self.state.elements, element_id)
            if seen is not None:  # a later step of a batch: was this number in the view it was sent from?
                composed = safety.element_line(seen, element_id)
                if not composed:
                    return NOT_IN_VIEW.format(element_id), "refused"
                if expected and _line_key(composed) != _line_key(expected):
                    return CHANGED_IN_BATCH.format(element_id, _line_key(expected), _line_key(composed)), "refused"
            if not expected:
                continue  # nothing recorded to compare against – not our problem to flag
            live = str(await self.safe_run("describe_element", {"element": element_id}))
            if live != expected:
                now = f', now "{live}"' if live else ", now gone"
                return (f'NOT done: [{element_id}] has changed since you looked (expected "{expected}"{now}) '
                        "– get the elements again."), "refused"
            if field == "element" and (refusal := wrong_element(self.state, element_id, args.get("label"))):
                await self.ui("step", f"🔢 [{element_id}] isn't \"{args.get('label')}\" any more – sent back")
                return refusal, "refused"
        if leaving := await self.leave_guard(name, args):
            return leaving
        # Enter sends whatever form the field is in: look at that form's own button (e.g. "Place order").
        if (name == "press_key" and str(args.get("key", "")).strip().lower() == "enter") or (name == "type" and args.get("submit")):
            target = await self.browser_quiet("enter_target", {"element": args.get("element") if name == "type" else None})
            if isinstance(target, dict):
                args = {**args, "_enter": target}
        if refusal := required_empty(self.state, name, args):
            await self.ui("step", "📝 Required fields still empty – filling those first")
            return refusal, "refused"
        reason = safety.check(name, args, self.state.elements, self.state.last_typed_line, self.state.current_url)
        if reason == "Editing" and self.state.edit_allowed:
            reason = None  # editing this document was already allowed in this task
        if reason and not await self.ask_permission(reason, name, args):
            return BLOCKED, "blocked"
        if reason == "Editing":
            self.state.edit_allowed = True
        if name == "go_to_cell":
            self.state.last_cell = str(args.get("cell", ""))

        if name == "scroll":
            # Scrolling a box (embedded page, chat list) has its own limit, apart from the page's.
            key = scroll_key(self.state, args)
            done = self.state.scrolls.setdefault(key, {"total": 0, "idle": 0})
            why = (f"the last {done['idle']} scrolls on this page showed nothing new." if done["idle"] >= MAX_IDLE_SCROLLS
                   else f"you already scrolled this page {done['total']} times." if done["total"] >= MAX_SCROLLS_PER_PAGE else "")
            if why:
                await self.ui("step", "🛑 Scroll limit for this page – deciding with what was found")
                return SCROLL_REFUSED.format(why), "refused"
            done["total"] += 1
            done.setdefault("views", set()).add(hash(self.last_page))  # where it scrolls from counts as seen

        if name == "type":
            self.state.last_typed_line = safety.element_line(self.state.elements, args.get("element"))
        result = str(await self.safe_run(name, args))
        if not result.startswith("Error"):
            self.state.actions.append((name, {k: v for k, v in args.items() if not k.startswith("_")}, result))
            track_form_edit(self.state, name, args)
        return result, "ok"

    async def run_batch(self, tool_msg, steps):
        """The batch tool: several actions in a row, the page shown once at the end. Stops at a
        blocked or failed step, or when the page changes (element numbers may have moved).
        Returns how many actions ran."""
        steps = [s for s in (steps or []) if isinstance(s, dict)]
        count = min(len(steps), MAX_BATCH)
        await self.ui("step", f"📦 {count} step{'s' if count != 1 else ''} at once")
        report, page, prev, ran = [], None, self.last_page, 0
        seen = self.state.elements  # the element list the AI saw when it sent the batch
        for n, step in enumerate(steps[:MAX_BATCH], 1):
            op = step.get("op")
            args = {k: v for k, v in step.items() if k != "op"}
            if op not in ACTION_OPS:
                report.append(f"{n}. '{op}' can't be used in batch – stopped here.")
                break
            label = element_label(self.state.elements, args.get("element"))
            before, open_list = (self.state.current_url, self.state.elements), self.state.open_list
            clicked = safety.element_line(self.state.elements, args.get("element")) if op == "click" else ""
            result, status = await self.do_action(op, args, seen=seen if ran else None)
            if status != "ok":
                report.append(f"{n}. {result}")
                break
            ran += 1
            page = await self.page_view()
            if op == "click" and not result.startswith("Error"):
                if sentence := click_result(before, (self.state.current_url, self.state.elements), clicked,
                                            self.state.current_title, open_list):
                    log.info("  told the AI: %s", sentence)
                    result = f"{result}\n{sentence}"
            if not_sent := form_not_sent(op, args, label, self.state.page_status):
                report.append(f"{n}. {result}{not_sent} – stopped here.")
                break
            new_page, loop_note = check_loop(self.state, page, page != prev, self.dev_mode, self.trace)
            if op == "scroll":
                scrolled(self.state, args, page)
            check_progress(self.state, op, new_page)
            if loop_note:
                report.append(f"{n}. {result}{loop_note} – stopped here.")
                break
            if result.startswith("Error"):
                report.append(f"{n}. {describe(op, args)} FAILED: {result} – stopped here.")
                break
            report.append(f"{n}. {result} ✓")
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
                return sheets.cells(data.get("csv", ""), str(args.get("cells", "")), self.state.unrestricted)
            return sheets.query(data.get("csv", ""), args.get("sql", ""), self.state.unrestricted)
        except Exception as err:
            await self.ui("step", f"⚠️ {err}")
            return f"Error: {err}"

    async def browser_quiet(self, name, args):
        """A browser call whose failure doesn't matter (None instead of an error step in the panel)."""
        try:
            return await self.browser(name, args)
        except Exception:
            return None

    async def safe_run(self, name, args):
        try:
            return await self.browser(name, args)
        except Exception as err:
            await self.ui("step", f"⚠️ {err}")
            return f"Error: {err}"
