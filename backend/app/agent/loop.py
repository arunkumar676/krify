"""The agent loop: ask the AI → run the operation in the browser → show it the new page → repeat."""

import json
import logging
import re

from app import history
from app.agent.state import TaskState
from app.config import MAX_STEPS
from app.context.element_list import element_label, layout, merge_elements
from app.context.pageview import get_records
from app.tools.schemas import ALL_GROUPS, GROUP_ORDER, TOOL_GROUPS, task_groups, tools_for
from app.context.prompt import TOPICS, page_topics, system_prompt, task_topics, where_line
from app.context.sites import is_sheet
from app.context.view import ContextMixin
from app.guardrails import safety
from app.guardrails.forms import form_not_sent
from app.guardrails.permission import PLAN_NOT_APPROVED, GuardrailsMixin
from app.guardrails.progress import (
    SAME_READ,
    WRAP_UP,
    WRAP_UP_AT,
    check_loop,
    check_progress,
    check_repeat,
    scrolled,
    start_trail,
)
from app.llm import report
from app.llm.client import ask_ai, skip_model
from app.memory.transcript import MemoryMixin
from app.output.answer import OutputMixin, preview
from app.output.text_reply import clean_text_reply
from app.tools.actions import ToolsMixin
from app.tools.describe import describe
from app.tools.ops import (
    ACTION_OPS,
    BATCH_LIMIT,
    BATCH_REFUSED,
    MAX_BATCH,
    PAGE_MOVED,
    READ_OPS,
    SHEET_OPS,
    STEP_FAILED,
    _args_of,
    uses_element,
)
from app.verification.changes import change_lines, click_result, opened_what_asked, same_page
from app.verification.claims import check_claims, check_handoff
from app.verification.facts import WRONG_PAGE, WRONG_PRODUCT, check_facts, missing_models, missing_words
from app.verification.filters import FILTER_HINT, check_filters, filter_dropdowns
from app.verification.ui_state import blocking_status, check_ui_state

log = logging.getLogger("krify")

DONE_TOO_EARLY ="Not sent: check the page after your steps first, then call done."
SAME_PLAN = "That's the same plan you already have – no need to repeat it. Continue with the next step."
NUDGE = (
    "Reminder: reply by calling one operation. If the task is finished, call done with a summary; "
    "if you need something from the user, call ask_user."
)


class Agent(ToolsMixin, GuardrailsMixin, ContextMixin, MemoryMixin, OutputMixin):
    """One agent per side-panel connection. Keeps the chat so follow-ups work."""

    def __init__(self, browser, ui, confirm, plan_review, client=""):
        self.client = client  # which extension install this is (history is kept per install)
        self.browser = browser  # async (name, args) -> str, raises on failure
        self.confirm = confirm  # async (text) -> bool: ask the user Allow / Block
        self.plan_review = plan_review  # async (steps) -> (action, steps): Approve / Update / Reject a plan
        self.state = TaskState()  # the task in progress and the page it's on (agent/state.py)
        # Sheets-only tools are sent only once a Google Sheet has been open (then always, since the
        # chat history may hold calls to them, and a call to a tool not sent is rejected by the API).
        # Tool groups sent to the model (tools/schemas.py TOOL_GROUPS): None until the first task; only grow
        # during a chat (its history may hold calls to any of them). router_groups: what the gate picked.
        self.tool_groups = None
        self.router_groups = None
        # Prompt topics sent (context/prompt.py TOPICS, e.g. the shopping rules): None until the first task, then
        # only grow, like tool groups.
        self.topics = None
        self.dev_mode = False  # debug trace (tools used + what they found) appended to the answer
        self.trace = []  # this task's trace, built only while dev_mode is on

        async def ui_and_log(kind, text):
            log.info("[%s] %s", kind, text)
            if self.task_id:
                history.add_step(self.task_id, kind, text)
            await ui(kind, text)

        self.task_id = None  # database id of the running task
        self.location = None  # where the user is, sent by the side panel with each task
        self.model = None  # model the task is currently using

        self.ui = ui_and_log  # async (type, text) -> None
        self.messages = [{"role": "system", "content": system_prompt()}]
        self.page_views = []  # (message, text_before_page_view) – trimmed later to save tokens
        self.last_page = None
        self.screen_reads = {}  # site -> its newest read_text result (older ones are trimmed)
        self.notes_at_last_read = 0  # len(state.notes) as of the last screen read – for the unnoted-read nudge
        self.tokens = 0
        self.last_task = ""  # previous task, so the planner understands follow-ups
        self.last_status = ""  # how the previous task ended ("ask" = the next message is an answer)
        self.gate_tail = ""  # the gate's last chat exchange, so its follow-ups make sense (context/router.py)

    def page_groups(self):
        """Tool groups the current page needs, whatever the gate picked: a Sheet/Doc, a form, a long list,
        hover-only buttons, more than one tab."""
        url = self.state.current_url or ""
        view = self.state.view_text or ""
        groups = set()
        if is_sheet(url) or "docs.google.com/document" in url:
            groups.add("sheets")
        if self.state.form:
            groups.add("forms")
        if "get_records(" in view:
            groups.add("lists")
        if "(hover" in view or ", hover" in view:
            groups.add("mouse")
        if "[Tabs]" in view:
            groups.add("tabs")
        if re.search(r"youtube\.com/(watch|shorts)|vimeo\.com/\d|/learn/lecture/|netflix\.com/watch|hotstar\.com", url):
            groups.add("media")  # a video page: the video tool works even when the player hides its buttons
        return groups

    def refresh_system(self):
        """Set the system message for the tool groups sent now (plus where the user is): a group's rules go
        out exactly when its tools do. Called whenever tool_groups changes, i.e. when the tool list the
        provider caches against changes anyway."""
        where = where_line(self.location)
        prompt = system_prompt(self.tool_groups, self.topics)
        self.messages[0]["content"] = f"{prompt}\n\n{where}" if where else prompt

    async def stop_looping(self):
        """Still going in circles after a warning: stop and ask the user instead of burning steps."""
        question, self.state.loop_question = self.state.loop_question, ""
        await self.ui("step", "🔁 Going in circles – stopping to ask you")
        await self.finish("ask", question)

    async def review_plan(self, steps):
        """Ask the user to Approve / Update / Reject `steps`. Returns (decision, text for the AI)."""
        decision, edited = await self.plan_review(steps)
        self.state.plan = edited
        shown = "\n".join(f"{i}. {s}" for i, s in enumerate(edited, 1))
        if decision == "approve":
            self.state.plan_approved = True
            await self.ui("step", f"✅ Plan approved:\n{shown}")
            return decision, f"Plan approved by the user:\n{shown}\nProceed with it."
        if decision == "reject":
            return decision, "Plan rejected by the user."
        await self.ui("step", f"✏️ You edited the plan – checking it with Krify:\n{shown}")
        return "update", (f"The user edited your plan to:\n{shown}\nReview it, adjust "
                          "if needed, and call plan again to re-propose it.")

    async def run(self, task, model, location=None):
        self.location = location
        # The user's message may answer the agent's ask_user: then carry on with the same task.
        continuing = self.last_status == "ask"
        self.state.start(task, continuing)
        self.tokens = 0
        self.trace = []
        self.model = model
        self.task_id = history.start_task(task, model, self.client)
        report.set_task(self.task_id)
        if not continuing and await self.try_gate(task):  # chat / general knowledge: no agent needed
            return
        if not continuing or self.tool_groups is None:
            wanted = ALL_GROUPS if self.router_groups is None else self.router_groups
            self.tool_groups = (self.tool_groups or set()) | {"basic"} | set(wanted) | task_groups(task)
            # (No gate answer = unknown task: every topic's rules are sent, as with the tool groups.)
            wanted_topics = set(TOPICS) if self.router_groups is None else task_topics(task)
            self.topics = (self.topics or set()) | wanted_topics
        self.refresh_system()
        self.notes_at_last_read = 0
        nudged = False
        empty_replies = 0
        page = await self.page_view()
        start_trail(self.state, page)
        plan_note = ""
        if not continuing:
            await self.make_plan(task, page, model)
            if self.state.plan and not self.state.plan_approved:
                # Ask now, instead of letting the agent try an action (refused) and call `plan` just to get asked.
                decision, plan_note = await self.review_plan(self.state.plan)
                if decision == "reject":
                    await self.finish("stopped", "You rejected the plan.")
                    return
        self.last_task = self.state.task_text
        self.add_with_page({"role": "user"}, task, page)
        if plan_note:  # the user edited the plan: the agent re-proposes it with `plan`
            self.messages.append({"role": "user", "content": plan_note})

        for step in range(MAX_STEPS):
            report.set_step(step + 1)
            if MAX_STEPS - step == WRAP_UP_AT:
                await self.ui("step", "⏰ Few steps left – asking Krify to wrap up")
                self.messages.append({"role": "user", "content": WRAP_UP})
            if more_topics := page_topics(self.state.view_text) - self.topics:  # the page shows prices: shop rules
                self.topics |= more_topics
                self.refresh_system()
            added = self.page_groups() - self.tool_groups
            if added:
                self.tool_groups |= added
                self.refresh_system()
                await self.ui("step", f"🧰 Tools added for this page: {', '.join(sorted(added))}")
            tools = tools_for(self.tool_groups)
            reply, used_model, tokens = await ask_ai(
                self.messages, tools, model, on_note=lambda t: self.ui("step", t),
                boost=self.state.boost_next,
            )
            self.state.boost_next = False
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
                if not clear and text.lstrip().startswith(("{", "[", "```")):
                    # Still unreadable JSON after the nudge: never show raw JSON to the user.
                    text = "I finished, but my answer came out garbled. Please ask again."
                if kind == "done" and (rejection := check_ui_state(self.state)):
                    await self.ui("step", "🔍 Checking the page isn't showing an error before finishing")
                    self.messages.append({"role": "user", "content": rejection})
                    continue
                if kind == "done" and (rejection := check_filters(self.state, text)):
                    await self.ui("step", "🔍 The list may be filtered – asking Krify to check the filters first")
                    self.messages.append({"role": "user", "content": rejection})
                    continue
                if kind == "done" and (rejection := check_facts(self.state, text)):
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
            seen = self.state.elements  # the element list the AI saw when it sent these calls
            for i, call in enumerate(calls):
                name = call.function.name
                tool_msg = {"role": "tool", "tool_call_id": call.id}
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                if finished:
                    self.messages.append({**tool_msg, "content": "Skipped."})
                elif name in ACTION_OPS | {"batch", "done"} and self.state.plan and not self.state.plan_approved:
                    self.messages.append({**tool_msg, "content": PLAN_NOT_APPROVED})
                    check_progress(self.state, name, False)
                elif name in ACTION_OPS and batch_stopped:
                    self.messages.append({**tool_msg, "content": batch_stopped})
                elif name == "done" and actions_done:
                    self.messages.append({**tool_msg, "content": DONE_TOO_EARLY})
                elif name == "done":
                    summary = args.get("summary", "Done.")
                    if rejection := check_ui_state(self.state):
                        await self.ui("step", "🔍 Checking the page isn't showing an error before finishing")
                        self.messages.append({**tool_msg, "content": rejection})
                        continue
                    if rejection := check_claims(self.state, summary):
                        await self.ui("step", "🔍 Claim check: Krify said it did something it didn't – sent back")
                        self.messages.append({**tool_msg, "content": rejection})
                        continue
                    if rejection := check_filters(self.state, summary):
                        await self.ui("step", "🔍 The list may be filtered – asking Krify to check the filters first")
                        self.messages.append({**tool_msg, "content": rejection})
                        continue
                    if rejection := check_facts(self.state, summary):
                        await self.ui("step", "🔍 Fact check: answer sent back to be checked")
                        self.messages.append({**tool_msg, "content": rejection})
                        continue
                    self.messages.append({**tool_msg, "content": "Reported to the user."})
                    await self.finish("done", summary)
                    finished = True
                elif (name == "plan" and self.state.plan_approved
                      and [str(s) for s in (args.get("steps") or [])][:8] == self.state.plan
                      and not str(args.get("done_when") or "").strip()):
                    self.messages.append({**tool_msg, "content": SAME_PLAN})
                elif name == "plan":
                    steps = [str(s) for s in (args.get("steps") or [])][:8]
                    if done_when := str(args.get("done_when") or "").strip():
                        # The site turned out different from what the first plan assumed.
                        self.state.done_when = done_when[:200]
                    if not self.state.plan_approved and not safety.plan_needs_approval(steps, self.state.task_text):
                        self.state.plan_approved = True  # short and harmless: saved below, no approval needed
                    if not self.state.plan_approved:
                        decision, text = await self.review_plan(steps)
                        self.messages.append({**tool_msg, "content": text})
                        if decision == "reject":
                            await self.finish("stopped", "You rejected the plan.")
                            finished = True
                    else:
                        title = "📋 Plan updated:" if self.state.plan else "📋 Plan:"
                        self.state.plan = steps
                        await self.ui("step", f"{title}\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(self.state.plan, 1)))
                        self.messages.append(
                            {**tool_msg, "content": "Plan saved – you'll see it at every step. Call plan again to "
                                                    "update it; start finished steps with ✔."}
                        )
                    check_progress(self.state, name, False)
                elif name == "ask_user" and (rejection := check_claims(self.state, str(args.get("question", "")))):
                    await self.ui("step", "🔍 Claim check: Krify said it did something it didn't – sent back")
                    self.messages.append({**tool_msg, "content": rejection})
                elif name == "ask_user" and (rejection := check_handoff(self.state, str(args.get("question", "")))):
                    await self.ui("step", "🔍 Krify asked you to do a step it can do itself – sent back")
                    self.messages.append({**tool_msg, "content": rejection})
                elif name == "ask_user":
                    self.messages.append({**tool_msg, "content": "Question shown. The user's answer comes next."})
                    await self.finish("ask", args.get("question", "Can you tell me more?"))
                    finished = True
                elif name == "note":
                    text = str(args.get("text", ""))
                    missing = missing_models(self.state.product_text, text)
                    if missing and tuple(missing) not in self.state.note_warnings:
                        # e.g. the task says "iPhone 16e" but the note says "iPhone 16" (a sponsored result).
                        self.state.note_warnings.add(tuple(missing))
                        await self.ui("step", f"🔍 Note doesn't mention {', '.join(missing)} – checking the product")
                        self.messages.append({**tool_msg, "content": WRONG_PRODUCT.format(", ".join(missing))})
                    else:
                        await self.ui("step", f"📝 {text}")
                        self.state.notes.append(text[:200])
                        self.messages.append({**tool_msg, "content": "Saved."})
                    check_progress(self.state, name, False)
                elif name == "more_tools":  # the AI needs a group that wasn't sent: add it, one step lost, not the task
                    group = str(args.get("group", "")).strip().lower()
                    if group in TOOL_GROUPS and group != "basic":
                        self.tool_groups.add(group)
                        self.refresh_system()
                        await self.ui("step", f"🧰 Tools added: {group}")
                        content = f"Added the {group} tools: {', '.join(TOOL_GROUPS[group])}. Use them from your next step."
                    else:
                        content = f"No such group. Groups: {', '.join(g for g in GROUP_ORDER if g != 'basic')}."
                    self.messages.append({**tool_msg, "content": content})
                    check_progress(self.state, name, False)
                elif name == "get_records":  # a long list's left-out items, from the last page view (no browser)
                    await self.ui("step", f"📋 More of “{args.get('list', '')}” {args.get('search') or ''}".strip())
                    result = get_records(self.state.records, str(args.get("list", "")), str(args.get("search", "")),
                                         args.get("start", 0))
                    self.state.seen.append(result)
                    self.messages.append({**tool_msg, "content": result})
                    check_progress(self.state, name, False)
                elif name in READ_OPS:
                    on_sheet = name in SHEET_OPS | {"read_text"} and is_sheet(self.state.current_url)
                    label = "📊 Read the sheet" if on_sheet and name == "read_text" else describe(name, args)
                    await self.ui("step", label)
                    if on_sheet:
                        result = await self.sheet_op(name, args)
                    elif name in SHEET_OPS:
                        result = f"Error: {name} only works on a Google Sheets page."
                    else:
                        result = await self.safe_run(name, args)
                    if name == "inspect" and args.get("ref") is not None and re.search(
                            rf'^\[{re.escape(str(args["ref"]))}\] (dropdown|select) ', self.state.elements or "", re.M):
                        # A closed dropdown's choices aren't on the page: inspecting it can't show them.
                        result = (f"{result}\n[{args['ref']}] is a dropdown: its choices exist only while it's open, so "
                                  f"inspect can't show them. Use select_option(element={args['ref']}, option=<the choice "
                                  "you want>) – it opens, searches and picks; a miss lists the real choices.")
                    if (name in ("find_text", "find_elements") and str(result).startswith(("(no text", "(no elements"))
                            and (filters := filter_dropdowns(self.state))):
                        result = f"{result}{FILTER_HINT.format(', '.join(filters))}"
                    same_read = name == "read_text" and str(result) == self.state.last_read
                    stuck = check_progress(self.state, "same_read" if same_read else name, False)
                    self.state.seen.append(str(result))
                    if self.dev_mode:
                        self.trace.append(f"{label} → {preview(str(result))}")
                    if name == "get_elements":
                        self.state.elements = str(result)
                        self.add_with_page(tool_msg, "Elements:", result)
                    elif name == "find_elements":
                        self.state.elements = merge_elements(self.state.elements, str(result))
                        self.messages.append({**tool_msg, "content": str(result)})
                    elif same_read:
                        # Reading the same unchanged text again (seen 6× in a row on Amazon): say so.
                        self.messages.append({**tool_msg, "content": SAME_READ + stuck})
                    elif name == "find_text" and not str(result).startswith(("Error", "(no text")):
                        # It scrolled to the match: show the page there, so the elements next to it have numbers.
                        page = await self.page_view()
                        prev_page = page
                        self.add_with_page(tool_msg, f"{result}\n(Save facts you'll need later with note.)", page)
                    elif name in ("read_text", "find_text"):
                        if name == "read_text":
                            self.state.last_read = str(result)
                            if not on_sheet and (models := missing_words(self.state.product_text, f"{self.state.current_title} {self.state.current_url}")):
                                result = f"{result}{WRONG_PAGE.format(', '.join(models))}"
                        self.add_screen_read(tool_msg, f"{result}\n(Save facts you'll need later with note.)")
                    else:
                        self.messages.append({**tool_msg, "content": str(result)})
                    if name in ("find_elements", "inspect"):
                        self.state.boost_next = True  # it already flagged this page as unclear – help it decide
                elif name == "batch":
                    actions_done += await self.run_batch(tool_msg, args.get("steps"))
                    if self.state.loop_question:
                        await self.stop_looping()
                        finished = True
                else:
                    label = element_label(self.state.elements, args.get("element"))  # before: numbers may change
                    # Remember the page before a click, to tell the AI plainly whether it worked.
                    before = (self.state.current_url, self.state.elements)
                    open_list = self.state.open_list  # a dropdown list open before the click: did it get a choice?
                    clicked = safety.element_line(self.state.elements, args.get("element")) if name == "click" else ""
                    # Later steps of a batch are checked against the view the AI sent them from.
                    result, status = await self.do_action(name, args, seen=seen if actions_done else None)
                    if status != "ok":
                        self.messages.append({**tool_msg, "content": result})
                        check_progress(self.state, name, False)
                        batch_stopped = ("Skipped: an earlier step was blocked by the user." if status == "blocked"
                                         else BATCH_REFUSED)
                        continue
                    actions_done += 1
                    failed = result.startswith("Error")
                    page = await self.page_view()
                    if name == "click" and not failed:
                        if sentence := click_result(before, (self.state.current_url, self.state.elements), clicked, self.state.current_title,
                                                     open_list):
                            log.info("  told the AI: %s", sentence)
                            result = f"{result}\n{sentence}"
                    if not failed and same_page(before[0], self.state.current_url) and (diff := change_lines(self.state.page_changes)):
                        log.info("  %s", diff.replace("\n", "\n    "))
                        result = f"{result}\n{diff}"
                    if (name == "click" and not failed and before[0] != self.state.current_url
                            and not blocking_status(self.state.page_status) and opened_what_asked(self.state.task_text, clicked)):
                        # "open agents" + clicked "Agents" + a new page opened: that was the whole task.
                        # Asking the AI again only made it doubt and click around (6 extra steps on Asana).
                        opened = self.state.current_title or self.state.current_url
                        self.state.seen.append(opened)
                        self.messages.append({**tool_msg, "content": f"{result}\nThe task is finished."})
                        await self.finish("done", f"Done – the page \"{opened}\" is open.")
                        finished = True
                        continue
                    changed = page != prev_page
                    # Going back to a page from a few actions ago isn't progress (A→B→A→B loops).
                    new_page, loop_note = check_loop(self.state, page, changed, self.dev_mode, self.trace)
                    if name == "scroll":
                        scrolled(self.state, args, page)
                    result = (f"{result}{form_not_sent(name, args, label, self.state.page_status)}"
                              f"{check_progress(self.state, name, new_page)}"
                              f"{check_repeat(self.state, name, args, new_page)}{loop_note}")
                    more = any(c.function.name in ACTION_OPS for c in calls[i + 1:])
                    later = [_args_of(c) for c in calls[i + 1:] if c.function.name in ACTION_OPS]
                    moved = failed or (layout(page) != layout(prev_page) and any(uses_element(a) for a in later))
                    prev_page = page
                    if more and not moved and not loop_note and actions_done < MAX_BATCH:
                        self.messages.append({**tool_msg, "content": f"{result} (next step follows)"})
                        continue
                    if more:
                        batch_stopped = BATCH_LIMIT if actions_done >= MAX_BATCH else (STEP_FAILED if failed else PAGE_MOVED)
                    # Show the AI the page after the last action, so it can check the result.
                    self.add_with_page(tool_msg, result, page)
                    if self.state.loop_question:
                        await self.stop_looping()
                        finished = True

            if finished:
                return

        # Never end empty-handed: report what was found so far.
        found = "\n".join(f"• {n}" for n in self.state.notes) or "• nothing confirmed yet"
        await self.finish(
            "done",
            f"Stopped after {MAX_STEPS} steps (safety limit). What Krify found so far:\n{found}\n"
            "Try a simpler or more specific task for the rest.",
        )
