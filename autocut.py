#!/usr/bin/env python3
"""Run autocut from a checkout without installing: python autocut.py --script ... (see --help)."""

import sys
from pathlib import Path

# The package lives in src/autocutlib (the CLI is the same as the installed `autocut` command).
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from autocutlib.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
