"""VIEW_MODE=hybrid: what to send the AI when the page it is on changed a little.

The AI already holds the last FULL page view. When the next view differs from it in a few lines, only those
lines are sent (`view_delta`) instead of the whole view. Element numbers are re-assigned from scratch on every
view (content.js numberElements), so a number can move when something is inserted before it. The delta checks
itself: numbers are inside the lines being compared, so a shifted number changes many lines at once, the diff is
too big, and `view_delta` answers None – the caller then sends the full view.

Pure functions, no agent state: checks/delta_check.py asserts them.
"""

import difflib
import re

from app.context.element_list import layout

# The Unrestricted-mode "[Page text]" block is appended after the view (context/view.py). Its first view of a page
# carries the text; later views of the same page carry PAGE_TEXT_REPEAT instead. Neither is part of what a
# delta compares.
PAGE_TEXT_MARK = "\n[Page text] Unrestricted mode:"
PAGE_TEXT_FULL = PAGE_TEXT_MARK + " all text on the page"
PAGE_TEXT_REPEAT = (PAGE_TEXT_MARK + " this page's text was sent with its first view and is not repeated – "
                    "use find_text or read_text to read it again.")

POPUP_MARK = "🔲"  # pageview.py: an open pop-up / dropdown list / dialog is listed first

SAME_LAYOUT_MAX_LINES = 20  # the numbering is identical: only values, states, messages differ
MOVED_LAYOUT_MAX_LINES = 8  # a few elements came or went: the numbers have to be checked line by line
MAX_SHARE_OF_LINES = 0.3  # more than this share of the view changed: send the view again
MAX_SHARE_OF_CHARS = 0.5  # the delta is not much shorter than the view: send the view
MAX_DELTAS = 3  # views in a row sent as a delta, then the full view again (a refresh)
LINE_CUT = 110  # a removed line is shown cut to this

UNCHANGED = "[Page unchanged since the last view.]"
REVERTED = ("[The page is back to the last full page view above – the changes listed after that view no longer "
            "apply.]")
_NOTE_SAME = ("[Layout unchanged – only the lines below differ from the last full page view; every other line "
              "is as it was there, with the same element numbers.]")
_NOTE_MOVED = ("[Page changed a little – only the lines below differ from the last full page view; every other "
               "line is as it was there. + new line, ~ changed line (same number), - gone.]")

_NUMBER = re.compile(r"^\s*(\[\d+\])")


def split_page_text(page):
    """(the view, the Unrestricted page text block or "") – a view string cut at the [Page text] marker."""
    page = page or ""
    at = page.find(PAGE_TEXT_MARK)
    return (page, "") if at < 0 else (page[:at], page[at:])


def same_view(a, b):
    """The same page view, whatever the Unrestricted page text block says (it is sent once per page)."""
    return split_page_text(a)[0] == split_page_text(b)[0]


def _cut(line):
    line = line.strip()
    return line if len(line) <= LINE_CUT else line[:LINE_CUT] + "…"


def _changes(base_lines, new_lines):
    """[(sign, line)] for what changed from base to new: '+' new, '-' gone, '~' replaced by a line with the
    same element number (only the new one is shown)."""
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base_lines, new_lines, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        old, new = base_lines[i1:i2], new_lines[j1:j2]
        if tag == "replace" and len(old) == len(new):
            for o, n in zip(old, new):
                same = (a := _NUMBER.match(o)) and (b := _NUMBER.match(n)) and a.group(1) == b.group(1)
                if same:
                    out.append(("~", n.strip()))
                else:
                    out += [("-", _cut(o)), ("+", n.strip())]
            continue
        out += [("-", _cut(line)) for line in old if line.strip()]
        out += [("+", line.strip()) for line in new if line.strip()]
    return out


def view_delta(base, page):
    """The AI holds `base` (a full view). `page` is the view now. → the text to send in place of the whole view
    (starting with a note that says what it is), or None when the whole view must be sent instead."""
    base_view, _ = split_page_text(base)
    view, _ = split_page_text(page)
    if base_view == view:
        return UNCHANGED
    if (POPUP_MARK in base_view) != (POPUP_MARK in view):  # a dialog opened or closed: the page is different now
        return None
    base_lines, lines = base_view.splitlines(), view.splitlines()
    same_layout = layout(base_view) == layout(view)
    changes = _changes(base_lines, lines)
    limit = SAME_LAYOUT_MAX_LINES if same_layout else MOVED_LAYOUT_MAX_LINES
    if not changes or len(changes) > limit or len(changes) > max(4, MAX_SHARE_OF_LINES * len(lines)):
        return None
    body = "\n".join(f"{sign} {line}" for sign, line in changes)
    if len(body) > MAX_SHARE_OF_CHARS * len(view):
        return None
    return f"{_NOTE_SAME if same_layout else _NOTE_MOVED}\n{body}"
