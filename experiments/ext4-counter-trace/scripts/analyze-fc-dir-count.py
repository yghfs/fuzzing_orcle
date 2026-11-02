#!/usr/bin/env python3
"""Confirm the fast-commit replay directory-count candidate from a special run."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import re
import sys

from trace_lifecycle import parse_trace

ANALYZER_PATH = pathlib.Path(__file__).with_name("analyze-special.py")
ANALYZER_SPEC = importlib.util.spec_from_file_location("analyze_special", ANALYZER_PATH)
ANALYZER = importlib.util.module_from_spec(ANALYZER_SPEC)
assert ANALYZER_SPEC and ANALYZER_SPEC.loader
ANALYZER_SPEC.loader.exec_module(ANALYZER)
REQUIRED = ANALYZER.REQUIRED
parse = ANALYZER.parse


def main(run: pathlib.Path) -> dict:
    events, stages = parse(run)
    issues = []
    expected_runtime_mismatches = {
        ("replay", "R-UG", 0, 3, 4),
        ("replay", "R-USUM.NS", 0xFFFFFFFF, 3, 4),
        ("replay", "R-LINK", 12, 2, 3),
    }
    observed_runtime_mismatches = set()
    for event in events:
        if not event.get("comparison_valid") or event.get("status") != 2:
            continue
        trace_file = event.get("trace_file", "")
        stage_name = pathlib.Path(trace_file).parent.name
        if event["point"] == "SNAPSHOT.MEMORY.STABLE":
            observed_runtime_mismatches.add((stage_name, event["formula"],
                                             event["group"] if event["formula"] != "R-LINK" else event["ino"],
                                             event["actual"], event["expected"]))
    if observed_runtime_mismatches != expected_runtime_mismatches:
        issues.append("unexpected_or_missing_stable_runtime_mismatches")
    validated_stages = {}
    for name, stage in stages.items():
        stage_valid = True
        for issue in stage["issues"]:
            if issue.startswith("valid_comparison_mismatch:"):
                seq = int(issue.rsplit(":", 1)[1])
                event = next((event for event in events
                              if event.get("trace_file", "").endswith(
                                  f"/stages/{name}/trace.txt")
                              and event["seq"] == seq), None)
                if event and event["point"] == "SNAPSHOT.MEMORY.STABLE":
                    continue
            issues.append(f"{name}:{issue}")
            stage_valid = False
        validated_stages[name] = stage_valid

    points = {}
    for event in events:
        points.setdefault(event["point"], []).append(event)
    for point in REQUIRED:
        matched = points.get(point, [])
        if not matched or any(event.get("status") != 1 for event in matched):
            issues.append(f"missing_or_unproven_required_point:{point}")

    disk = json.loads((run / "fc-disk.json").read_text())
    disk_evidence = json.loads((run / "fc-disk-evidence.json").read_text())
    disk_image = run / "fc.raw"
    for name, expected in disk_evidence.get("sha256", {}).items():
        actual = hashlib.sha256((run / name).read_bytes()).hexdigest()
        if actual != expected:
            issues.append(f"evidence_hash_mismatch:{name}")

    mismatches = [group for group in disk["groups"]
                  if group["descriptor_dirs"] != group["inode_dirs"]]
    if disk.get("r_ug", {}).get("valid") is not False or len(mismatches) != 1:
        issues.append("expected_single_R_UG_group_mismatch_missing")
    if disk_evidence.get("disk_checker_exit") == 0:
        issues.append("offline_checker_did_not_report_mismatch")
    if disk_evidence.get("fsck_exit") == 0:
        issues.append("e2fsck_did_not_report_mismatch")

    fsck = (run / "fc-fsck.txt").read_text(errors="replace")
    if "Directories count wrong for group #" not in fsck:
        issues.append("e2fsck_directory_count_diagnostic_missing")
    if "fast_commit_verify=ok" not in (
            run / "ext4-counter-trace/fc-verify.txt").read_text():
        issues.append("post_replay_namespace_verification_missing")

    replay_trace = run / "ext4-counter-trace/stages/replay/trace.txt"
    trace_text = replay_trace.read_text(errors="replace")
    replay_events, _ = parse_trace(replay_trace)
    dir_claims = [event for event in replay_events
                  if event["point"] == "FAST_COMMIT.INODE.REPLAY.BITMAP"
                  and event["reason"] == "replay_inode_claimed"
                  and event["update_delta"] == -1]
    initialized_dirs = [event for event in replay_events
                        if event["point"] == "NAMESPACE.NLINK.INIT_DIR"
                        and event["is_dir"] == 1]
    replayed_dir_inodes = set()
    for claim in dir_claims:
        if any(event["ino"] == claim["ino"] for event in initialized_dirs):
            replayed_dir_inodes.add(claim["ino"])
    if len(replayed_dir_inodes) != 1:
        issues.append("replayed_directory_inode_not_proven")
    else:
        ino = next(iter(replayed_dir_inodes))
        if not re.search(rf"ext4_fc_replay:.*tag 3, ino {ino},", trace_text):
            issues.append("fast_commit_CREAT_tag_for_directory_missing")

    report = {
        "finding_reproduced": not issues,
        "issues": issues,
        "formula": "R-UG",
        "trigger": "mkdir + file fsync + hard power cut + fast-commit replay",
        "replayed_directory_inodes": sorted(replayed_dir_inodes),
        "group_mismatches": mismatches,
        "offline_r_ug": disk.get("r_ug"),
        "fsck_exit": disk_evidence.get("fsck_exit"),
        "disk_checker_exit": disk_evidence.get("disk_checker_exit"),
        "special_stages": validated_stages,
        "disk_image_sha256": disk_evidence.get("sha256", {}).get("fc.raw"),
        "disk_image": str(disk_image),
    }
    return report


if __name__ == "__main__":
    result = main(pathlib.Path(sys.argv[1]))
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["finding_reproduced"] else 1)
