"""The 14 basic browser operations, described for the AI (Groq tool calling)."""


def _tool(name, description, properties=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


ELEMENT = {"type": "integer", "description": "Element number from the page list, e.g. 7 for [7]"}

TOOLS = [
    # See the page
    _tool("get_page_info", "Get the current page URL and title."),
    _tool("get_elements", "List the visible buttons, links, inputs and dropdowns, each with a number."),
    _tool("read_text", "Read the main text of the current page."),
    # Navigate
    _tool(
        "open_url",
        "Open a website in the current tab.",
        {"url": {"type": "string", "description": "Full URL, e.g. https://www.google.com"}},
        ["url"],
    ),
    _tool("go_back", "Go back to the previous page."),
    _tool("reload", "Reload the current page."),
    # Interact
    _tool("click", "Click an element.", {"element": ELEMENT}, ["element"]),
    _tool(
        "type",
        "Type text into an input box. Replaces what is already there.",
        {"element": ELEMENT, "text": {"type": "string", "description": "Text to type"}},
        ["element", "text"],
    ),
    _tool(
        "press_key",
        "Press a keyboard key, e.g. Enter to submit or Escape to close a pop-up.",
        {"key": {"type": "string", "enum": ["Enter", "Escape", "Tab", "ArrowDown", "ArrowUp"]}},
        ["key"],
    ),
    _tool(
        "scroll",
        "Scroll the page to see more.",
        {"direction": {"type": "string", "enum": ["up", "down"]}},
        ["direction"],
    ),
    _tool(
        "select_option",
        "Choose an option from a dropdown.",
        {"element": ELEMENT, "option": {"type": "string", "description": "Visible text of the option"}},
        ["element", "option"],
    ),
    # Control
    _tool(
        "wait",
        "Wait for the page to finish loading.",
        {"seconds": {"type": "number", "description": "1 to 10 seconds"}},
        ["seconds"],
    ),
    _tool(
        "done",
        "The task is finished. Report the result to the user.",
        {"summary": {"type": "string", "description": "Short result for the user"}},
        ["summary"],
    ),
    _tool(
        "ask_user",
        "Ask the user a question when the request is unclear or a choice is needed.",
        {"question": {"type": "string"}},
        ["question"],
    ),
]
