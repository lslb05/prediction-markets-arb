"""Formata e aplica correcoes automaticas de lint.

uv run python scripts/format.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("formato", ("ruff", "format", ".")),
    ("lint --fix", ("ruff", "check", "--fix", ".")),
)


def main() -> int:
    if shutil.which("ruff") is None:
        print(
            "erro: ruff nao encontrado no PATH.\n"
            "Rode 'uv sync' e chame este script com 'uv run python scripts/format.py'.",
            file=sys.stderr,
        )
        return 127

    for name, cmd in STEPS:
        print(f"==> {name}: {' '.join(cmd)}", flush=True)
        code = subprocess.run(cmd, cwd=ROOT).returncode
        if code != 0:
            print(f"erro: etapa '{name}' falhou com codigo {code}", file=sys.stderr)
            return code

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
