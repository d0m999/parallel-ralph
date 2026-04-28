"""Test fixtures for parallel-ralph harness.

Adds .ralph/scripts/ and scripts_4x/ to sys.path so individual test modules
can import acceptance, run_batch, render_shards, etc. directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / ".ralph" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts_4x"))
