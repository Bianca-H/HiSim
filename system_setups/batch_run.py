"""Convenience wrapper to run the repo-root batch runner from `system_setups/`.

This allows:
  python batch_run.py ...
when your current working directory is `system_setups`.
"""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parents[1]
    target = repo_root / "batch_run.py"
    runpy.run_path(str(target), run_name="__main__")

