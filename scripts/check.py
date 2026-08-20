"""Quality gate for both halves of the repository: Python and TypeScript.

Runs the steps in order, stops at the first failure and propagates its exit
code. Uses only the standard library so it runs the same on Windows, Linux
and in CI, and it is the single command CI invokes -- there is no second list
of commands in the workflow to drift from this one.

    uv run python scripts/check.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRADER = ROOT / "trader"

Step = tuple[str, tuple[str, ...], Path]

PYTHON_STEPS: tuple[Step, ...] = (
    ("format", ("ruff", "format", "--check", "."), ROOT),
    ("lint", ("ruff", "check", "."), ROOT),
    ("types", ("mypy", "src", "tests"), ROOT),
    ("test", ("pytest",), ROOT),
)

# npm.cmd on Windows: subprocess without a shell won't resolve the .cmd
# shim that `npm` actually is there.
NPM = "npm.cmd" if sys.platform == "win32" else "npm"

TRADER_STEPS: tuple[Step, ...] = (
    ("trader types", (NPM, "run", "--silent", "build"), TRADER),
    ("trader test", (NPM, "run", "--silent", "test"), TRADER),
)


def main() -> int:
    if missing := sorted({cmd[0] for _, cmd, _ in PYTHON_STEPS if not on_path(cmd[0])}):
        print(
            f"error: {', '.join(missing)} not found on PATH.\n"
            "Run 'uv sync' and call this script with "
            "'uv run python scripts/check.py'.",
            file=sys.stderr,
        )
        return 127

    steps = list(PYTHON_STEPS)
    if not on_path(NPM):
        print(
            "error: npm not found on PATH. The trader is part of this "
            "repository's definition of done; install Node 20+ or run only "
            "the Python steps with 'uv run pytest'.",
            file=sys.stderr,
        )
        return 127
    if not (TRADER / "node_modules").is_dir():
        print(
            "error: trader/node_modules is missing. Run 'npm ci' in trader/.",
            file=sys.stderr,
        )
        return 127
    steps.extend(TRADER_STEPS)

    for name, cmd, cwd in steps:
        print(f"==> {name}: {' '.join(cmd)}", flush=True)
        code = subprocess.run(cmd, cwd=cwd).returncode
        if code != 0:
            print(f"error: step '{name}' failed with code {code}", file=sys.stderr)
            return code

    print("==> ok: format, lint, types and tests passed on both halves")
    return 0


def on_path(command: str) -> bool:
    return shutil.which(command) is not None


if __name__ == "__main__":
    raise SystemExit(main())
