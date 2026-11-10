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


_PROMPT = re.compile(r"^\s*(-+\s*)?(select|choose|pick)\b", re.I)  # "Select a contact": a placeholder


def _list_outcome(open_list, clicked, gone):
    """What a click did to a custom dropdown list that was open before it:
    'picked' (clicked one of its real options), 'left' (the list closed with nothing chosen: another
    field, the placeholder option, or the field itself was clicked) or '' (no list, or still open).
    open_list: (field name, [option labels]) from before the click."""
    if not open_list:
        return ""
    options = [o for o in open_list[1] if o]
    real = [_short(o) for o in options if not _PROMPT.match(o)]
    gone_names = {_name(key) for key in gone}
    if not real or not any(o in gone_names for o in real):
        return ""  # nothing to choose from, or the list is still open
    return "picked" if _name(clicked) in real else "left"


def _still_empty(open_list):
    """' "Contact" is still empty: …' – how to choose an option after leaving its list."""
    field, options = open_list
    example = next((o for o in options if o and not _PROMPT.match(o)), "")
    example = f' (e.g. "{_short(example)}")' if example else ""
    field = f'"{_short(field)}"' if field else "That field"
    return (f" {field} is still empty: click its field once, then make your very next click one of its "
            f"options{example}.")


def click_result(before, after, clicked, title="", open_list=None):
    """One sentence for the AI after a click, or '' if nothing clear changed.
    before/after: (url, flat element list). clicked: the clicked element's line ('div "Knowledge"'),
    or '' for click_at. title: the page title after the click. open_list: (field name, [option
    labels]) of a custom dropdown list that was open before the click, or None."""
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
    outcome = _list_outcome(open_list, clicked, gone)
    if outcome == "picked":
        return (f"✅ You chose {label}: its list closed. Check the field now shows it, then go on with "
                "the next field (don't reopen this list).")
    if outcome == "left":
        # Clicking the next field while a list is open closes that list with nothing chosen. Saying
        # only "it HID things" here sent the AI back to reopen the first list, round and round.
        opened = f", and opened {_names(brand_new)}" if _names(brand_new) else ""
        return (f"⚠️ Your click on {label} closed the open list ({_names(gone)}) WITHOUT choosing anything"
                f"{opened}. Nothing was selected.{_still_empty(open_list)} Fill the fields one at a time.")
    if added and sum(added.values()) >= sum(gone.values()):
        return (f"✅ Your click on {label} worked: it opened {_names(added)}. Don't click {label} again "
                "(that may close it). Pick one of these, or call done if this is what the task asked for.")
    if _names(brand_new) and _names(gone):
        # One panel swapped for another: say both, not just "it HID things".
        return (f"Your click on {label} closed {_names(gone)} and opened {_names(brand_new)}. "
                "Use what's open now, or call done if this is what the task asked for.")
    if gone:
        return (f"⚠️ Your click on {label} HID things that were on screen ({_names(gone)}). {label} probably "
                "opens/closes a panel, so your previous click had already done the job. Don't keep clicking "
                f"{label}: if you need those back, click it once more, then use them or call done.")
    return ""
