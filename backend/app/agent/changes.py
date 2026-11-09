"""What an action changed on the page, as one short line for the AI.

The agent already has the page view before and after every action. Instead of making the model
spot the difference in two long element lists, this compares them and says it plainly:

    Changed: URL /list → /agents. New (12): "New agent", "Templates", … (+9 more). Gone (8): …
    Nothing changed on the page – the action may not have worked.

Elements are compared by their label, not their [number], because numbers shift when the page
changes. Duplicate labels (several "Add to cart" buttons) are counted, not merged.
"""

import re
from collections import Counter
from urllib.parse import urlparse

_HEADER = re.compile(r"^\[Current page\] (.*) – (\S*)$", re.M)
_ELEMENT = re.compile(r"^\[\d+\] (.+)$", re.M)
# Typed values and ticks: a change there isn't a new element (same rule as loop.layout()).
_VALUES = re.compile(r' \((?:value: ".*?"|empty|checked|unchecked)\)| = ".*?"')
MAX_LABEL = 40


def _parse(page):
    """(title, url, Counter of element labels without values, raw element lines) – None if unreadable."""
    if not page or "(can't read it" in page:
        return None
    header = _HEADER.search(page)
    title, url = (header.group(1), header.group(2)) if header else ("", "")
    raw = _ELEMENT.findall(page)
    return title, url, Counter(_VALUES.sub("", line) for line in raw), raw


def _short(label):
    return label if len(label) <= MAX_LABEL else label[: MAX_LABEL - 1] + "…"


def _names(counter, limit):
    labels = list(counter.elements())
    shown = ", ".join(_short(x) for x in labels[:limit])
    more = f" (+{len(labels) - limit} more)" if len(labels) > limit else ""
    return f"({len(labels)}): {shown}{more}"


def _url_change(old, new):
    """'/list → /agents' on the same site, the full addresses when the site changed."""
    a, b = urlparse(old), urlparse(new)
    if a.netloc == b.netloc:
        def rest(u):
            return (u.path or "/") + (f"?{u.query}" if u.query else "") + (f"#{u.fragment}" if u.fragment else "")
        old, new = rest(a), rest(b)
    return f"{old[:80]} → {new[:80]}"


def what_changed(before, after, limit=5):
    """One line saying what changed between two page views ('' if either can't be compared)."""
    old, new = _parse(before), _parse(after)
    if not old or not new:
        return ""
    (t1, u1, els1, raw1), (t2, u2, els2, raw2) = old, new
    parts = []
    if u1 != u2:
        parts.append(f"URL {_url_change(u1, u2)}")
    elif t1 != t2:
        parts.append(f'title "{_short(t1)}" → "{_short(t2)}"')
    added, gone = els2 - els1, els1 - els2
    if added:
        parts.append(f"New {_names(added, limit)}")
    if gone:
        parts.append(f"Gone {_names(gone, limit)}")
    if not parts and raw1 != raw2:
        parts.append("only a typed value or tick changed")
    if parts:
        return "Changed: " + ". ".join(parts) + "."
    if before.strip() != after.strip():
        return "Changed: something outside the element list (e.g. page notes or tabs)."
    return "Nothing changed on the page – the action may not have worked."
