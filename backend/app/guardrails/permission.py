"""Asking the user (Allow / Block) before risky steps, and what the AI is told when a step was blocked."""

import logging

from app.context.element_list import _line_key, element_label
from app.guardrails import safety
from app.tools import sheets
from app.tools.describe import describe

log = logging.getLogger("krify")

OVERWRITE_BLOCKED = (
    "NOT typed: those cells already have data ({}). The user blocked overwriting them. To add a new "
    "row, go_to_cell the first EMPTY row: {}."
)
BLOCKED = ("BLOCKED by the user – this action was NOT done. "
           "Don't try it again. Tell the user what's left, or call ask_user.")

PLAN_NOT_APPROVED = ("Skipped: waiting for the user to approve your plan. If they asked for changes, "
                     "revise it and call plan again.")

# A click that would navigate away from a form the AI has started filling.
LEAVE_FORM = ("NOT done: {what} is outside the form{name} you are filling and would leave it – what you entered{filled} "
              "isn't saved and would be lost. If you meant a field or option of this form, use its number from the "
              "LATEST page view (dropdowns: select_option). Finish the form and save it{submit} first. Only if you "
              "really must leave it unsaved, send this same step again (the user will be asked).")


class GuardrailsMixin:
    """Guards that need the user or the browser: the Allow/Block prompt, leaving an unsaved form,
    overwriting cells in a Google Sheet.

    Mixed into Agent (agent/loop.py): uses its connection (browser, ui, confirm, messages, …), its
    TaskState (self.state) and the other layers' methods through self."""

    async def ask_permission(self, reason, name, args):
        """Show Allow / Block in the side panel. Returns True only if the user allows."""
        line = safety.element_line(self.state.elements, args.get("element"))
        what = describe(name, args) + (f" – {line}" if line else "")
        if name == "type_here" and self.state.last_cell:
            what += f" (cell {self.state.last_cell})"
        allowed = await self.confirm(f"{reason}: {what}")
        await self.ui("step", "✅ Allowed by you" if allowed else "⛔ Blocked")
        return allowed

    async def leave_guard(self, name, args):
        """A step that would leave a form Krify has started filling (a sidebar/header link or other
        link outside it, open_url, back, reload): refused once with an explanation – usually the AI
        used a stale number. Sent again: the user decides (Allow/Block).
        → (refusal text, status) or None to go ahead."""
        edit, form = self.state.form_edit, self.state.form
        if not edit:
            return None
        if edit["url"] != self.state.current_url or not form:
            self.state.form_edit = None  # that form is gone (saved, closed or left already)
            return None
        if name == "click" and args.get("element") is not None and not args.get("cell"):
            where = await self.browser_quiet("leave_check", {"element": args["element"]})
            if not (isinstance(where, dict) and where.get("form") and not where.get("inside") and where.get("navigates")):
                return None
            line = safety.element_line(self.state.elements, args["element"])
            what = f"[{args['element']}] {_line_key(line)}".strip()
        elif name in ("open_url", "go_back", "reload"):
            what = {"open_url": f"opening {args.get('url')}", "go_back": "going back", "reload": "reloading"}[name]
            what = what[0].upper() + what[1:]
        else:
            return None
        key = (name, str(args.get("element") if args.get("element") is not None else args.get("url", "")))
        if key not in self.state.leave_warned:
            self.state.leave_warned.add(key)
            await self.ui("step", "📝 That would leave the unsaved form – sent back")
            filled = [f'{f.get("label") or "[" + str(f.get("id")) + "]"} = "{f.get("value")}"'
                      for f in form.get("fields") or [] if f.get("filled") and f.get("value")]
            filled_text = f" ({', '.join(filled[:4])}{' …' if len(filled) > 4 else ''})" if filled else ""
            submits = [f"[{i}] \"{element_label(self.state.elements, i)}\"" for i in form.get("submits") or []]
            return LEAVE_FORM.format(what=what, name=f' "{form["name"]}"' if form.get("name") else "",
                                     filled=filled_text, submit=f" with {submits[0]}" if submits else ""), "refused"
        if await self.ask_permission("Leaves a form Krify filled in without saving it", name, args):
            return None
        return BLOCKED, "blocked"

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
