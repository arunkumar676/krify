"""Form guards: Save/Submit while required fields are empty, and a submit the page shows errors for."""

import re

from app.context import pageview
from app.context.element_list import element_label

# A submit that didn't go through: the page shows inline field errors (content.js's "Form error" lines).
SUBMIT_WORDS = re.compile(r"save|submit|add|create|update|send|continue|next|confirm|apply|register|sign ?up|book|"
                          r"post|done|finish|place order|pay", re.I)
FORM_ERROR = "Form error – "
FORM_NOT_SENT = ("\nNOT saved – the form shows errors: {}. Fill those fields (a dropdown: select_option with the "
                 "field and the option's text), then submit again.")

# Save/Submit while the [Form] checklist still shows required fields empty: refused once per form
# and set of empty fields (the detection can be wrong – a second click goes through, and
# form_not_sent then reports the site's own errors).
REQUIRED_EMPTY = ("NOT clicked – required fields are still empty: {}. {} If you're sure they're already filled, "
                  "click it again.")
FILL_ANY = ("This is a dummy/test request: fill each with any real option (or short placeholder text), one field "
            "at a time, then save. Dropdown: select_option with a likely option – if it isn't there, the reply lists "
            "the real ones.")
ASK_ONCE = ("Use values from the user's request. For any the request doesn't give, call ask_user ONCE listing all "
            "of them with the options you saw (select_option with a guess lists a dropdown's real options), then fill "
            "them and save.")
DUMMY = re.compile(r"\b(dummy|test|testing|sample|fake|placeholder|example|random|any)\b", re.I)


def form_not_sent(name, args, label, page_status):
    """After a submit-like step: if the page now shows inline form errors, say plainly it wasn't saved."""
    submitted = ((name == "click" and SUBMIT_WORDS.search(label or ""))
                 or (name == "press_key" and str(args.get("key", "")).lower() == "enter")
                 or (name == "type" and args.get("submit")))
    if not submitted:
        return ""
    errors = [part[len(FORM_ERROR):] for part in (page_status or "").split(" | ") if part.startswith(FORM_ERROR)]
    return FORM_NOT_SENT.format("; ".join(errors)) if errors else ""


def track_form_edit(state, name, args):
    """Remember that Krify has started filling the form in view (typed into / chose in one of its
    fields), so a click that would leave it is caught; forget it once the form is submitted.
    state.form is still the view from before this action."""
    form = state.form
    if not form:
        return
    el = str(args.get("element"))
    submits = {str(i) for i in form.get("submits") or []}
    submitted = ((name == "click" and (el in submits or (not submits and SUBMIT_WORDS.search(
                     element_label(state.elements, el) or ""))))
                 or (name == "type" and args.get("submit"))
                 or (name == "press_key" and str(args.get("key", "")).strip().lower() == "enter"))
    if submitted:
        state.form_edit, state.leave_warned = None, set()
        return
    kinds = {str(f.get("id")): f.get("kind") for f in form.get("fields") or []}
    # Clicked one of an open list's options, or ticked a checkbox/radio (opening a dropdown isn't filling it).
    picked = name == "click" and state.open_list is not None and element_label(state.elements, el) in state.open_list[1]
    ticked = name == "click" and kinds.get(el) == "choice"
    if (name in ("type", "select_option") and el in kinds) or picked or ticked:
        state.form_edit = {"url": state.current_url, "name": form.get("name", "")}


def required_empty(state, name, args):
    """Refuse (once) a click on the form's Save/Submit button while the [Form] checklist still
    shows required fields empty. → the refusal text, or '' to let the click through."""
    form = state.form
    if name != "click" or not form or str(args.get("element")) not in {str(i) for i in form.get("submits") or []}:
        return ""
    missing = pageview.missing_required(form)
    key = (form.get("name", ""), tuple(missing))
    if not missing or key in state.submit_guarded:
        return ""
    state.submit_guarded.add(key)
    return REQUIRED_EMPTY.format(", ".join(missing), FILL_ANY if DUMMY.search(state.task_text) else ASK_ONCE)
