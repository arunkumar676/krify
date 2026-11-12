"""Structured page view: turns the extension's page model (get_page_model, built by content.js
buildPageModel) into what the AI reads, plus the old flat element list the code itself relies on.

The extension always collects the WHOLE page; this module decides how much of it the AI sees:
- Restricted (default): what's on screen, an open dialog, the form being filled; long nav/sidebar
  lists cut short, the footer collapsed; off-screen parts summarised as counts.
- Full (the side panel's Unrestricted switch): everything, with off-screen parts marked.

The flat list (`View.flat`) always holds EVERY collected element in the old `[n] role "label"`
format, so safety.check, the fact check and merge_elements see the whole page, never just the
shortened view the AI got.

Try it on a saved page model:  uv run python -m app.context.pageview fixtures/amazon_search.json [--full]
"""

import json
import re
import sys
from dataclasses import dataclass

MAX_SHOWN = 100  # elements shown in restricted mode (the old flat list's cap)
SECTION_LIMITS = {"nav": 8, "aside": 6, "toolbar": 12, "menubar": 12}  # on-screen elements shown per section
COLLAPSED = {"footer"}  # sections shown only as a count in restricted mode
# Which on-screen elements win when there are more than MAX_SHOWN (lower = first).
PRIORITY = {"dialog": 0, "menu": 0, "form": 1, "search": 1, "main": 2, "page": 2, "region": 2, "header": 3,
            "aside": 4, "nav": 5, "toolbar": 5, "menubar": 5, "footer": 6}
POPUP_KINDS = {"dialog", "menu"}
# Controls worth naming even off screen or past a section's cap: paging and form submit buttons –
# on a long page they're what moves the task on, and a count ("+40 below") hides them.
KEY_CONTROL = re.compile(r'^(button submit|input submit|input image)\b|"\s*(next( page)?|previous( page)?|prev|load more|'
                         r'show more|see more|view more|more results|›|»|‹|«)\s*"', re.I)
MAX_KEY_CONTROLS = 6
MAX_AMBIGUITY_GROUPS = 3
MAX_FORM_REQUIRED = 12  # names listed in the [Form] checklist
MAX_FORM_OPTIONAL = 10
POSITION = {-1: "above the screen", 1: "below the screen", 2: "off to the side"}


@dataclass
class View:
    text: str  # what the AI reads
    flat: str  # every collected element, old format – for safety/fact checks
    status: str  # the page's own status/error message ("" if none)
    hint: str  # warning about identical elements the view can't tell apart ("" if none)
    # An open custom dropdown list: (its field's name, [option labels]), or None. Lets the loop tell
    # the AI when a click closed the list without choosing anything (changes.click_result).
    open_list: tuple | None = None
    # The form being filled (content.js formSummary): {name, marks, fields, submits}, or None.
    form: dict | None = None
    full: bool = False  # Unrestricted mode: nothing was cut (the loop then lifts its own caps too)


@dataclass
class _Entry:
    el: dict
    page: int
    section: tuple  # (page index, section index)
    card: tuple | None  # (page index, card index)
    order: int


def dupes_warning(dupes):
    """[(label, [ids])] → the warning line(s) asking the AI to look closer before clicking."""
    return "\n".join(
        f"⚠️ Same label on {','.join(f'[{i}]' for i in ids)} (\"{label}\") – call inspect ref=<one of "
        "them> to see which is which before clicking."
        for label, ids in dupes[:MAX_AMBIGUITY_GROUPS]
    )


def _scroll(page):
    y, top = page.get("scrollY", 0), page.get("scrollMax", 0)
    if top <= 4:
        return "whole page fits on screen"
    if y <= 4:
        return "at the top, more below"
    if y >= top - 50:
        return "at the bottom"
    return f"{round(100 * y / top)}% down, more above and below"


def _field(f):
    """'Contact [16] ✗ empty' / 'Subject [2] ✓ "Dummy Activity"' for the [Form] checklist."""
    name = f"{f.get('label') or '(no label)'} [{f['id']}]"
    if f.get("kind") == "dropdown":
        name += " (dropdown)"
    if f.get("filled"):
        state = f'✓ "{f["value"]}"' if f.get("value") else "✓"
    else:
        state = "✗ empty"
    if f.get("disabled") and not f.get("filled"):
        state += " (disabled – opens after an earlier field is chosen)"
    if f.get("error"):
        state += f' (error: "{f["error"]}")'
    return f"{name} {state}"


def form_lines(form, full=False):
    """The [Form] checklist: which fields are required (filled ✓ / empty ✗) and which are optional,
    so the AI fills the right ones and knows what's still missing. [] if there's no form."""
    if not form or not form.get("fields"):
        return []
    name = f' "{form["name"]}"' if form.get("name") else ""
    fields = form["fields"]
    cap_required, cap_optional = (None, None) if full else (MAX_FORM_REQUIRED, MAX_FORM_OPTIONAL)
    if not form.get("marks"):
        filled = [f for f in fields if f.get("filled")]
        done = f" Filled: {', '.join(_field(f) for f in filled[:cap_required])}." if filled else ""
        return [f"[Form{name}] It marks no fields as required: fill what the user gave; after saving, "
                f"read [Status] for anything missing.{done}"]
    required = [f for f in fields if f.get("required")]
    optional = [f for f in fields if not f.get("required")]
    more = f" (+{len(required) - cap_required} more)" if cap_required and len(required) > cap_required else ""
    lines = [f"[Form{name}] Required: " + " · ".join(_field(f) for f in required[:cap_required]) + more]
    if optional:
        names = ", ".join(f"{f.get('label') or '(no label)'} [{f['id']}]" + (" ✓" if f.get("filled") else "")
                          for f in optional[:cap_optional])
        extra = f" (+{len(optional) - cap_optional} more)" if cap_optional and len(optional) > cap_optional else ""
        lines.append(f"Optional (fill only if the user gave a value; don't ask about these): {names}{extra}")
    return lines


def missing_required(form):
    """Labels of required fields that are still empty ([] if none, or the form marks nothing)."""
    if not form or not form.get("marks"):
        return []
    return [f.get("label") or f"[{f['id']}]" for f in form.get("fields") or [] if f.get("required") and not f.get("filled")]


def _label(text):
    """'clickable "Amit Verma"' → 'Amit Verma' ('' without a quoted label)."""
    quoted = re.search(r'"([^"]*)"', text or "")
    return quoted.group(1) if quoted else ""


def _element_line(el):
    line = f"[{el['id']}] {el['text']}"
    if el.get("states"):
        line += f" ({', '.join(el['states'])})"
    if el.get("col"):
        line += f' · column "{el["col"]}"'
    if el.get("hint"):
        line += f' · hint: "{el["hint"]}"'
    return line


def _card_line(card, pos):
    if not card or not card.get("summarised"):
        return f"▸ (item {POSITION.get(pos, 'off screen')})"
    badges = list(card.get("badges") or [])
    parts = []
    if "Sponsored" in badges:
        badges.remove("Sponsored")
        parts.append("Sponsored ·")
    parts.append(f'"{card.get("title", "")}"')
    if card.get("price"):
        parts.append(card["price"])
    if card.get("rating"):
        parts.append(card["rating"])
    line = " ".join(parts)
    if badges:
        line += " · " + " · ".join(badges)
    return f"▸ {line}"


def flat_lines(model):
    """Every collected element in the old get_elements format: `[n] role "label" (...)`."""
    parts = []
    for i, page in enumerate(model.get("pages") or []):
        lines = []
        if page.get("status"):
            lines.append(f"[Status] {page['status']}")
        if page.get("popup"):
            lines.append("(an open menu/dialog is listed first)")
        lines += [f"[{el['id']}] {el['text']}" for el in page.get("elements") or []]
        if page.get("partial"):
            lines.append("(more elements not listed – the page is very large)")
        info = page.get("page") or {}
        if i == 0 and info.get("scrollMax", 0) - info.get("scrollY", 0) > 50:
            lines.append("(more content below – scroll down to see it)")
        if not lines:
            continue
        text = "\n".join(lines)
        parts.append(f'--- inside embedded page "{page.get("frame")}" ---\n{text}' if i else text)
    return "\n".join(parts) or "(no interactive elements visible)"


def _entries(pages):
    entries = []
    for p, page in enumerate(pages):
        for el in page.get("elements") or []:
            card = (p, el["c"]) if el.get("c", -1) >= 0 else None
            entries.append(_Entry(el, p, (p, el["s"]), card, len(entries)))
    return entries


def _choose(entries, sections, focused, full):
    """Which elements the AI sees. Full: all. Restricted: popups, the focused form, then on-screen
    elements by section priority, up to MAX_SHOWN."""
    if full:
        return {e.order for e in entries}
    focus_sections = {e.section for e in entries if e.el["id"] in focused
                      and "form" in sections[e.section]["kinds"]}
    must, candidates, per_section, key_controls = [], [], {}, []
    for e in entries:
        sec = sections[e.section]
        key = bool(KEY_CONTROL.search(e.el["text"]))
        if POPUP_KINDS & set(sec["kinds"]) or e.el["id"] in focused or e.section in focus_sections:
            must.append(e)
        elif e.el.get("pos", 0) == 0 and not COLLAPSED & set(sec["kinds"]):
            per_section[e.section] = per_section.get(e.section, 0) + 1
            limit = SECTION_LIMITS.get(sec["kind"])
            if limit is None or per_section[e.section] <= limit or key:
                candidates.append(e)
        elif key:
            key_controls.append(e)  # off screen, or in the footer: named anyway (marked where it is)
    shown = must[:MAX_SHOWN]
    shown += key_controls[:MAX_KEY_CONTROLS]
    candidates.sort(key=lambda e: (PRIORITY.get(sections[e.section]["kind"], 2), e.order))
    shown += candidates[:max(0, MAX_SHOWN - len(shown))]
    return {e.order for e in shown}


def _ambiguity(entries, shown):
    """Shown elements with the same description and no item line to tell them apart: twice inside
    one item, or several with no item at all. (Identical buttons in different items are fine –
    the ▸ line above each says which is which – and so are ones whose states differ, e.g. a
    dropdown field "expanded" and its list's "placeholder" option with the same text.)"""
    groups = {}
    for e in entries:
        if e.order in shown and '"' in e.el["text"]:
            groups.setdefault((e.el["text"], tuple(e.el.get("states") or ()), e.card), []).append(e.el["id"])
    dupes = [(text.split('"')[1], ids) for (text, _, _), ids in groups.items() if len(ids) > 1]
    return dupes_warning(dupes) if dupes else ""


def render(model):
    """→ View: the outline the AI reads, the flat list, the status line and an ambiguity hint."""
    pages = model.get("pages") or []
    full = bool(model.get("full"))
    if not pages:
        return View("(no interactive elements visible)", "(no interactive elements visible)", "", "", None)
    sections, cards = {}, {}
    for p, page in enumerate(pages):
        prefix = f'Embedded page "{page.get("frame")}" › ' if p else ""
        for s, sec in enumerate(page.get("sections") or []):
            sections[(p, s)] = {**sec, "name": prefix + sec["name"]}
        for c, card in enumerate(page.get("cards") or []):
            cards[(p, c)] = card
    entries = _entries(pages)
    focused = {page["focused"] for page in pages if page.get("focused")}
    shown = _choose(entries, sections, focused, full)

    info = pages[0].get("page") or {}
    head = [x for x in (info.get("type"), f'"{info["h1"]}"' if info.get("h1") else "", info.get("results"),
                        _scroll(info)) if x]
    lines = ["[Page] " + " · ".join(head)]
    statuses = [page["status"] for page in pages if page.get("status")]
    if statuses:
        lines.append(f"[Status] {' | '.join(statuses)}")
    if focused:
        lines.append("[Focused] " + " ".join(f"[{i}]" for i in sorted(focused)))
    form = next((page["form"] for page in pages if page.get("form")), None)
    lines += form_lines(form, full)

    by_section = {}
    for e in entries:
        by_section.setdefault(e.section, []).append(e)
    # An open dropdown list goes first: its options are numbered after everything else (so opening
    # it doesn't shift the form's numbers) but they're what the AI should pick from next.
    ordered = sorted(by_section.items(), key=lambda item: not sections[item[0]].get("list"))
    open_list = next(((sections[key]["name"], [_label(e.el["text"]) for e in group  # options, not its search box
                                                if not e.el["text"].startswith(("input", "textarea"))])
                      for key, group in ordered if sections[key].get("list")), None)
    off_screen = []
    for key, group in ordered:
        sec = sections[key]
        visible = [e for e in group if e.order in shown]
        hidden = [e for e in group if e.order not in shown]
        cut = sum(1 for e in hidden if e.el.get("pos", 0) == 0)
        where = {pos: sum(1 for e in hidden if e.el.get("pos") == pos) for pos in POSITION}
        if not visible:
            if cut:
                lines.append(f"## {sec['name']} ({cut} not shown – find_elements)")
            else:
                counts = ", ".join(f"{n} {POSITION[pos].replace(' the screen', '')}" for pos, n in where.items() if n)
                off_screen.append(f"{sec['name']}: {counts}")
            continue
        header = f"## {sec['name']}"
        items = len({e.card for e in group if e.card})
        if items >= 3:
            header += f" ({items} items)"
        if sec.get("modal"):
            header += " (modal – blocks the page)"
        if sec.get("list"):
            header += " (open dropdown list – click one of these options, or select_option on its field)"
        lines.append(header)
        card, pos = None, 0
        # A clickable table row is its own card: its `row link "…"` line is the ▸ line, with the
        # row's own buttons/links indented under it (a card summary would repeat the same cells).
        row_ids = {cards[e.card]["row"] for e in visible if e.card and cards.get(e.card, {}).get("row")}
        rows = {e.card: e for e in visible if e.el["id"] in row_ids}
        for e in visible:
            el_pos = e.el.get("pos", 0)
            if el_pos != pos:
                lines.append(f"-- {POSITION.get(el_pos, 'on screen')} --")
                pos = el_pos
            if e.card != card:
                card = e.card
                if card in rows:
                    lines.append("▸ " + _element_line(rows[card].el))
                elif card:
                    lines.append(_card_line(cards.get(card), el_pos))
            if card in rows and e is rows[card]:
                continue
            lines.append(("  " if card else "") + _element_line(e.el))
        more = [f"{cut} more on screen"] if cut else []
        more += [f"{n} {POSITION[p]}" for p, n in where.items() if n]
        if more:
            how = "use find_elements" if len(hidden) == cut else "scroll there or use find_elements"
            lines.append(f"(+{len(hidden)} more here: {', '.join(more)} – {how})")
    if off_screen:
        lines.append(f"[Not on screen] {' · '.join(off_screen)} – scroll there or use find_elements")
    if any(page.get("partial") for page in pages):
        lines.append("(very large page: far-off parts weren't listed – scroll there to see them)")
    return View("\n".join(lines), flat_lines(model), statuses[0] if statuses else "", _ambiguity(entries, shown),
                open_list, form, full)


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    if "--full" in sys.argv:
        data["full"] = True
    view = render(data)
    print(view.text)
    if view.hint:
        print(view.hint)
    if "--flat" in sys.argv:
        print("\n--- flat (for safety/fact checks) ---\n" + view.flat)
