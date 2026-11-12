"""The [Your task] / [Your plan] / [Your notes] lines sent to the AI with every page view."""


def header(state):
    """'[Your task] … [Your plan] … [Your notes] …' – sent with every page view."""
    lines = [f"[Your task] {state.task_text}"]
    if state.goal:
        lines.append(f"[Goal] {state.goal}")
    if state.plan:
        lines.append("[Your plan] " + " | ".join(f"{i}. {s}" for i, s in enumerate(state.plan, 1)))
    if state.notes:
        lines.append("[Your notes] " + " | ".join(state.notes))
    if state.done_when:
        lines.append(f"[Done when] {state.done_when}")
    return "\n".join(lines)
