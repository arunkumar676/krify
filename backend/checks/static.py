"""G1: static checks for the layer layout – everything compiles, ruff finds no undefined/unused
names, every module imports on its own in a fresh interpreter (catches import cycles that only
show up in some import orders), the layer packages' __init__.py files stay empty, and no layer
imports the agent loop."""

import ast
import dataclasses
import os
import pathlib
import subprocess
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
ENV = {**os.environ, "DATABASE_URL": "sqlite://"}
LAYERS = {"context", "tools", "guardrails", "verification", "memory", "output"}


def run(*cmd):
    return subprocess.run(cmd, cwd=BACKEND, env=ENV, capture_output=True, text=True)


def main():
    problems = []
    if (r := run(sys.executable, "-m", "compileall", "-q", "app", "checks")).returncode:
        problems.append(f"compileall:\n{r.stdout}{r.stderr}")
    r = run("uvx", "ruff", "check", "--select", "F", "--output-format", "concise", "app", "try_nlu.py", "eval_tasks.py")
    if r.returncode:
        problems.append(f"ruff:\n{r.stdout}{r.stderr}")
    modules = sorted(".".join(p.relative_to(BACKEND).with_suffix("").parts) for p in (BACKEND / "app").rglob("*.py")
                     if "__pycache__" not in p.parts)
    for module in modules:
        name = module.removesuffix(".__init__")
        if (r := run(sys.executable, "-c", f"import {name}")).returncode:
            problems.append(f"import {name}:\n{r.stderr[-1500:]}")
    for init in (BACKEND / "app").rglob("__init__.py"):
        if init.read_text().strip():
            problems.append(f"{init.relative_to(BACKEND)} isn't empty (re-exports can create import cycles)")
    for path in (BACKEND / "app").rglob("*.py"):
        layer = path.relative_to(BACKEND / "app").parts[0]
        if layer in LAYERS and "app.agent.loop" in path.read_text():
            problems.append(f"{path.relative_to(BACKEND)} imports app.agent.loop (layers must not)")
    problems += field_whitelist()
    for p in problems:
        print("✗ " + p)
    print(f"{'✗' if problems else '✓'} static: {len(modules)} modules checked, {len(problems)} problem(s)")
    sys.exit(1 if problems else 0)


def field_whitelist():
    """G5: every self.X used by Agent and its layer mixins is an Agent attribute (set in __init__), a
    method or `state`; every state.X / self.state.X is a TaskState field; no method name is defined by
    two of Agent's classes (method resolution order would silently pick one)."""
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("DATABASE_URL", "sqlite://")
    from app.agent.loop import Agent
    from app.agent.state import TaskState

    fields = {f.name for f in dataclasses.fields(TaskState)} | {"start"}
    classes = [c for c in Agent.__mro__ if c is not object]
    owners = {}
    problems = []
    for cls in classes:
        for name, value in vars(cls).items():
            if callable(value) and not name.startswith("__"):
                if name in owners:
                    problems.append(f"method {name} is defined by both {owners[name]} and {cls.__name__}")
                owners[name] = cls.__name__
    init = ast.parse(pathlib.Path(sys.modules[Agent.__module__].__file__).read_text())
    attrs = {n.attr for n in ast.walk(init) if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
             and isinstance(n.value, ast.Name) and n.value.id == "self"}
    allowed = attrs | set(owners) | {"state"}
    files = {pathlib.Path(sys.modules[c.__module__].__file__) for c in classes}
    files |= {p for p in (BACKEND / "app").rglob("*.py") if p.parts[-2] in LAYERS}
    for path in sorted(files):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, (ast.Name, ast.Attribute)):
                continue
            base = node.value
            where = f"{path.relative_to(BACKEND)}:{node.lineno}"
            if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) and base.value.id == "self" \
                    and base.attr == "state" and node.attr not in fields:
                problems.append(f"{where}: self.state.{node.attr} isn't a TaskState field")
            elif isinstance(base, ast.Name) and base.id == "state" and node.attr not in fields:
                problems.append(f"{where}: state.{node.attr} isn't a TaskState field")
            elif isinstance(base, ast.Name) and base.id == "self" and path.parts[-2] != "agent" or \
                    path.name == "loop.py" and isinstance(base, ast.Name) and base.id == "self":
                if node.attr not in allowed and not (path.name == "state.py"):
                    problems.append(f"{where}: self.{node.attr} is neither an Agent attribute nor a method")
    return problems


if __name__ == "__main__":
    main()
