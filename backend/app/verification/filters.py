"""Before a "not found" answer: the list on the page may just be filtered."""

import re

# Filter dropdowns on a list page ("My Activities") show only their current choice, and rows outside
# the filter aren't on the page at all: a search that finds nothing may just be filtered out.
FILTER_HINT = ("\nThis page has dropdowns that may be filtering what's listed: {}. If what you're looking for could be "
               "hidden by one (\"My …\" = only yours), change it with select_option first (e.g. option = the person's "
               "name), then look again.")
FILTER_CHECK = ("REJECTED – not sent to the user: you're saying it isn't there, but this page's list may be filtered "
                "by {} and you haven't changed any filter. Set the filter that matches the request with select_option "
                "(e.g. option = the person's name or the type), read the list again, then answer.")
NOT_FOUND = re.compile(r"\b(no|none|not found|couldn'?t find|could not find|nothing|zero|isn'?t any|aren'?t any|"
                       r"there are no|didn'?t find|did not find)\b", re.I)
_DROPDOWN_LINE = re.compile(r'^\[(\d+)\] (?:dropdown|select) "([^"]*)"', re.M)


def filter_dropdowns(state):
    """Dropdowns on the page outside the form being filled – list filters such as
    '[16] "My Activities"' (at most 4)."""
    in_form = {str(f.get("id")) for f in (state.form or {}).get("fields") or []}
    found = [f'[{n}] "{label}"' for n, label in _DROPDOWN_LINE.findall(state.elements or "") if n not in in_form]
    return found[:4]


def check_filters(state, summary):
    """Before a 'not found' answer: reject once if the page has filter dropdowns and none was
    changed this task – the rows may just be filtered out ("My Activities" hid Geetha's)."""
    if state.filter_check_failed or not NOT_FOUND.search(summary or ""):
        return None
    filters = filter_dropdowns(state)
    if not filters or any(op == "select_option" for op, _, _ in state.actions):
        return None
    state.filter_check_failed = True
    return FILTER_CHECK.format(", ".join(filters))
