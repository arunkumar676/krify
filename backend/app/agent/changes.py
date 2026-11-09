"""Tell the AI in one plain sentence whether its click worked.

The AI used to see only the new page after a click and could miss that the click had already done
its job, so it clicked toggle buttons (Asana's "Knowledge" sidebar) open and closed until the step
limit. click_result() compares the flat element lists (View.flat, lines like `[8] div "Knowledge"`)
from before and after the click and says: it worked (new page / new things appeared), or it HID
things (a toggle closed again), or nothing ('' – nothing clear to say).
"""

import re
from collections import Counter

_LINE = re.compile(r"^\[\d+\]\s+(.*)$")
# 'input text "Search" (value: "mouse")' -> 'input text "Search"': the role and label only, so a typed
# value, (empty) or (checked) doesn't count as a new element. Lines without a label are kept whole.
_KEY = re.compile(r'^(.*?"[^"]*")')
MAX_NAME = 40


def _labels(flat):
    """Counter of element keys in a flat list, ignoring the [n] numbers (they change every look)."""
    keys = Counter()
    for line in (flat or "").splitlines():
        match = _LINE.match(line.strip())
        if match:
            text = match.group(1)
            keys[(_KEY.match(text) or match).group(1).strip()] += 1
    return keys


def _short(text):
    """Whitespace squeezed, max 40 chars."""
    text = " ".join((text or "").split())
    return text if len(text) <= MAX_NAME else text[:MAX_NAME - 1] + "…"


def _name(line):
    """'link "AI Meetings"' -> 'AI Meetings', '' if there's no quoted text."""
    quoted = re.search(r'"([^"]*)"', line or "")
    return _short(quoted.group(1)) if quoted else ""


def _names(keys, limit=5):
    """Up to `limit` distinct names: 'AI Meetings, Pages, Tables (+2 more)'."""
    names = []
    for key in keys:
        name = _name(key)
        if name and name not in names:
            names.append(name)
    more = f" (+{len(names) - limit} more)" if len(names) > limit else ""
    return ", ".join(names[:limit]) + more


def click_result(before, after, clicked, title=""):
    """One sentence for the AI after a click, or '' if nothing clear changed.
    before/after: (url, flat element list). clicked: the clicked element's line ('div "Knowledge"'),
    or '' for click_at. title: the page title after the click."""
    (url1, flat1), (url2, flat2) = before, after
    old, new = _labels(flat1), _labels(flat2)
    added, gone = new - old, old - new  # Counter subtraction keeps only what really appeared / went
    label = f'"{_name(clicked)}"' if _name(clicked) else "that spot"

    if url1 != url2:
        shown = f" New on screen: {_names(added)}." if _names(added) else ""
        return (f"✅ Your click on {label} worked: a new page opened (\"{_short(title)}\").{shown} "
                f"Don't click {label} again. If this is what the task asked for, call done; otherwise continue.")
    # Only extra copies of things already on the page (e.g. "Load more" adding product tiles): say nothing.
    brand_new = [key for key in added if key not in old]
    if not _names(brand_new) and not _names(gone):
        return ""
    if added and sum(added.values()) >= sum(gone.values()):
        return (f"✅ Your click on {label} worked: it opened {_names(added)}. Don't click {label} again "
                "(that may close it). Pick one of these, or call done if this is what the task asked for.")
    if gone:
        return (f"⚠️ Your click on {label} HID things that were on screen ({_names(gone)}). {label} probably "
                "opens/closes a panel, so your previous click had already done the job. Don't keep clicking "
                f"{label}: if you need those back, click it once more, then use them or call done.")
    return ""
