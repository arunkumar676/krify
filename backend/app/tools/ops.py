"""Which tool calls are which: the op-name sets the agent loop dispatches on, and the batch rules."""

import json

READ_OPS = {"video", "get_page_info", "get_elements", "find_elements", "find_text", "read_text", "sheet_query", "read_cells", "inspect"}
SHEET_OPS = {"sheet_query", "read_cells"}
SHEET_ONLY_TOOLS = SHEET_OPS | {"go_to_cell"}  # left out of the tool list until a Sheet is open (saves tokens)

# Batches: actions the AI may send several of in one reply.
ACTION_OPS = {"open_url", "go_back", "reload", "new_tab", "switch_tab", "close_tab", "click", "hover", "type",
              "type_here", "go_to_cell", "press_key", "scroll", "select_option", "wait", "drag"}
MAX_BATCH = 5
BATCH_LIMIT = "Skipped: at most 5 steps at once. Look at the page and continue."
PAGE_MOVED = ("Skipped: the page changed after an earlier step, so element numbers may be different. "
              "Look at the new page and decide again.")
STEP_FAILED = "Skipped: an earlier step in this batch failed. Look at the page and decide again."

BATCH_REFUSED = "Skipped: an earlier step in this batch was not done. Look at the page and decide again."

ELEMENT_FIELDS = ("element", "from_element", "to_element")


def uses_element(args):
    """Does this step point at an element number? Only those can hit the wrong thing when the page
    changes; steps by cell (go_to_cell, type_here, click cell=) or keys can't."""
    return any(args.get(f) is not None for f in ELEMENT_FIELDS)


def _args_of(call):
    try:
        return json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        return {}
