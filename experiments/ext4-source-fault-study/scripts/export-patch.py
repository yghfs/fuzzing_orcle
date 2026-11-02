#!/usr/bin/env python3
"""Export only fs/ext4 source differences as an upstream-applicable patch."""

import argparse
import difflib
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("clean_source", type=Path)
    parser.add_argument("fault_source", type=Path)
    parser.add_argument("patch", type=Path)
    args = parser.parse_args()

    clean_ext4 = args.clean_source / "fs/ext4"
    fault_ext4 = args.fault_source / "fs/ext4"
    files = sorted(path.relative_to(args.fault_source)
                   for path in fault_ext4.rglob("*") if path.is_file())
    args.patch.parent.mkdir(parents=True, exist_ok=True)
    with args.patch.open("w") as output:
        for relative in files:
            clean = args.clean_source / relative
            fault = args.fault_source / relative
            old = clean.read_text(errors="surrogateescape").splitlines(keepends=True) \
                if clean.exists() else []
            new = fault.read_text(errors="surrogateescape").splitlines(keepends=True)
            if old == new:
                continue
            output.writelines(difflib.unified_diff(
                old, new, fromfile=f"a/{relative.as_posix()}",
                tofile=f"b/{relative.as_posix()}"))


if __name__ == "__main__":
    main()
