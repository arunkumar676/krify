"""fill_form: the pure parts – find a form field by its name, check what it shows, judge a submit.

The tool itself (ToolsMixin.fill_form in tools/actions.py) fills every field the AI listed with no AI call in
between, checks each one by reading the form back, and presses the form's Submit button only when every check
passes. These helpers work on the TaskState only (no browser), so they are testable on their own.
"""

import re

from app.context.element_list import element_label
from app.guardrails import safety
from app.guardrails.element_match import label_matches
from app.guardrails.forms import FORM_ERROR, SUBMIT_WORDS

MAX_FIELDS = 30  # fields one fill_form call may fill
WAIT_TRIES = 3  # a disabled field (opens after an earlier choice): look again this many times
WAIT_SECONDS = 0.8
YES = re.compile(r"^\s*(yes|y|true|on|1|checked|tick(ed)?|check|agree|accept(ed)?|select(ed)?)\s*$", re.I)
NO = re.compile(r"^\s*(no|n|false|off|0|unchecked|untick(ed)?|uncheck|clear)\s*$", re.I)
# After a submit: the page says it worked.
SUCCESS = re.compile(r"\b(success\w*|submitted|saved|created|added|thank(s| you)|received|sent|updated|"
                     r"registered|booked|confirmed|completed|done)\b", re.I)
FIELD_ROLE = re.compile(r"^(input|textarea|select|dropdown|clickable|combobox|checkbox|radio)\b")


def _norm(text):
    return " ".join(str(text or "").lower().replace("…", "").split()).strip(" \"'*:")


def form_fields(state):
    return (state.form or {}).get("fields") or []


def find_field(state, name, taken=()):
    """The form field the AI calls `name` → (element id, its [Form] entry or None, kind), or None.
    Looks in the [Form] checklist first (exact label, then a label that contains / fits the name), then at
    any field-like element whose label or hint fits (a form the checklist missed). `taken`: ids already
    used by this call (two fields never get the same box)."""
    want = _norm(name)
    if not want:
        return None
    fields = [f for f in form_fields(state) if str(f.get("id")) not in taken]
    for test in (lambda lab: lab == want,
                 lambda lab: len(want) >= 3 and (want in lab or (len(lab) >= 3 and lab in want)),
                 lambda lab: label_matches(name, [lab])):
        for f in fields:
            if test(_norm(f.get("label"))):
                return f.get("id"), f, f.get("kind") or "text"
    # Not in the checklist: a field-like element with that label, or with "field: <name>" as its hint.
    hints = {m.group(1): m.group(2) for m in re.finditer(r'^\s*\[(\d+)\] .*· hint: "field: ([^"]*)"', state.view_text or "", re.M)}
    for m in re.finditer(r'^\[(\d+)\] (.*)$', state.elements or "", re.M):
        element_id, line = m.group(1), m.group(2)
        if element_id in taken or not FIELD_ROLE.match(line):
            continue
        names = [safety.visible_text(line), hints.get(element_id, "")]
        if any(n and (_norm(n) == want or label_matches(name, [n])) for n in names):
            kind = ("choice" if re.search(r"\b(checkbox|radio)\b", line) else
                    "dropdown" if re.match(r"(select|dropdown|clickable|combobox)\b", line) else
                    "date" if re.search(r"\b(date|time|datetime-local)\b", line) else "text")
            return int(element_id), None, kind
    return None


def radio_option(state, value, near_id):
    """The radio/checkbox element whose own label is `value` (a radio group's choice), nearest to the
    group's first element `near_id` → its id, or None."""
    found = []
    for m in re.finditer(r'^\[(\d+)\] input (radio|checkbox) "([^"]*)"', state.elements or "", re.M):
        if _norm(m.group(3)) == _norm(value) or label_matches(value, [m.group(3)]):
            found.append(int(m.group(1)))
    try:
        near = int(near_id)
    except (TypeError, ValueError):
        near = 0
    return min(found, key=lambda i: abs(i - near)) if found else None


def is_checked(state, element_id):
    line = safety.element_line(state.elements, element_id)
    return "(checked)" in line


def step_for(state, element_id, field, kind, value):
    """The action that puts `value` into the field → (op, args) or (None, reason it can't be done)."""
    label = element_label(state.elements, element_id) or (field or {}).get("label", "")
    if kind == "file":
        return None, "a file upload – you have to choose the file yourself"
    if kind == "dropdown":
        return "select_option", {"element": element_id, "label": label, "option": value}
    if kind == "choice":
        line = safety.element_line(state.elements, element_id)
        if "radio" in line or not (YES.match(value) or NO.match(value)):
            option = radio_option(state, value, element_id)
            if option is None:
                return None, f'no choice "{value}" found'
            if is_checked(state, option):
                return "skip", "already chosen"
            return "click", {"element": option, "label": element_label(state.elements, option)}
        want_on = bool(YES.match(value))
        if is_checked(state, element_id) == want_on:
            return "skip", "already " + ("ticked" if want_on else "unticked")
        return "click", {"element": element_id, "label": label}
    return "type", {"element": element_id, "label": label, "text": value}


def value_ok(field, kind, wanted, line=""):
    """Does the field now show the value it was given? field: its fresh [Form] entry (None: judge by the
    element line). The form cuts long values to ~30 characters, so a cut-off start counts."""
    if field is None:
        shown = re.search(r'\(value: "([^"]*)"', line or "")
        if kind == "choice":
            return ("(checked)" in line) == (not NO.match(wanted))
        return bool(shown) and (_norm(wanted) in _norm(shown.group(1)) or _norm(shown.group(1)) in _norm(wanted))
    now, want = _norm(field.get("value")), _norm(wanted)
    if kind == "choice":
        if YES.match(wanted):
            return bool(field.get("filled"))
        if NO.match(wanted):
            return not field.get("filled")
        return bool(field.get("filled")) and (now == want or label_matches(wanted, [field.get("value", "")]))
    if not field.get("filled"):
        return False
    if now == "••••" or kind == "date":  # a password is never shown; dates come back in the site's format
        return True
    if kind == "dropdown":
        return now == want or want in now or now in want or label_matches(wanted, [field.get("value", "")])
    return now == want or (len(now) >= 10 and want.startswith(now)) or want in now


def submit_button(state, text=""):
    """The form's submit button → element id, or None. `text`: the button the AI named ("Save Activity");
    it must be the form's own submit button, or a button whose label reads like one (Save, Submit, Apply…)."""
    form = state.form or {}
    submits = [str(i) for i in form.get("submits") or []]
    candidates = []
    for m in re.finditer(r'^\[(\d+)\] (.*)$', state.elements or "", re.M):
        element_id, line = m.group(1), m.group(2)
        label = safety.visible_text(line)
        if element_id in submits or (re.match(r"(button|input (submit|button))\b", line) and SUBMIT_WORDS.search(label)):
            candidates.append((element_id, label))
    if text:
        for element_id, label in candidates:
            if _norm(label) == _norm(text) or label_matches(text, [label]):
                return int(element_id)
        return None
    for element_id, _ in candidates:
        if element_id in submits:
            return int(element_id)
    return None


def form_errors(state):
    """The form's own error messages now: 'Form error – …' status parts and per-field errors."""
    errors = [p[len(FORM_ERROR):] for p in (state.page_status or "").split(" | ") if p.startswith(FORM_ERROR)]
    errors += [f'{f.get("label")}: {f["error"]}' for f in form_fields(state) if f.get("error")]
    return errors


def after_submit(before_url, before_form, state, problems):
    """Did the submit go through? → ("ok", what the page said) / ("failed", why) / ("unsure", "").
    problems: blocking_status() of the page now."""
    errors = form_errors(state)
    if errors or problems:
        return "failed", "; ".join(errors) or problems
    shown = (((state.page_changes or {}) if isinstance(state.page_changes, dict) else {}).get("texts") or {}).get("shown") or []
    said = next((t for t in [*shown, *(state.page_status or "").split(" | ")] if SUCCESS.search(t or "")), "")
    form = state.form or {}
    gone = not form or (form.get("name") != (before_form or {}).get("name"))
    if said or state.current_url != before_url or gone:
        return "ok", said[:200]
    return "unsure", ""
