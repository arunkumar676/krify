"""The browser operations, described for the AI (tool calling). Descriptions are kept short on
purpose: they're sent with every request. The page (URL, title, numbered elements) is sent
automatically after every action, so there are no "look at the page" tools."""


def _tool(name, description, properties=None, required=None):
    parameters = {"type": "object", "properties": properties or {}}
    if required:
        parameters["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


ELEMENT = {"type": "integer", "description": "Element number, e.g. 7 for [7]"}
TAB = {"type": "integer", "description": "Number from [Tabs]"}
URL = {"type": "string", "description": "Full URL"}
TEXT = {"type": "string"}

TOOLS = [
    _tool("read_text", "Read on-screen text (prices, facts). scope=page: more than one screen (from the current "
          "screen down on a long page), with exact list/table row counts (for 'how many').",
          {"scope": {"type": "string", "enum": ["screen", "page"]}}),
    _tool("inspect", "Structure (headings, regions, nearby text) around an element or the page – only when the "
          "page view doesn't show what goes with what (which button belongs to which item). "
          "ref=one element's area; filter=\"all\" adds non-clickable context.",
          {"ref": ELEMENT, "filter": {"type": "string", "enum": ["interactive", "all"]},
           "depth": {"type": "integer"}}),
    _tool("find_elements", "Find an element by its visible text when it's not in the list. Matches synonyms "
          "(\"sign out\" finds \"Logout\"); if nothing matches, try another word. Returns [id] lines "
          "you can click/type on.",
          {"query": TEXT}, ["query"]),
    _tool("find_text", "Search the WHOLE page for a phrase/fact (any text, no length limit), jump to it "
          "and read around it; you get the page view there too. Same query again = the next match.",
          {"query": TEXT}, ["query"]),
    _tool("sheet_query", "Google Sheets: one SQL SELECT (DuckDB) on table sheet; quote column names, e.g. "
          "SELECT \"First Name\", \"Math Score\" FROM sheet WHERE \"Math Score\" = (SELECT MAX(\"Math Score\") FROM sheet)",
          {"sql": TEXT}, ["sql"]),
    _tool("screenshot", "See the screen as an image with element numbers. Costs more: only when text isn't "
          "enough (pictures, icons, colours, maps, charts)."),
    _tool("open_url", "Open a URL in this tab.", {"url": URL}, ["url"]),
    _tool("go_back", "Go back."),
    _tool("reload", "Reload the page."),
    _tool("new_tab", "Open a URL in a new tab.", {"url": URL}, ["url"]),
    _tool("switch_tab", "Switch tab.", {"tab": TAB}, ["tab"]),
    _tool("close_tab", "Close a tab you opened.", {"tab": TAB}, ["tab"]),
    _tool("click", "Click an element (or a Google Sheets cell: cell=\"B22\"). button=right opens its menu; "
          "count=2 double-click, 3 triple-click. real=true: the real mouse, for sites that ignore a normal click.",
          {"element": ELEMENT, "cell": TEXT, "button": {"type": "string", "enum": ["left", "right"]},
           "count": {"type": "integer"}, "real": {"type": "boolean"}}),
    _tool("click_at", "Click a spot (x, y) on your latest screenshot, for things without an element number "
          "(Sheets grid, charts, maps). Same button/count as click.",
          {"x": {"type": "number"}, "y": {"type": "number"}, "button": {"type": "string", "enum": ["left", "right"]},
           "count": {"type": "integer"}}, ["x", "y"]),
    _tool("drag", "Drag from an element or screenshot spot to an element or spot (sliders, cards, columns).",
          {"from_element": ELEMENT, "to_element": ELEMENT, "from_x": {"type": "number"}, "from_y": {"type": "number"},
           "to_x": {"type": "number"}, "to_y": {"type": "number"}}),
    _tool("hover", "Point at an element without clicking (opens hover menus). If no menu opened, try real=true.",
          {"element": ELEMENT, "real": {"type": "boolean"}}, ["element"]),
    _tool("type", "Type into a box (replaces its text). submit=true presses Enter after.",
          {"element": ELEMENT, "text": TEXT, "submit": {"type": "boolean"}}, ["element", "text"]),
    _tool("press_key", "Press a key or shortcut, e.g. Enter, Escape, Delete, ArrowDown, Ctrl+Z, Ctrl+End. "
          "repeat = how many times. Single Enter/Escape/Tab/arrows are simulated (Tab doesn't move focus – click "
          "the next field instead); the result says what Enter did.",
          {"key": TEXT, "repeat": {"type": "integer"}}, ["key"]),
    _tool("type_here", "Type where the cursor is, with the real keyboard (e.g. into a Google Sheets cell). "
          "\\t presses Tab, \\n presses Enter.", {"text": TEXT}, ["text"]),
    _tool("go_to_cell", "Google Sheets: select a cell or range, e.g. A130 or A1:C5 (checked before you type).",
          {"cell": TEXT}, ["cell"]),
    _tool("read_cells", "Google Sheets: exact values of a cell or range now, e.g. A130 or A128:C130.",
          {"cells": TEXT}, ["cells"]),
    _tool("scroll", "Scroll the page (top/bottom: jump there). element = scroll only the box/embedded page "
          "holding that element.",
          {"direction": {"type": "string", "enum": ["up", "down", "top", "bottom"]}, "element": ELEMENT}, ["direction"]),
    _tool("select_option", "Choose a dropdown option.", {"element": ELEMENT, "option": TEXT}, ["element", "option"]),
    _tool("wait", "Wait for loading (1-10 s).", {"seconds": {"type": "number"}}, ["seconds"]),
    _tool("batch", "Up to 5 actions in a row when sure without looking (e.g. a form). "
          "Each step: {op, ...its fields}. You see the page after the last step.",
          {"steps": {"type": "array", "items": {"type": "object", "properties": {
              "op": {"type": "string", "enum": ["type", "type_here", "go_to_cell", "click", "select_option", "press_key", "hover",
                                                "scroll", "wait"]},
              "element": {"type": "integer"}, "text": TEXT, "cell": TEXT, "submit": {"type": "boolean"}, "option": TEXT,
              "key": TEXT, "repeat": {"type": "integer"}, "direction": TEXT, "seconds": {"type": "number"}},
              "required": ["op"]}}},
          ["steps"]),
    _tool("plan", "Write/update your checklist; start finished steps with ✔. done_when: a new finish "
          "check, only when the site turned out different from what you planned.",
          {"steps": {"type": "array", "items": TEXT}, "done_when": TEXT}, ["steps"]),
    _tool("note", "Save a fact for later, e.g. 'Amazon: iPhone 16 128GB ₹69,900'.", {"text": TEXT}, ["text"]),
    _tool("done", "Task finished. summary: your answer to the user in plain English sentences "
          "(not JSON, not a line copied from the page), e.g. \"There are 33 people – the page lists 1–20 of 33.\"",
          {"summary": TEXT}, ["summary"]),
    _tool("ask_user", "Ask the user a question.", {"question": TEXT}, ["question"]),
]
