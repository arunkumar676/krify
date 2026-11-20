"""Running browser actions for the AI: one step with its guards, a batch of steps, Google Sheets reads."""

import asyncio
import logging

from app.context import pageview
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
from app.tools.form_fill import (
    MAX_FIELDS,
    WAIT_SECONDS,
    WAIT_TRIES,
    after_submit,
    find_field,
    form_errors,
    form_fields,
    step_for,
    submit_button,
    value_ok,
)
from app.tools.ops import ACTION_OPS, ELEMENT_FIELDS, MAX_BATCH, uses_element
from app.verification.changes import change_lines, click_result, same_page
from app.verification.ui_state import add_toasts, blocking_status

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
        reason = safety.check(name, args, self.state.elements, self.state.last_typed_line, self.state.current_url)             or safety.cart_again(name, args, self.state.elements, self.state.actions)
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
            if not result.startswith("Error") and same_page(before[0], self.state.current_url) and (diff := change_lines(self.state.page_changes)):
                log.info("  %s", diff.replace("\n", "\n    "))
                result = f"{result}\n{diff}"
                self.state.page_status = add_toasts(self.state.page_status, self.state.page_changes)
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
        # A step that failed or stopped the batch: the AI gets the whole page, not just the changed lines.
        rough = any(" – stopped here." in r or "FAILED" in r or r.startswith(("Stopped", "Only the first")) for r in report)
        self.add_with_page(tool_msg, "\n".join(report), page or await self.page_view(), full=rough)
        return ran

    async def fill_form(self, tool_msg, args):
        """The fill_form tool: fill every field the AI listed – no AI call in between – then read the form back
        and check each one. Only when every check passes (values shown, no required field empty, no error on the
        page) does it press the form's submit button (the safety Allow/Block prompt still applies), and then
        it reads the page again: a clear success ends the task with no further AI call (unless then_done is
        false). Anything else goes back to the AI once, as one short report plus the page.
        Returns (actions run, task finished)."""
        items = [i for i in (args.get("fields") or []) if isinstance(i, dict) and str(i.get("field") or "").strip()]
        items = items[:MAX_FIELDS]
        submit = args.get("submit")
        submit_text = submit.strip() if isinstance(submit, str) else ""
        wants_submit = bool(submit_text) or submit is True
        if not items:
            self.messages.append({**tool_msg, "content": "Error: no fields given. fields = [{field: the name shown in "
                                                         "[Form], value: what to put in it}]."})
            return 0, False
        await self.ui("step", f"📝 Filling {len(items)} field{'s' if len(items) != 1 else ''} at once"
                              + (" and submitting if all is right" if wants_submit else ""))
        prev, ran, stopped = self.last_page, 0, ""
        outcome = []  # one dict per field: name, value, id, kind, error
        for n, item in enumerate(items):
            name = str(item["field"]).strip()
            value = "" if item.get("value") is None else str(item["value"]).strip()
            entry = {"name": name, "value": value, "id": None, "kind": "text", "error": ""}
            outcome.append(entry)
            if n:
                await self.page_view()  # the page after the last step: numbers may have moved
            taken = {str(o["id"]) for o in outcome if o["id"] is not None}
            found = find_field(self.state, name, taken)
            for _ in range(WAIT_TRIES):  # a dropdown that opens only after an earlier choice (State after Country)
                if not (found and found[1] and found[1].get("disabled")):
                    break
                await asyncio.sleep(WAIT_SECONDS)
                await self.page_view()
                found = find_field(self.state, name, taken)
            if not found:
                entry["error"] = "no field with that name on the page"
                continue
            entry["id"], field, entry["kind"] = found
            if field and field.get("disabled"):
                entry["error"] = "still disabled (it opens after an earlier field is chosen)"
                continue
            op, step = step_for(self.state, entry["id"], field, entry["kind"], value)
            if op is None:
                entry["error"] = step
                continue
            if op == "skip":
                continue
            result, status = await self.do_action(op, step)
            if status == "blocked":
                entry["error"] = "blocked by you"
                stopped = f"Stopped at {name}: blocked by the user – the fields after it were not filled."
                break
            if status != "ok" or result.startswith("Error"):
                entry["error"] = result[:300]
                continue
            ran += 1

        # Read the form back and check every field – in code, not by the AI.
        page = await self.page_view()
        report, bad = [], 0
        fresh = {str(f.get("id")): f for f in form_fields(self.state)}
        for entry in outcome:
            line = safety.element_line(self.state.elements, entry["id"]) if entry["id"] is not None else ""
            secret = "password" in line or (fresh.get(str(entry["id"])) or {}).get("value") == "••••"
            shown_value = "••••" if secret else entry["value"]
            if entry["error"]:
                bad += 1
                report.append(f'✗ {entry["name"]} = "{shown_value}": {entry["error"]}')
                continue
            if entry["id"] is None:  # not reached (stopped earlier)
                bad += 1
                report.append(f'✗ {entry["name"]}: not filled')
                continue
            field = fresh.get(str(entry["id"]))
            if value_ok(field, entry["kind"], entry["value"], line):
                report.append(f'✓ {entry["name"]} = "{shown_value}"')
            else:
                bad += 1
                now = "••••" if secret else ((field or {}).get("value") or "")
                report.append(f'✗ {entry["name"]}: shows "{now}", not "{shown_value}"')
        missing = pageview.missing_required(self.state.form)
        problems = blocking_status(self.state.page_status)
        errors = form_errors(self.state)
        if missing:
            report.append(f"Required and still empty: {', '.join(missing)}")
        if errors or problems:
            report.append(f"The page shows: {'; '.join(errors) or problems}")
        all_ok = not stopped and not bad and not missing and not errors and not problems
        await self.ui("step", f"✅ All {len(outcome)} fields filled and checked" if all_ok
                      else "📝 Form check:\n" + "\n".join(report))
        if stopped:
            report.append(stopped)

        if wants_submit and all_ok:
            button = submit_button(self.state, submit_text)
            line = safety.element_line(self.state.elements, button) if button is not None else ""
            if button is None:
                named = f' "{submit_text}"' if submit_text else ""
                report.append(f"Not submitted: no submit button{named} found – click the form's own Save/Submit button.")
            elif "disabled" in line:
                report.append(f"Not submitted: [{button}] {line} is disabled – something is still missing.")
            else:
                before_url, before_form = self.state.current_url, self.state.form
                label = element_label(self.state.elements, button)
                result, status = await self.do_action("click", {"element": button, "label": label})
                if status != "ok" or result.startswith("Error"):
                    report.append(f'Not submitted ("{label}"): {result[:300]}')
                else:
                    ran += 1
                    page = await self.page_view()
                    self.state.page_status = add_toasts(self.state.page_status, self.state.page_changes)
                    verdict, said = after_submit(before_url, before_form, self.state, blocking_status(self.state.page_status))
                    if diff := change_lines(self.state.page_changes):
                        result = f"{result}\n{diff}"
                    if verdict == "ok" and args.get("then_done", True) is not False:
                        name = (before_form or {}).get("name") or label
                        summary = (f'Done – filled {len(outcome)} field{"s" if len(outcome) != 1 else ""} and submitted '
                                   f'"{name}".' + (f' The page says: "{said}"' if said else ""))
                        self.messages.append({**tool_msg, "content": "\n".join(report) + f"\nSubmitted with \"{label}\" – "
                                                                     "the page confirms it. The task is finished."})
                        check_progress(self.state, "fill_form", True)
                        await self.finish("done", summary, verify=False)
                        return ran, True
                    if verdict == "ok":
                        report.append(f'Submitted with "{label}" – it went through{f": {said}" if said else ""}. '
                                      "Go on with the next step of your plan.")
                    elif verdict == "failed":
                        report.append(f'Clicked "{label}", but NOT saved – the page shows: {said}. Fix those fields '
                                      "(fill_form again with just them, and submit), or ask_user.")
                    else:
                        report.append(f'Clicked "{label}". The page doesn\'t clearly say it worked – look at it, then '
                                      "call done or fix what's wrong.\n" + result)
        elif wants_submit:
            report.append("Not submitted, because of the ✗ / missing / error lines above: fix only those (fill_form "
                          "with just them, and submit), or ask_user for what's missing.")
        elif all_ok:
            report.append("All fields filled and checked. Not submitted (no submit asked).")
        check_progress(self.state, "fill_form", page != prev)
        self.add_with_page(tool_msg, "\n".join(report), page, full=True)
        return ran, False

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
