#!/usr/bin/env python3
"""Validate a matched no-injection control for a formula fault fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
from typing import Any


KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")
PREFIX = re.compile(r"(?P<pid>\d+)\s+.*?:\s*(?P<body>.*)")


def load(path: pathlib.Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def integer(value: Any, default: int | None = None) -> int | None:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def trace_rows(path: pathlib.Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line_number, line in enumerate(lines, 1):
        match = PREFIX.search(line.strip())
        if match:
            rows.append({"line": line_number, "pid": int(match["pid"]),
                         "body": match["body"],
                         "fields": dict(KEY.findall(match["body"]))})
    return rows


def clean_fsck(text: str) -> bool:
    return bool(text.strip()) and not any(marker in text for marker in (
        "FILE SYSTEM WAS MODIFIED", "still has errors", "Fix? no",
        "UNEXPECTED INCONSISTENCY", "UNRECOVERED ERRORS"))


def disk_verified(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("valid") is not True or value.get("status") != "verified":
        return False
    if value.get("bitmap_descriptor_match") is not True or \
            value.get("superblock_descriptor_match") is not True:
        return False
    return value.get("unverified") in ([], None)


def image_integrity(directory: pathlib.Path, issues: list[str]) -> dict[str, Any] | None:
    metadata = load(directory / "image-integrity.json")
    if not isinstance(metadata, dict):
        issues.append("image_integrity:missing")
        return None
    primary = metadata.get("primary_image")
    recovery = metadata.get("recovery_image")
    if not isinstance(primary, dict) or not isinstance(recovery, dict):
        issues.append("image_integrity:invalid")
        return metadata
    if metadata.get("repair_target") != "recovery.raw":
        issues.append("image_integrity:repair_target_invalid")
    parent = directory.parent.resolve()
    for label, item in (("primary", primary), ("recovery", recovery)):
        relative = item.get("path")
        expected = item.get("sha256_after_checks")
        if not isinstance(relative, str) or not isinstance(expected, str):
            issues.append(f"image_integrity:{label}_hash_missing")
            continue
        image = (directory / relative).resolve()
        try:
            image.relative_to(parent)
            with image.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
        except (OSError, ValueError):
            issues.append(f"image_integrity:{label}_unreadable")
            continue
        if actual != expected:
            issues.append(f"image_integrity:{label}_hash_changed")
    if primary.get("sha256_after_guest") != primary.get("sha256_after_checks"):
        issues.append("image_integrity:primary_changed")
    if primary.get("sha256_after_guest") != recovery.get("sha256_before_repair") or \
            recovery.get("sha256_before_repair") != recovery.get("sha256_after_checks"):
        issues.append("image_integrity:control_copy_changed")
    if metadata.get("repair_performed") is True:
        issues.append("image_integrity:control_repair_performed")
    return metadata


def validate(directory: pathlib.Path) -> dict[str, Any]:
    directory = pathlib.Path(directory)
    issues: list[str] = []
    plan = load(directory / "fault-plan.json", {}) or {}
    workload = load(directory / "workload-result.json", {}) or {}
    run_meta = load(directory / "run-meta.json", {}) or {}
    if plan.get("schema") != 2 or plan.get("control_mode") is not True:
        issues.append("plan:control_mode_missing")
    pid = integer(workload.get("pid"))
    if pid is None:
        issues.append("workload:pid_missing")
    rows = trace_rows(directory / "trace.txt")
    target_rows = [row for row in rows if pid is not None and row["pid"] == pid]
    fault_hits = [row for row in target_rows if "ext4_fault_inject:" in row["body"]]
    if fault_hits:
        issues.append("fault:unexpected_hit")
    arm_end = [row for row in target_rows if "CT_FAULT_ARM" in row["body"] and
               row["fields"].get("phase") == "ARM_END"]
    if len(arm_end) != 1 or arm_end[0]["fields"].get("status") != "disabled":
        issues.append("fault:control_arm_marker_invalid")
    begins = [row for row in target_rows if "CT_SYSCALL" in row["body"] and
              row["fields"].get("phase") == "BEGIN"]
    ends = [row for row in target_rows if "CT_SYSCALL" in row["body"] and
            row["fields"].get("phase") == "END"]
    if len(begins) != 1 or len(ends) != 1:
        issues.append("syscall:marker_count_mismatch")
    else:
        begin, end = begins[0], ends[0]
        if begin["fields"].get("program") != plan.get("program") or \
                integer(begin["fields"].get("call_index")) != plan.get("call_index"):
            issues.append("syscall:selector_mismatch")
        if integer(begin["fields"].get("nr")) != integer(plan.get("syscall_nr")):
            issues.append("syscall:number_mismatch")
        enters = [row for row in target_rows if begin["line"] < row["line"] < end["line"]
                  and "sys_enter:" in row["body"] and re.search(
                      r"\bNR\s+" + str(plan.get("syscall_nr")) + r"\b", row["body"])]
        exits = [row for row in target_rows if begin["line"] < row["line"] < end["line"]
                 and "sys_exit:" in row["body"] and re.search(
                     r"\bNR\s+" + str(plan.get("syscall_nr")) + r"\s*=", row["body"])]
        if len(enters) != 1 or len(exits) != 1:
            issues.append("syscall:native_window_missing")
        else:
            raw_match = re.search(r"\bsys_exit:\s*NR\s+\d+\s*=\s*(-?\d+)",
                                  exits[0]["body"])
            raw_return = integer(raw_match.group(1)) if raw_match else None
            if raw_return is None:
                issues.append("syscall:raw_return_missing")
            if end["fields"].get("ret") != str(workload.get("syscall_result")):
                issues.append("syscall:marker_return_mismatch")
            if raw_return is not None and raw_return != workload.get("syscall_result"):
                issues.append("syscall:raw_return_mismatch")
    if workload.get("call_performed") is not True:
        issues.append("workload:call_not_performed")
    if workload.get("arm_verified") is not False:
        issues.append("workload:control_arm_state_invalid")
    for name in ("stable-before.json", "stable-after.json", "stable-oracle.json",
                 "remount.json"):
        snapshot = load(directory / name, {}) or {}
        if snapshot.get("valid") is not True or snapshot.get("relations_valid") is not True or \
                snapshot.get("namespace_valid") is not True or \
                snapshot.get("observation_stable") is not True or \
                snapshot.get("trace_loss", 0) != 0 or snapshot.get("workers") != 1:
            issues.append("stable:" + name + ":invalid")
    run_status = {}
    try:
        run_status = dict(re.findall(r"(?m)^([A-Za-z_]+)=([^\s]+)$",
                                     (directory / "run-status.txt").read_text()))
    except OSError:
        issues.append("run_status:missing")
    if run_status.get("workload_rc") != "0" or run_status.get("trace_loss") != "0" or \
            run_status.get("workers") != "1":
        issues.append("run_status:control_not_clean")
    if (run_meta.get("guest_exit") != 0 or run_meta.get("fsck_exit") != 0 or
            run_meta.get("disk_exit") != 0):
        issues.append("run_meta:exit_failure")
    try:
        fsck = (directory / "fsck.txt").read_text(errors="replace")
        before_fsck = (directory / "fsck-before-recovery.txt").read_text(errors="replace")
    except OSError:
        fsck = before_fsck = ""
        issues.append("persistence:fsck_missing")
    if not clean_fsck(fsck) or not clean_fsck(before_fsck):
        issues.append("persistence:fsck_not_clean")
    if not disk_verified(load(directory / "disk.json", {})) or \
            not disk_verified(load(directory / "disk-before-recovery.json", {})):
        issues.append("persistence:disk_not_verified")
    integrity = image_integrity(directory, issues)
    association = load(directory / "association-index.json")
    if not isinstance(association, dict) or association.get("schema") != 1:
        issues.append("association:missing_or_invalid_index")
    elif association.get("fault_hits"):
        issues.append("association:unexpected_fault_hits")
    classification = "matched_control" if not issues else "control_invalid"
    return {
        "schema": 1, "kind": "matched_control", "classification": classification,
        "passed": not issues, "fault_hit_count": len(fault_hits),
        "syscall_result": workload.get("syscall_result"),
        "syscall_errno": workload.get("syscall_errno"),
        "image_integrity": integrity, "association_index": association,
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    report = validate(args.directory)
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
