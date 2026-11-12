"""The flat element list ('[12] button "Save"'): looking elements up, comparing layouts, merging finds."""

import re

from app.context import pageview

# "[12] input text "Email"" – an element's number, type and label, without its value/ticks/states.
_ELEMENT_KEY = re.compile(r'\[\d+\] [^"\n]*"[^"\n]*"')


def element_label(elements, element_id):
    """The label of element [n] in the flat element list ("" if it isn't there)."""
    try:
        n = int(element_id)
    except (TypeError, ValueError):
        return ""
    match = re.search(rf'^\s*\[{n}\] [^"\n]*"([^"\n]*)"', elements or "", re.M)
    return match.group(1) if match else ""


def _line_key(line):
    """'input text "Email" (value: "x")' -> 'input text "Email"': what the element is, not its state."""
    match = re.match(r'[^"\n]*"[^"\n]*"', line or "")
    return match.group(0) if match else (line or "")


def layout(page):
    """Which number points at which element (number, type, label) – typed values, ticks, states,
    the focus and status lines left out – to see if element numbers could have moved."""
    return "\n".join(_ELEMENT_KEY.findall(page or ""))


_ELEMENT_ID = re.compile(r"^\[(\d+)\] ", re.MULTILINE)


def merge_elements(elements, found):
    """Fold find_elements matches into the stored element blob (by id), so safety.check's
    element_line() can see elements discovered outside the shown window this turn. A match for an
    id already there replaces its line: the page's own description of it now is what counts."""
    new = {m.group(1): ln for ln in (found or "").splitlines() if (m := re.match(r"^\[(\d+)\] ", ln))}
    if not new:
        return elements
    kept = [ln for ln in (elements or "").splitlines() if not ((m := re.match(r"^\[(\d+)\] ", ln)) and m.group(1) in new)]
    return "\n".join(kept + list(new.values()))


_ELEMENT_LINE = re.compile(r'^\[(\d+)\] (\S+) "([^"]+)"', re.MULTILINE)


def ambiguity_hint(elements):
    """Flag elements that share a role+label (e.g. three identical 'Add to cart' buttons): the
    model is meant to call inspect ref=<id> first to tell them apart, but a low-effort model often
    skips that unless nudged."""
    groups = {}
    for eid, role, label in _ELEMENT_LINE.findall(elements or ""):
        groups.setdefault((role, label), []).append(eid)
    dupes = [(label, ids) for (role, label), ids in groups.items() if len(ids) > 1]
    return pageview.dupes_warning(dupes) if dupes else ""
