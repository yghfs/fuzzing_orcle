#!/usr/bin/env python3
"""Verify the three independent ext4 audit replays and their disk evidence.

The QEMU runs are retained under work/ext4-independent-audit.  This verifier
only reads serial logs and disk images; e2fsck is invoked with -n and therefore
does not modify an image.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    name: str
    fault_run: str
    control_run: str
    fault_result: str
    control_result: str
    fault_model: str
    control_model: str
    fsck_fault: str


CASES = (
    Case(
        "linkat inode nlink",
        "linkat-eio-precise",
        "linkat-control",
        r"LINK_RESULT ret=-1 errno=5",
        r"LINK_RESULT ret=0 errno=0",
        r"model=R-LINK source=linkat_inode_dirty expected=2 actual=1 refs_live=2",
        r"model=R-LINK source=linkat_inode_dirty expected=2 actual=2 refs_live=2",
        r"Inode 93 ref count is 1, should be 2\.",
    ),
    Case(
        "punch-hole extent ownership",
        "punch-eio-verified3",
        "punch-control-seeded",
        r"PUNCH_RESULT ret=-1 errno=5",
        r"PUNCH_RESULT ret=0 errno=0",
        r"model=EXTENT-OWNERSHIP source=punch_inode_dirty expected_free_reachable=0 actual=128",
        r"model=EXTENT-OWNERSHIP source=punch_inode_dirty expected_free_reachable=0 actual=0",
        r"Block bitmap differences:  \+\(2306--2433\)",
    ),
    Case(
        "fragmented collapse-range i_blocks",
        "collapse-fragmented-eio",
        "collapse-fragmented-control",
        r"COLLAPSE_RESULT ret=-1 errno=5",
        r"COLLAPSE_RESULT ret=0 errno=0",
        r"model=R-EXTENT source=collapse_extent_update expected_sectors=776 actual_sectors=1032",
        r"model=R-EXTENT source=collapse_extent_update expected_sectors=776 actual_sectors=776",
        r"Inode 94, i_blocks is 1032, should be 776\.",
    ),
)


def read(path: pathlib.Path) -> str:
    return path.read_text(errors="replace")


def require(text: str, pattern: str, label: str) -> None:
    if not re.search(pattern, text):
        raise AssertionError(f"missing {label}: /{pattern}/")


def fsck(image: pathlib.Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["e2fsck", "-fn", str(image)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return proc.returncode, proc.stdout


def verify_case(work: pathlib.Path, case: Case) -> dict[str, object]:
    fault = work / "runs" / case.fault_run
    control = work / "runs" / case.control_run
    fault_log = read(fault / "serial.log")
    control_log = read(control / "serial.log")

    require(fault_log, case.fault_result, "fault syscall result")
    require(control_log, case.control_result, "control syscall result")
    require(fault_log, case.fault_model, "fault checker model")
    require(control_log, case.control_model, "control checker model")
    require(fault_log, r"I/O error, dev vda, sector 368", "guest-visible sector EIO")
    require(fault_log, r"RUNTIME_AUDIT_DONE", "fault normal unmount")
    require(control_log, r"RUNTIME_AUDIT_DONE", "control normal unmount")

    fault_rc, fault_fsck = fsck(fault / "disk0.img")
    control_rc, control_fsck = fsck(control / "disk0.img")
    if fault_rc != 4:
        raise AssertionError(f"{case.name}: expected fault e2fsck rc=4, got {fault_rc}")
    if control_rc != 0:
        raise AssertionError(f"{case.name}: expected control e2fsck rc=0, got {control_rc}")
    require(fault_fsck, case.fsck_fault, "offline e2fsck finding")
    return {
        "case": case.name,
        "fault_run": str(fault),
        "control_run": str(control),
        "fault_e2fsck_rc": fault_rc,
        "control_e2fsck_rc": control_rc,
        "status": "PASS",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--work-dir",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parents[3]
        / "work/ext4-independent-audit",
    )
    args = parser.parse_args()
    work = args.work_dir.resolve()
    results = []
    try:
        for case in CASES:
            result = verify_case(work, case)
            results.append(result)
            print(f"PASS {case.name}: fault e2fsck=4, control e2fsck=0")
    except (AssertionError, FileNotFoundError, subprocess.SubprocessError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print(f"VERIFIED {len(results)}/{len(CASES)} independent ext4 cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
