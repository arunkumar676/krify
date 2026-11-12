"""Run the refactor safety checks (G1 static, G2 golden text, G3 characterization, G4 replay) and compare
their output with checks/golden/. No LLM, no browser, no network, no real database.

  uv run python checks/run.py            # compare (exit 1 on any difference)
  uv run python checks/run.py --record   # (re)record the golden files – only from known-good code
"""

import difflib
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
BACKEND = HERE.parent
JOBS = [  # (name, module, extra env)
    ("golden_sv1", "checks.golden", {"STRUCTURED_VIEW": "1"}),
    ("golden_sv0", "checks.golden", {"STRUCTURED_VIEW": "0"}),
    ("characterize", "checks.characterize", {"STRUCTURED_VIEW": "1"}),
    ("replay_sv1", "checks.replay", {"STRUCTURED_VIEW": "1"}),
    ("replay_sv0", "checks.replay", {"STRUCTURED_VIEW": "0"}),
]


def main():
    record = "--record" in sys.argv
    static = subprocess.run([sys.executable, "-m", "checks.static"], cwd=BACKEND, capture_output=True, text=True)
    print(static.stdout.rstrip())
    (HERE / "out").mkdir(exist_ok=True)
    (HERE / "golden").mkdir(exist_ok=True)
    failed = False
    for name, module, extra in JOBS:
        env = {**os.environ, "PYTHONHASHSEED": "0", "DATABASE_URL": "sqlite://", "MAX_STEPS": "25", **extra}
        env.pop("REPLAY_VERBOSE", None)
        proc = subprocess.run([sys.executable, "-m", module], cwd=BACKEND, env=env, capture_output=True, text=True)
        out = proc.stdout
        (HERE / "out" / f"{name}.txt").write_text(out, encoding="utf-8")
        if proc.returncode != 0:
            print(f"✗ {name}: exit {proc.returncode}\n{proc.stderr[-3000:]}")
            failed = True
            continue
        golden = HERE / "golden" / f"{name}.txt"
        if record:
            golden.write_text(out, encoding="utf-8")
            print(f"● {name}: recorded ({len(out.splitlines())} lines)")
            continue
        expected = golden.read_text(encoding="utf-8") if golden.exists() else ""
        if out == expected:
            print(f"✓ {name}: identical ({len(out.splitlines())} lines)")
            continue
        failed = True
        diff = list(difflib.unified_diff(expected.splitlines(), out.splitlines(), "golden", "now", lineterm="", n=1))
        print(f"✗ {name}: {sum(1 for d in diff if d[:1] in '+-') - 2} changed lines")
        for line in diff[:40]:
            print("   " + (line if len(line) < 400 else line[:400] + " …"))
    sys.exit(1 if failed or static.returncode else 0)


if __name__ == "__main__":
    main()
