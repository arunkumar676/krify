"""The side panel's readable text for each tool call, e.g. '🖱️ Click [12]'."""


def describe(name, args):
    """Readable step text for the side panel, e.g. 'click [12]'."""
    match name:
        case "open_url": return f"🌐 Open {args.get('url')}"
        case "click":
            where = f"cell {args['cell']}" if args.get("cell") else f"[{args.get('element')}]"
            count = str(args.get("count") or 1)
            word = "Right-click" if args.get("button") == "right" else {"2": "Double-click", "3": "Triple-click"}.get(count, "Click")
            return f"🖱️ {word} {where}"
        case "drag":
            return f"✋ Drag [{args.get('from_element')}] → [{args.get('to_element')}]"
        case "hover": return f"👆 Hover over [{args.get('element')}]"
        case "type":
            enter = " + Enter" if args.get("submit") else ""
            return f"⌨️ Type \"{args.get('text')}\" into [{args.get('element')}]{enter}"
        case "press_key":
            times = f" × {args.get('repeat')}" if str(args.get("repeat") or 1) not in ("1", "0") else ""
            return f"⌨️ Press {args.get('key')}{times}"
        case "type_here": return f"⌨️ Type \"{args.get('text')}\" where the cursor is"
        case "go_to_cell": return f"📍 Go to cell {args.get('cell')}"
        case "read_cells": return f"📊 Read cells {args.get('cells')}"
        case "scroll":
            where = f" the box at [{args['element']}]" if args.get("element") is not None else ""
            return f"↕️ Scroll{where} {args.get('direction')}"
        case "select_option": return f"📋 Choose \"{args.get('option')}\" in [{args.get('element')}]"
        case "new_tab": return f"🗂️ New tab: {args.get('url')}"
        case "switch_tab": return f"🗂️ Switch to tab {args.get('tab')}"
        case "close_tab": return f"🗂️ Close tab {args.get('tab')}"
        case "go_back": return "⬅️ Go back"
        case "reload": return "🔄 Reload"
        case "wait": return f"⏳ Wait {args.get('seconds')}s"
        case "get_page_info" | "get_elements": return "👀 Look at the page"
        case "find_elements": return f"🔎 Looking for \"{args.get('query')}\""
        case "find_text": return f"🔎 Searching the page for \"{args.get('query')}\""
        case "read_text": return "📖 Read the whole page" if args.get("scope") == "page" else "📖 Read the screen"
        case "inspect": return f"🔍 Looking closer at [{args['ref']}]" if args.get("ref") else "🔍 Looking at the page structure"
        case "sheet_query": return f"📊 Ask the sheet: {args.get('sql')}"
        case _: return f"{name} {args}"
