"""Look names up wherever they live, so the same check scripts run before and after the
layer-folder refactor (old paths: app/agent/*.py, app/sheets.py; new: app/<layer>/*.py)."""

import importlib

# New locations first; the old ones are the fallback while (or before) code moves.
MODULES = [
    "app.output.text_reply", "app.output.answer",
    "app.verification.facts", "app.verification.claims", "app.verification.filters",
    "app.verification.ui_state", "app.verification.changes",
    "app.guardrails.safety", "app.guardrails.element_match", "app.guardrails.forms",
    "app.guardrails.permission", "app.guardrails.progress",
    "app.memory.transcript",
    "app.context.view", "app.context.element_list", "app.context.sites", "app.context.header",
    "app.context.pageview", "app.context.prompt", "app.context.understand",
    "app.tools.ops", "app.tools.describe", "app.tools.sheets", "app.tools.schemas", "app.tools.actions",
    "app.agent.loop", "app.agent.safety", "app.agent.changes", "app.agent.pageview", "app.agent.prompt",
    "app.agent.tools", "app.agent.understand", "app.sheets",
]


def _load(path):
    try:
        return importlib.import_module(path)
    except ModuleNotFoundError as err:
        if err.name and path.startswith(err.name):
            return None  # the module isn't there (yet / any more)
        raise


def mod(*paths):
    """The first of these modules that exists."""
    for path in paths:
        if (m := _load(path)) is not None:
            return m
    raise LookupError(f"none of {paths} exists")


def find(name):
    """`name` from whichever module defines it now."""
    for path in MODULES:
        m = _load(path)
        if m is not None and name in vars(m):
            return vars(m)[name]
    raise LookupError(name)
