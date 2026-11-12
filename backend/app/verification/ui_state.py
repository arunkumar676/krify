"""Before "done": the page mustn't still be showing a status or error message."""


def check_ui_state(state):
    """UI check before 'done': reject once if the page is showing a status/error message
    (see content.js's [Status] line), so the model can't declare success while the page is
    still complaining. Returns a rejection message, or None if the answer may go out."""
    if not state.page_status or state.ui_check_failed:
        return None
    state.ui_check_failed = True
    return (
        f"REJECTED – not sent to the user. The page is showing: \"{state.page_status}\". Make sure this "
        "isn't an error before calling done – read it, fix the problem, or call ask_user."
    )
