#!/usr/bin/env python3
"""Validate a deterministic single-worker fault evidence directory.

The implementation lives in :mod:`fault_evidence` so Python tests can import
it; this hyphenated entry point is the stable command-line interface.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fault_evidence import main, validate  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
