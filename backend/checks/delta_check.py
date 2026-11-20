"""Assertions for context/delta.py (VIEW_MODE=hybrid): when a page change is sent as its changed lines, and when
the whole view goes out instead. No LLM, browser or network. Run via checks/run.py, or alone:
python -m checks.delta_check"""

import copy
import json
import sys

from app.context import pageview
from app.context.delta import (MAX_DELTAS, MOVED_LAYOUT_MAX_LINES, PAGE_TEXT_FULL, PAGE_TEXT_REPEAT, REVERTED, UNCHANGED,
                               same_view, split_page_text, view_delta)

problems = []


def check(ok, what):
    print(("✓ " if ok else "✗ ") + what)
    if not ok:
        problems.append(what)


def view(name, **changes):
    with open(f"fixtures/{name}.json", encoding="utf-8") as f:
        data = json.load(f)
    if "status" in changes:
        data["pages"][0]["status"] = changes["status"]
    return "[Current page] x – https://x.example/\n" + pageview.render(copy.deepcopy(data)).text


def numbered(n, extra_at=None):
    """A flat list of n elements: [1] button "B1" … – with one more ("Extra") inserted at position `extra_at`
    and every element after it numbered one higher, as the extension numbers a page again from the top."""
    labels = [f"B{i}" for i in range(1, n + 1)]
    if extra_at is not None:
        labels.insert(extra_at, "Extra")
    return "\n".join(f'[{i}] button "{label}"' for i, label in enumerate(labels, 1))


form = view("add_activity_form")

# --- the same view, a message appearing, a different page
check(view_delta(form, form) == UNCHANGED, "an identical view is 'unchanged'")
err = view("add_activity_form", status="Form error – Contact is required")
d = view_delta(form, err)
check(d is not None and "+ ⚠ ALERTS" in d and d.count("\n") == 1, f"a new ALERTS line alone is a one-line delta: {d!r}")
check(d is not None and d.startswith("[Layout unchanged"), "…and says the layout is unchanged")
check(view_delta(err, form) is not None and "- ⚠ ALERTS" in view_delta(err, form), "a message going away is shown as '-'")
check(view_delta(form, view("login_dialog")) is None, "a different page (a pop-up opened) is sent whole")

# --- numbers that move
base = numbered(14)
check(view_delta(base, numbered(14, extra_at=14)) is not None, "an element added at the END (no number moves) is a delta")
check(view_delta(base, numbered(14, extra_at=1)) is None, "an element inserted at the START (every later number moves) → whole view")
renumbered = base.replace('[7] button "B7"', '[7] button "Other"')
d = view_delta(base, renumbered)
check(d is not None and d.splitlines()[1] == '~ [7] button "Other"', "a changed element keeps its number: shown as '~' with the new line")
check(len(base.splitlines()) > 4 and view_delta(base, base.replace("B", "Z")) is None, "most lines changed → whole view")

# --- size limits
many = "\n".join(f'[{i}] button "B{i}"' for i in range(1, 60))
moved = many
for i in range(1, MOVED_LAYOUT_MAX_LINES + 2):
    moved = moved.replace(f'[{i}] button "B{i}"', f'[{i}] button "New{i}"')
check(view_delta(many, moved) is None, f"more than {MOVED_LAYOUT_MAX_LINES} replaced elements → whole view")
check(view_delta(many, many.replace('[3] button "B3"', '[3] button "New3"')) is not None, "one replaced element in a long view is a delta")

# --- the Unrestricted page text block
text_full = form + PAGE_TEXT_FULL + " (…)\nlots of words"
text_repeat = form + PAGE_TEXT_REPEAT
check(split_page_text(text_full)[0] == form and split_page_text(text_full)[1].startswith("\n[Page text]"), "the page text block is split off the view")
check(view_delta(text_full, text_repeat) == UNCHANGED and same_view(text_full, text_repeat), "page text full vs repeated line is not a change")
check(view_delta(text_full, err + PAGE_TEXT_REPEAT) is not None, "a real change is still a delta when the text block differs")
check("lots of words" not in (view_delta(text_full, err + PAGE_TEXT_REPEAT) or ""), "the page text is never part of a delta")

# --- constants and determinism
check(MAX_DELTAS >= 1 and REVERTED != UNCHANGED, "constants are sane")
check(view_delta(form, err) == view_delta(form, err), "same input, same delta")

sys.exit(1 if problems else 0)
