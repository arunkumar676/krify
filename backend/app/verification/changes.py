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
    return (f" {field} is still empty: use select_option with its field's number and the option's text"
            f"{example}.")


# First words that undo each other: after "Collapse sidebar", a new "Open sidebar" is its undo button.
_OPPOSITES = {
    "collapse": {"expand", "open", "show"}, "expand": {"collapse", "close", "hide"},
    "close": {"open", "show", "expand"}, "open": {"close", "hide", "collapse"},
    "hide": {"show", "open", "expand"}, "show": {"hide", "close", "collapse"},
    "minimize": {"maximize", "restore", "expand"}, "maximize": {"minimize", "restore"},
}
_CLOSING = {"collapse", "close", "hide", "minimize", "dismiss"}  # clicks meant to make things go away


def _full_name(line):
    """'button "Open sidebar"' -> 'Open sidebar' (not shortened, for comparing)."""
    quoted = re.search(r'"([^"]*)"', line or "")
    return " ".join(quoted.group(1).split()) if quoted else ""


def _undoes(clicked, other):
    """True if `other` undoes `clicked`: same words except an opposite first word
    ('Collapse sidebar' / 'Open sidebar', 'Close' / 'Open')."""
    a, b = clicked.lower().split(), other.lower().split()
    return bool(a and b) and b[0] in _OPPOSITES.get(a[0], ()) and a[1:] == b[1:]


def _words(text):
    """'AI Teammates' -> ('ai', 'teammate'): lowercase words, a plural 's' dropped ('agents' = 'agent')."""
    return tuple(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"[a-z0-9]+", (text or "").lower()))


# A task that is only "open one named place": "open agents", "go to the settings page".
_OPEN_TASK = re.compile(
    r"^\s*(?:please\s+)?(?:open|go\s+to|goto|navigate\s+to|take\s+me\s+to|show(?:\s+me)?)\s+"
    r"(?:the\s+|my\s+)?(.+?)(?:\s+(?:page|tab|section|screen))?\s*[.!]?\s*$", re.I)
# More to do after opening it ("open agents and create one"): the AI has to carry on, so no shortcut.
_MORE_TO_DO = re.compile(r"\b(?:and|then|after|also|search|find|create|add|type|click|with)\b|[,;]", re.I)
MAX_TARGET_WORDS = 4


def opened_what_asked(task, clicked):
    """True if the whole task was "open X" and the clicked element is labelled X (plural or not, any
    case): after that click opened a new page there's nothing left to do. clicked: 'div "Agents"'."""
    match = _OPEN_TASK.match(task or "")
    if not match or _MORE_TO_DO.search(match.group(1)):
        return False
    target = _words(match.group(1))
    return 0 < len(target) <= MAX_TARGET_WORDS and target == _words(_full_name(clicked))


def same_page(url1, url2):
    """Same page apart from ?query / #hash (a list filter that only changes the address: /contacts →
    /contacts?assignee=geetha). A real reload sends no diff anyway (content.js starts fresh)."""
    return (url1 or "").split("#")[0].split("?")[0] == (url2 or "").split("#")[0].split("?")[0]


def change_lines(changes):
    """The page's own diff after an action (content.js watchDiff) as + / - / ~ lines, '' if nothing
    changed or there's no diff (new page, scroll, no action)."""
    if not isinstance(changes, dict):
        return ""
    lines = []
    for key, sign in (("added", "+"), ("changed", "~"), ("removed", "-"), ("texts", "+ text:")):
        part = changes.get(key) or {}
        shown = part.get("shown") or []
        lines += [f"{sign} {line}" if key != "texts" else f'{sign} "{line}"' for line in shown]
        if part.get("more"):
            lines.append(f"{sign} (+{part['more']} more)")
    if not lines:
        return "Page changes after your action: none (nothing on the page changed)."
    return "Page changes after your action (+ new, ~ changed, - gone):\n" + "\n".join(lines)


def click_result(before, after, clicked, title="", open_list=None):
    """One sentence for the AI after a click, or '' if nothing clear changed.
    before/after: (url, flat element list). clicked: the clicked element's line ('div "Knowledge"'),
    or ''. title: the page title after the click. open_list: (field name, [option
    labels]) of a custom dropdown list that was open before the click, or None.
    A new button that would undo the click ("Open sidebar" after "Collapse sidebar") is never offered
    as a next step: it's named with a warning instead (the AI clicked it and undid its own work)."""
    (url1, flat1), (url2, flat2) = before, after
    old, new = _labels(flat1), _labels(flat2)
    added, gone = new - old, old - new  # Counter subtraction keeps only what really appeared / went
    label = f'"{_name(clicked)}"' if _name(clicked) else "that spot"
    clicked_name = _full_name(clicked)
    undo = [key for key in added if clicked_name and _undoes(clicked_name, _full_name(key))]
    added = Counter({key: n for key, n in added.items() if key not in undo})
    undo_note = (f' "{_name(undo[0])}" would undo your click – don\'t click it unless you need that back.'
                 if undo else "")
    # What went away apart from the clicked button itself (it often swaps for its undo button).
    gone_others = Counter({key: n for key, n in gone.items() if _full_name(key) != clicked_name})

    if url1 != url2:
        shown = f" New on screen: {_names(added)}." if _names(added) else ""
        # Asana's "Agents" opens "AI Teammates": the AI doubted it was there and clicked around.
        renamed = (f" Sites often name a page differently from the button that opens it: this IS the page "
                   f"{label} opens, even though it's called \"{_short(title)}\"."
                   if clicked_name and title and not set(_words(clicked_name)) & set(_words(title)) else "")
        return (f"✅ Your click on {label} worked: a new page opened (\"{_short(title)}\").{shown}{renamed} "
                f"Don't click {label} again.{undo_note} If this is what the task asked for, call done; "
                "otherwise continue.")
    # Only extra copies of things already on the page (e.g. "Load more" adding product tiles): say nothing.
    brand_new = [key for key in added if key not in old]
    if not _names(brand_new) and not _names(gone) and not undo:
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
                f"(that may close it).{undo_note} Pick one of these, or call done if this is what the task asked for.")
    if undo and not gone_others:  # only the button swapped ("Collapse sidebar" became "Open sidebar")
        return f"✅ Your click on {label} worked.{undo_note}"
    if gone and (clicked_name.lower().split() or [""])[0] in _CLOSING:
        # Collapse / Close / Hide: things going away is what it's for (the button itself isn't news).
        return (f"✅ Your click on {label} worked: it hid {_names(gone_others or gone)}.{undo_note} Don't click "
                f"{label} again; continue with the task, or call done if this is what it asked for.")
    if _names(brand_new) and _names(gone_others or gone):
        # One panel swapped for another: say both, not just "it HID things".
        return (f"Your click on {label} closed {_names(gone_others or gone)} and opened {_names(brand_new)}."
                f"{undo_note} Use what's open now, or call done if this is what the task asked for.")
    if gone_others:  # (only the clicked button gone = it went grey while the site works: not "hid")
        return (f"⚠️ Your click on {label} HID things that were on screen ({_names(gone_others)}). {label} probably "
                "opens/closes a panel, so your previous click had already done the job. Don't keep clicking "
                f"{label}: if you need those back, click it once more, then use them or call done.")
    return ""
