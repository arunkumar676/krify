"""Does the element number the AI sent still point at the element it means? (Numbers change every view.)"""

import os
import re

from app.context.element_list import _line_key
from app.guardrails import safety

# A batch step on an element that wasn't in the page view the AI sent the batch from (a dropdown's
# options appear only once it's open – the AI guessed their numbers and hit a sidebar link instead).
NOT_IN_VIEW = ("Skipped: [{}] wasn't in the page view you sent these steps from – it appeared only after an earlier "
               "step (a dropdown's options exist only while it's open). Look at the page now; for a dropdown use "
               "select_option with the field and the option's text.")
CHANGED_IN_BATCH = ("Skipped: [{}] is now {}, but when you sent these steps it was {} – an earlier step changed the "
                    "page. Look at the new page and decide again.")

# The AI's label for [n] doesn't fit what [n] is now: it used a number from an older view.
WRONG_ELEMENT = ('NOT done: [{id}] is {line}, not "{label}". Element numbers change with every page view – use the '
                 'number shown next to "{label}" in the LATEST page view{hint}.')

# Words that say what KIND of element it is, not which one: "Save button" is the "Save" element.
_KIND_WORDS = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "button", "link", "field", "box", "input",
               "dropdown", "option", "icon", "menu", "tab", "text", "clickable", "select", "choose", "enter", "your",
               "item", "list", "checkbox"}


def _words_of(text):
    """'Opportunity Titles' -> {'opportunity', 'title'}: lowercase words, a plural 's' dropped, kind words out."""
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"[a-z0-9]+", text.lower())} - _KIND_WORDS


def _same_word(a, b):
    """Same word, or the same stem ('opportunity' / 'opportunitie', 'create' / 'created')."""
    if a == b:
        return True
    common = len(os.path.commonprefix([a, b]))
    return common >= 4 and common >= min(len(a), len(b)) - 2


def label_matches(label, names):
    """Does the AI's label for an element fit what the element is now? names: its element line(s),
    form-field label and value. Short labels must match every word ("Create Opportunity" is not the
    "Opportunities" link); longer ones two thirds of their words."""
    want = " ".join(str(label).lower().split()).strip(" \"'")
    have = " ".join(" ".join(names).lower().split())
    if not want or (len(want) >= 3 and want in have):
        return True
    words, have_words = _words_of(want), _words_of(have)
    if not words:
        return True
    hits = sum(any(_same_word(w, h) for h in have_words) for w in words)
    return hits == len(words) if len(words) <= 2 else hits / len(words) >= 2 / 3


def element_names(state, element_id):
    """What [n] is in the latest view: its flat line, its structured line (with the field's visible
    label as a hint) and, for a form field, the [Form] checklist's label and value."""
    names = []
    if line := safety.element_line(state.elements, element_id):
        names.append(line)
    try:
        n = int(element_id)
    except (TypeError, ValueError):
        return names
    if match := re.search(rf"^\s*\[{n}\] (.*)$", state.view_text or "", re.M):
        names.append(match.group(1))
    for f in (state.form or {}).get("fields") or []:
        if str(f.get("id")) == str(n):
            names.append(f'"{f.get("label", "")}" "{f.get("value", "")}"')
    return names


def wrong_element(state, element_id, label):
    """The refusal when the AI's label for [n] doesn't fit what [n] is now ('' if it fits, or if
    either is unknown). Names the number(s) that do carry that label now, if any."""
    label = str(label or "").strip()
    names = element_names(state, element_id)
    if not label or not names or label_matches(label, names):
        return ""
    now = [m.group(1) for m in re.finditer(r'^\[(\d+)\] [^\n]*$', state.elements or "", re.M)
           if label_matches(label, [m.group(0)[len(m.group(1)) + 3:]])]
    if now:
        hint = f" (\"{label}\" is {', '.join(f'[{i}]' for i in now[:3])} now)"
    else:
        hint = (" – it isn't on the page now. A dropdown's options exist only while it's open: use select_option "
                "with the dropdown field and the option's text")
    line = safety.element_line(state.elements, element_id)
    return WRONG_ELEMENT.format(id=element_id, line=_line_key(line) or line, label=label, hint=hint)
