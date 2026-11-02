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
    _tool("read_text", "Read text on screen (prices, facts). scope=page reads the whole page.",
          {"scope": {"type": "string", "enum": ["screen", "page"]}}),
    _tool("sheet_query", "Google Sheets: one SQL SELECT (DuckDB) on table sheet; quote column names, e.g. "
          "SELECT \"First Name\", \"Math Score\" FROM sheet WHERE \"Math Score\" = (SELECT MAX(\"Math Score\") FROM sheet)",
          {"sql": TEXT}, ["sql"]),
    _tool("screenshot", "See the screen as an image, with element numbers drawn on it. Costs more: use only "
          "when text isn't enough (pictures, icons, colours, maps, charts)."),
    _tool("open_url", "Open a URL in this tab.", {"url": URL}, ["url"]),
    _tool("go_back", "Go back."),
    _tool("reload", "Reload the page."),
    _tool("new_tab", "Open a URL in a new tab.", {"url": URL}, ["url"]),
    _tool("switch_tab", "Switch tab.", {"tab": TAB}, ["tab"]),
    _tool("close_tab", "Close a tab you opened.", {"tab": TAB}, ["tab"]),
    _tool("click", "Click an element. button=right opens its menu; count=2 double-click, 3 triple-click.",
          {"element": ELEMENT, "button": {"type": "string", "enum": ["left", "right"]}, "count": {"type": "integer"}},
          ["element"]),
    _tool("click_at", "Click a spot (x, y) on your latest screenshot, for things without an element number "
          "(Sheets grid, charts, maps). Same button/count as click.",
          {"x": {"type": "number"}, "y": {"type": "number"}, "button": {"type": "string", "enum": ["left", "right"]},
           "count": {"type": "integer"}}, ["x", "y"]),
    _tool("drag", "Drag and drop: from an element or a screenshot spot, to an element or a spot (sliders, cards, columns).",
          {"from_element": ELEMENT, "to_element": ELEMENT, "from_x": {"type": "number"}, "from_y": {"type": "number"},
           "to_x": {"type": "number"}, "to_y": {"type": "number"}}),
    _tool("hover", "Point at an element without clicking (opens hover menus). If no menu opened, try real=true.",
          {"element": ELEMENT, "real": {"type": "boolean"}}, ["element"]),
    _tool("type", "Type into a box (replaces its text). submit=true presses Enter after.",
          {"element": ELEMENT, "text": TEXT, "submit": {"type": "boolean"}}, ["element", "text"]),
    _tool("press_key", "Press a key or shortcut (real keyboard), e.g. Enter, Tab, Escape, Delete, ArrowDown, "
          "Ctrl+Z, Ctrl+End. repeat = how many times.",
          {"key": TEXT, "repeat": {"type": "integer"}}, ["key"]),
    _tool("type_here", "Type where the cursor is, with the real keyboard (e.g. into a Google Sheets cell). "
          "\\t presses Tab, \\n presses Enter.", {"text": TEXT}, ["text"]),
    _tool("go_to_cell", "Google Sheets: select a cell or range, e.g. A130 or A1:C5 (checked before you type).",
          {"cell": TEXT}, ["cell"]),
    _tool("read_cells", "Google Sheets: exact values of a cell or range now, e.g. A130 or A128:C130.",
          {"cells": TEXT}, ["cells"]),
    _tool("scroll", "Scroll the page.", {"direction": {"type": "string", "enum": ["up", "down"]}}, ["direction"]),
    _tool("select_option", "Choose a dropdown option.", {"element": ELEMENT, "option": TEXT}, ["element", "option"]),
    _tool("wait", "Wait for loading (1-10 s).", {"seconds": {"type": "number"}}, ["seconds"]),
    _tool("batch", "Do up to 5 actions in a row when you're sure of them without looking, e.g. filling a form. "
          "Each step: {op, ...that action's fields}. You see the page after the last step.",
          {"steps": {"type": "array", "items": {"type": "object", "properties": {
              "op": {"type": "string", "enum": ["type", "type_here", "go_to_cell", "click", "select_option", "press_key", "hover",
                                                "scroll", "wait"]},
              "element": {"type": "integer"}, "text": TEXT, "cell": TEXT, "submit": {"type": "boolean"}, "option": TEXT,
              "key": TEXT, "repeat": {"type": "integer"}, "direction": TEXT, "seconds": {"type": "number"}},
              "required": ["op"]}}},
          ["steps"]),
    _tool("plan", "Write/update your checklist; start finished steps with ✔.",
          {"steps": {"type": "array", "items": TEXT}}, ["steps"]),
    _tool("note", "Save a fact for later, e.g. 'Amazon: iPhone 16 128GB ₹69,900'.", {"text": TEXT}, ["text"]),
    _tool("done", "Task finished: give the result.", {"summary": TEXT}, ["summary"]),
    _tool("ask_user", "Ask the user a question.", {"question": TEXT}, ["question"]),
]
