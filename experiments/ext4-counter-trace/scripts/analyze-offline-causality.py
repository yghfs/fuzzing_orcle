#!/usr/bin/env python3
"""Link an offline ext4 inconsistency to the first degraded segment.

The input campaign remains immutable.  This audit only reads manifests,
snapshots, traces, disk checker JSON and e2fsck output and writes a separate
causality report.  A link is strong only when the hook event, same-task
syscall, target inode and final offline evidence agree.
"""
from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import re
import sys
from typing import Any, Iterator

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from trace_lifecycle import correlate_syscalls  # noqa: E402


KINDS = ("regular", "bigalloc", "inline")
NUMERIC_FIELDS = {
    "block", "call_index", "dir_ino", "errno", "errno_choice_draw", "generation",
    "group", "ino", "injection", "len", "match_index", "nlink_after",
    "nlink_before", "op", "parent", "pid", "probability_ppm", "random_check",
    "random_draw", "random_seed", "ref_delta", "result", "seq", "slot", "status",
    "target_pid", "tid", "update_delta", "version",
}
TRACE_PREFIX = re.compile(
    r"(?P<comm>.+)-(?P<pid>\d+)\s+\[(?P<cpu>\d+)\]\s+\S+\s+"
    r"(?P<time>\d+\.\d+):\s*(?P<body>.*)"
)
KV = re.compile(r"(\w+)=([^\s]+)")


def load_json(path: pathlib.Path, default: Any = None) -> Any:
    try:
        with path.open() as stream:
            return json.load(stream)
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def parse_fields(text: str) -> dict[str, Any]:
    values: dict[str, Any] = dict(KV.findall(text))
    for key in NUMERIC_FIELDS:
        if key not in values:
            continue
        try:
            values[key] = int(values[key])
        except (TypeError, ValueError):
            pass
    return values


def trace_path(folder: pathlib.Path) -> pathlib.Path | None:
    for candidate in (
        folder / "analysis/trace.txt",
        folder / "analysis/trace.txt.gz",
        folder / "trace.txt",
        folder / "trace.txt.gz",
    ):
        if candidate.is_file():
            return candidate
    return None


def trace_rows(path: pathlib.Path) -> list[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    rows: list[dict[str, Any]] = []
    with opener(path, "rt", errors="replace") as stream:
        for number, raw in enumerate(stream, 1):
            match = TRACE_PREFIX.search(raw.strip())
            if not match:
                continue
            seconds, fraction = match["time"].split(".")
            row: dict[str, Any] = {
                "line": number,
                "pid": int(match["pid"]),
                "cpu": int(match["cpu"]),
                "comm": match["comm"].strip(),
                "timestamp_ns": int(seconds) * 10**9 + int(fraction.ljust(9, "0")),
                "body": match["body"],
            }
            if "ext4_counter_event:" in row["body"]:
                body = row["body"].split("ext4_counter_event:", 1)[1]
                row["kind"] = "counter"
                row.update(parse_fields(body))
            elif "ext4_fault_inject:" in row["body"]:
                body = row["body"].split("ext4_fault_inject:", 1)[1]
                row["kind"] = "fault"
                row.update(parse_fields(body))
            rows.append(row)
    return rows


def snapshot_health(snapshot: Any) -> bool:
    if not isinstance(snapshot, dict):
        return False
    return (
        snapshot.get("valid") is True
        and snapshot.get("namespace_valid") is True
        and snapshot.get("relations_valid") is True
        and not any(snapshot.get(key, 0) for key in
                    ("observed_mismatches", "namespace_mismatched", "relations_mismatches"))
    )


def snapshot_summary(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {"available": False}
    return {
        "available": True,
        "valid": snapshot.get("valid"),
        "namespace_valid": snapshot.get("namespace_valid"),
        "namespace_reason": snapshot.get("namespace_reason"),
        "relations_valid": snapshot.get("relations_valid"),
        "observed_mismatches": snapshot.get("observed_mismatches"),
        "namespace_mismatched": snapshot.get("namespace_mismatched"),
        "relations_mismatches": snapshot.get("relations_mismatches"),
        "free_clusters": snapshot.get("free_clusters"),
        "free_inodes": snapshot.get("free_inodes"),
        "dirs": snapshot.get("dirs"),
        "snapshot_id": snapshot.get("snapshot_id"),
    }


def manifests(run: pathlib.Path) -> Iterator[tuple[pathlib.Path, dict[str, Any]]]:
    paths = sorted((run / "segments").glob("segment-*/manifest.json"),
                   key=lambda p: int(p.parent.name.split("-")[-1]))
    for path in paths:
        value = load_json(path)
        if isinstance(value, dict):
            yield path, value


def first_anomaly(run: pathlib.Path, kind: str) -> dict[str, Any]:
    previous: dict[str, Any] | None = None
    previous_id: int | None = None
    for manifest_path, manifest in manifests(run):
        if manifest.get("kind") != kind or manifest.get("status") != "complete":
            continue
        segment_id = int(manifest.get("segment_id", -1))
        folder = manifest_path.parent
        before = load_json(folder / f"{kind}-before.json")
        after = load_json(folder / f"{kind}-after.json")
        if not isinstance(after, dict):
            continue
        before_ok = snapshot_health(before)
        after_ok = snapshot_health(after)
        if not after_ok and (previous is None or snapshot_health(previous) or before_ok):
            return {
                "segment_id": segment_id,
                "phase": "after",
                "previous_segment_id": previous_id,
                "before": snapshot_summary(before),
                "after": snapshot_summary(after),
                "transition": "healthy_to_degraded" if before_ok else "degraded_at_entry",
                "evidence": f"segments/segment-{segment_id:06d}/{kind}-after.json",
            }
        previous, previous_id = after, segment_id
    return {"status": "not_observed"}


def fsck_inodes(text: str) -> dict[str, list[int]]:
    deleted = sorted({int(value) for value in re.findall(
        r"(?:deleted inode|已删除\s*inode)\s+(\d+).*?(?:dtime|删除时间)",
        text, re.IGNORECASE)})
    sections = re.findall(
        r"(?:Inode bitmap differences|Inode 位图的差异)\s*[:：]\s*([^\n]+)",
        text, re.IGNORECASE)
    bitmap = sorted({int(value) for section in sections
                     for value in re.findall(r"-(\d+)", section)})
    return {"deleted_dtime_zero": deleted, "bitmap_clear_candidates": bitmap}


def disk_affected_inodes(disk: Any) -> list[int]:
    if not isinstance(disk, dict):
        return []
    result = []
    for check_name in ("r_link", "r_inode_lifecycle"):
        check = disk.get(check_name, {})
        if not isinstance(check, dict):
            continue
        for item in check.get("mismatches", []):
            try:
                result.append(int(item["ino"]))
            except (KeyError, TypeError, ValueError):
                pass
    return sorted(set(result))


def _event_rows(rows: list[dict[str, Any]], kind: str, dev: str, pid: int,
                line: int, operation: str) -> list[dict[str, Any]]:
    points = {
        "inode_evict_final_dirty": ("INODE.EVICT.TARGET.", "INODE.FREE."),
        "unlink_inode_dirty": ("NAMESPACE.REF.", "NAMESPACE.DIRENT.",
                                "NAMESPACE.NLINK.", "INODE.EVICT."),
        "mballoc_mark": ("BLOCK.",),
    }.get(operation, ())
    selected = []
    for row in rows:
        if row.get("kind") != "counter" or row.get("dev") != dev or row.get("pid") != pid:
            continue
        point = str(row.get("point", ""))
        if abs(int(row.get("line", 0)) - line) > 180:
            continue
        if points and not point.startswith(points):
            continue
        selected.append(row)
    return selected


def _compact_counter(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in (
        "line", "point", "formula", "ino", "dir_ino", "group", "block",
        "nlink_before", "nlink_after", "ref_delta", "actual", "expected", "status"
    )}


def _syscall_attribution(calls: list[dict[str, Any]], fault: dict[str, Any]) -> dict[str, Any]:
    """Describe why a fault was or was not assigned to a syscall envelope.

    A hook can run during cleanup after the generated syscall marker has
    closed.  Returning the nearest call in that case would create a false
    causal edge, so only an enclosing native/CT_SYSCALL pair is authoritative.
    """
    exact = [call for call in calls
             if call.get("pid") == fault.get("pid")
             and call.get("marker_begin_line", 0) < fault.get("line", 0)
             < call.get("marker_end_line", 0)]
    if len(exact) == 1:
        return {"method": "exact_ct_syscall_envelope", "candidates": [
            exact[0].get("id")], "ambiguous": False}
    if len(exact) > 1:
        return {"method": "ambiguous_ct_syscall_envelope", "candidates": [
            call.get("id") for call in exact], "ambiguous": True}
    return {"method": "no_ct_syscall_envelope", "candidates": [], "ambiguous": False}


def hook_context(run: pathlib.Path, manifest_path: pathlib.Path,
                 manifest: dict[str, Any], devices: dict[str, str],
                 final_inodes: list[int]) -> dict[str, Any]:
    fault = manifest.get("fault_injection") or {}
    status = fault.get("status") or {}
    if not fault.get("hit") or status.get("enabled") is False or status.get("operation") == "off":
        return {"status": "not_active"}
    folder = manifest_path.parent
    path = trace_path(folder)
    if path is None:
        return {"status": "trace_missing"}
    rows = trace_rows(path)
    dev = devices.get(manifest.get("kind"))
    operation = status.get("errno_operation")
    fault_rows = [row for row in rows if row.get("kind") == "fault" and
                  (dev is None or row.get("dev") == dev) and
                  row.get("operation") == operation and row.get("injection") == 1]
    if not fault_rows:
        return {"status": "fault_event_missing", "trace": path.name,
                "operation": operation}
    row = fault_rows[0]
    lines = [item for item in rows if "body" in item]
    try:
        calls, syscall_issues = correlate_syscalls(lines)
    except (KeyError, ValueError, TypeError) as error:
        calls, syscall_issues = [], [f"correlation_error:{error}"]
    call = next((item for item in calls if item.get("pid") == row.get("pid") and
                 item.get("marker_begin_line", 0) < row["line"] <
                 item.get("marker_end_line", 0)), None)
    syscall_attribution = _syscall_attribution(calls, row)
    counters = _event_rows(rows, manifest.get("kind", ""), dev or "", row["pid"],
                           row["line"], operation)
    targets = sorted({int(item["ino"]) for item in counters
                      if item.get("ino") not in (None, 0) and
                      str(item.get("point", "")).endswith("TARGET.BEFORE")})
    if not targets:
        targets = sorted({int(item["ino"]) for item in counters
                          if item.get("ino") not in (None, 0)})
    matched = sorted(set(targets) & set(final_inodes))
    syscall = None
    if call is not None:
        syscall = {key: call.get(key) for key in (
            "id", "pid", "name", "call_index", "program", "marker_begin_line",
            "marker_end_line", "user_result", "user_errno", "raw_result", "errno",
            "verified"
        )}
    if matched and syscall and syscall.get("verified"):
        strength = "strong"
    elif matched:
        strength = "partial"
    elif syscall and syscall.get("verified"):
        strength = "hook_syscall_only"
    else:
        strength = "hook_only_or_unattributed"
    return {
        "status": "matched",
        "strength": strength,
        "operation": operation,
        "errno": row.get("errno"),
        "pid": row.get("pid"),
        "target_pid": row.get("target_pid"),
        "generation": row.get("generation"),
        "block": row.get("block"),
        "trace": path.name,
        "fault_line": row.get("line"),
        "syscall": syscall,
        "syscall_attribution": syscall_attribution,
        "syscall_issues": syscall_issues,
        "counter_events": [_compact_counter(item) for item in counters],
        "target_inodes": targets,
        "matched_final_inodes": matched,
    }


def final_evidence(run: pathlib.Path, kind: str,
                   disk_dir: pathlib.Path | None = None) -> dict[str, Any]:
    disk_path = ((disk_dir / f"{kind}-disk.json")
                 if disk_dir is not None else run / f"{kind}-disk.json")
    disk = load_json(disk_path)
    fsck_path = run / f"{kind}-fsck.txt"
    fsck_text = fsck_path.read_text(errors="replace") if fsck_path.is_file() else ""
    meta = load_json(run / "campaign.json", {}) or {}
    disk_meta = (meta.get("disk", {}) or {}).get(kind, {})
    fsck = fsck_inodes(fsck_text)
    disk_inodes = disk_affected_inodes(disk)
    affected = sorted(set(disk_inodes) | set(fsck["deleted_dtime_zero"]) |
                      set(fsck["bitmap_clear_candidates"]))
    return {
        "disk": {
            "status": disk.get("status") if isinstance(disk, dict) else None,
            "valid": disk.get("valid") if isinstance(disk, dict) else None,
            "r_link_mismatches": (disk.get("r_link", {}).get("mismatches", [])
                                   if isinstance(disk, dict) else []),
            "r_inode_lifecycle_mismatches": (
                disk.get("r_inode_lifecycle", {}).get("mismatches", [])
                if isinstance(disk, dict) else []),
            "affected_inodes": disk_inodes,
            "evidence": f"{kind}-disk.json",
        },
        "fsck": {
            "exit": disk_meta.get("fsck_exit"),
            "deleted_dtime_zero": fsck["deleted_dtime_zero"],
            "bitmap_clear_candidates": fsck["bitmap_clear_candidates"],
            "evidence": f"{kind}-fsck.txt",
        },
        "affected_inodes": affected,
        "evidence_quality": "offline_disk_and_fsck" if disk_inodes and fsck["deleted_dtime_zero"] else
                           "fsck_only" if affected else "clean_or_no_evidence",
    }


PARAMETERS = (
    "mode", "workers", "scenario", "seed", "segment_seconds", "max_batches", "syz_calls",
    "resource_valid_every", "fresh_generate_every", "cpus", "memory_mb",
    "guest_memory_mb",
)

BUILD_ARTIFACTS = (
    "kernel.bzImage",
    "evidence/kernel.config",
    "evidence/0001-counter-trace-core-and-probes.patch",
    "evidence/manifest.json",
)


def _digest_map(meta: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    values = meta.get("build_sha256", {}) or {}
    return {key: values.get(key) for key in keys}


def _start_image_digests(meta: dict[str, Any]) -> dict[str, Any]:
    images = meta.get("start_images", {}) or {}
    return {kind: (value or {}).get("sha256") for kind, value in images.items()
            if isinstance(value, dict)}


def control_summary(run: pathlib.Path, control: pathlib.Path | None,
                    kind: str, target_limits: dict[str, Any],
                    target_meta: dict[str, Any] | None = None,
                    disk_dir: pathlib.Path | None = None) -> dict[str, Any]:
    if control is None:
        return {"status": "missing", "required": True,
                "interpretation": "control_missing"}
    target_meta = target_meta or {}
    meta = load_json(control / "campaign.json", {}) or {}
    limits = meta.get("limits", {}) or {}
    differences = {
        key: {"target": target_limits.get(key), "control": limits.get(key)}
        for key in PARAMETERS if target_limits.get(key) != limits.get(key)
    }
    disk_path = ((disk_dir / f"{kind}-disk.json")
                 if disk_dir is not None else control / f"{kind}-disk.json")
    disk = load_json(disk_path)
    final = load_json(control / "checkpoint" / f"{kind}-final.json")
    disk_meta = (meta.get("disk", {}) or {}).get(kind, {})
    run_complete = meta.get("status") == "complete"
    clean_shutdown = meta.get("clean_shutdown") is True
    fault_setting = limits.get("random_fault_injection")
    legacy_control_without_fault_setting = (
        fault_setting is None and not meta.get("fault_injection_hit") and
        not meta.get("fault_injection_candidates"))
    fault_injection_disabled = fault_setting is False or legacy_control_without_fault_setting
    target_build = _digest_map(target_meta, BUILD_ARTIFACTS)
    control_build = _digest_map(meta, BUILD_ARTIFACTS)
    build_differences = {
        key: {"target": target_build[key], "control": control_build[key]}
        for key in BUILD_ARTIFACTS if target_build[key] != control_build[key]
    }
    target_images = _start_image_digests(target_meta)
    control_images = _start_image_digests(meta)
    image_differences = {
        kind_name: {"target": target_images.get(kind_name),
                    "control": control_images.get(kind_name)}
        for kind_name in sorted(set(target_images) | set(control_images))
        if target_images.get(kind_name) != control_images.get(kind_name)
    }
    target_rootfs = (target_meta.get("dependencies", {}) or {}).get("rootfs_sha256")
    control_rootfs = (meta.get("dependencies", {}) or {}).get("rootfs_sha256")
    rootfs_match = target_rootfs == control_rootfs and target_rootfs is not None
    healthy = bool(
        run_complete and clean_shutdown and fault_injection_disabled and
        not build_differences and not image_differences and rootfs_match and
        isinstance(disk, dict) and disk.get("valid") is True and
        disk.get("status") == "verified" and disk_meta.get("fsck_exit") == 0 and
        snapshot_health(final)
    )
    blockers = []
    if not run_complete:
        blockers.append("campaign_not_complete")
    if not clean_shutdown:
        blockers.append("clean_shutdown_not_confirmed")
    if not fault_injection_disabled:
        blockers.append("fault_injection_enabled")
    if differences:
        blockers.append("workload_parameters_differ")
    if build_differences:
        blockers.append("kernel_or_probe_binding_differs")
    if image_differences:
        blockers.append("start_image_differs")
    if not rootfs_match:
        blockers.append("rootfs_differs_or_unbound")
    if not isinstance(disk, dict) or disk.get("valid") is not True or disk.get("status") != "verified":
        blockers.append("disk_not_clean")
    if disk_meta.get("fsck_exit") != 0 or not snapshot_health(final):
        blockers.append("offline_or_snapshot_not_clean")
    return {
        "status": "clean" if healthy else "degraded",
        "run_status": meta.get("status"),
        "clean_shutdown": clean_shutdown,
        "fault_injection_enabled": bool(limits.get("random_fault_injection")),
        "fault_injection_disabled": fault_injection_disabled,
        "fault_injection_evidence": "explicit_disabled" if fault_setting is False else
                                    "legacy_absent_without_hits" if legacy_control_without_fault_setting else
                                    "enabled_or_unknown",
        "parameters_match": not differences,
        "parameter_differences": differences,
        "build_match": not build_differences,
        "build_differences": build_differences,
        "start_images_match": not image_differences,
        "start_image_differences": image_differences,
        "rootfs_match": rootfs_match,
        "fsck_exit": disk_meta.get("fsck_exit"),
        "disk_valid": disk.get("valid") if isinstance(disk, dict) else None,
        "disk_status": disk.get("status") if isinstance(disk, dict) else None,
        "final_snapshot": snapshot_summary(final),
        "run": str(control),
        "blockers": blockers,
        "interpretation": "matched_control" if healthy and not differences else
                          "control_run_failed" if not run_complete or not clean_shutdown else
                          "control_observed_but_not_matched",
    }


def audit(run: pathlib.Path, control: pathlib.Path | None = None,
          disk_dir: pathlib.Path | None = None,
          control_disk_dir: pathlib.Path | None = None) -> dict[str, Any]:
    meta = load_json(run / "campaign.json", {}) or {}
    devices = meta.get("devices", {}) or {}
    limits = meta.get("limits", {}) or {}
    result: dict[str, Any] = {
        "schema": 2,
        "run": str(run),
        "fault_injection_enabled": bool(limits.get("random_fault_injection")),
        "control": str(control) if control else None,
        "kinds": {},
    }
    for kind in KINDS:
        final = final_evidence(run, kind, disk_dir)
        first = first_anomaly(run, kind)
        context = {"status": "not_observed"}
        if first.get("segment_id") is not None:
            target_manifest = run / "segments" / f"segment-{int(first['segment_id']):06d}" / "manifest.json"
            manifest = load_json(target_manifest, {}) or {}
            context = hook_context(run, target_manifest, manifest, devices,
                                   final["affected_inodes"])
            context["segment_id"] = first["segment_id"]
        control_data = control_summary(run, control, kind, limits, meta,
                                       control_disk_dir)
        matched_inodes = sorted(set(context.get("matched_final_inodes", [])))
        final_affected = set(final["affected_inodes"])
        context["same_inode_as_offline_evidence"] = bool(matched_inodes)
        context["unmatched_offline_inodes"] = sorted(final_affected - set(matched_inodes))
        control_data["causal_control_ready"] = control_data.get("interpretation") == "matched_control"
        result["kinds"][kind] = {
            "first_anomaly": first,
            "hook_context": context,
            "final_evidence": final,
            "control": control_data,
        }
    strengths = [row["hook_context"].get("strength")
                 for row in result["kinds"].values() if row["hook_context"].get("strength")]
    result["summary"] = {
        "strong_links": strengths.count("strong"),
        "partial_links": strengths.count("partial"),
        "control_matched": sum(row["control"].get("interpretation") == "matched_control"
                                for row in result["kinds"].values()),
        "final_disk_problems": sum(bool(row["final_evidence"]["affected_inodes"] or
                                         row["final_evidence"]["fsck"].get("exit") not in (None, 0))
                                    for row in result["kinds"].values()),
        "causal_candidates": sum(
            row["hook_context"].get("strength") == "strong"
            and row["hook_context"].get("same_inode_as_offline_evidence")
            and row["control"].get("causal_control_ready")
            for row in result["kinds"].values()),
        "control_blocked": sum(row["control"].get("interpretation") != "matched_control"
                                for row in result["kinds"].values()),
    }
    return result


def markdown(report: dict[str, Any]) -> str:
    lines = ["# Offline Causality Audit", "", f"Run: `{report['run']}`", ""]
    for kind, row in report["kinds"].items():
        first = row["first_anomaly"]
        hook = row["hook_context"]
        final = row["final_evidence"]
        control = row["control"]
        lines.extend([f"## {kind}", ""])
        if first.get("segment_id") is None:
            lines.append("First anomaly: not observed.")
        else:
            lines.append(f"First anomaly: segment `{first['segment_id']}` ({first['transition']}).")
            lines.append(f"Before: `{first['before']}`")
            lines.append(f"After: `{first['after']}`")
        lines.append(f"Hook link: `{hook.get('strength', hook.get('status'))}`")
        if hook.get("operation"):
            lines.append(f"Hook: `{hook['operation']}` errno `{hook.get('errno')}`, "
                         f"inode candidates `{hook.get('target_inodes', [])}`, "
                         f"final intersection `{hook.get('matched_final_inodes', [])}`.")
        syscall = hook.get("syscall")
        if syscall:
            lines.append(f"Syscall: `{syscall.get('name')}` call `{syscall.get('call_index')}`, "
                         f"verified=`{syscall.get('verified')}`.")
        elif hook.get("operation"):
            lines.append("Syscall: no enclosing CT_SYSCALL envelope; attribution remains partial.")
        else:
            lines.append("Syscall: not applicable; no hook anomaly was observed.")
        if hook.get("syscall_attribution"):
            lines.append(f"Syscall attribution: `{hook['syscall_attribution'].get('method')}`.")
        lines.append(f"Final affected inodes: `{final['affected_inodes']}`")
        lines.append(f"Same inode intersection: `{hook.get('matched_final_inodes', [])}`; "
                     f"unmatched offline inodes: `{hook.get('unmatched_offline_inodes', [])}`.")
        lines.append(f"Control: `{control.get('interpretation', control.get('status'))}`")
        if control.get("parameter_differences"):
            lines.append(f"Control parameter differences: `{sorted(control['parameter_differences'])}`")
        if control.get("blockers"):
            lines.append(f"Control blockers: `{control['blockers']}`")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=pathlib.Path)
    parser.add_argument("--control", type=pathlib.Path)
    parser.add_argument("--disk-dir", type=pathlib.Path,
                        help="use rechecked <kind>-disk.json files from this directory")
    parser.add_argument("--control-disk-dir", type=pathlib.Path,
                        help="use rechecked control disk JSON files from this directory")
    parser.add_argument("--output", type=pathlib.Path,
                        help="JSON output path; Markdown is written beside it")
    args = parser.parse_args()
    report = audit(args.run, args.control, args.disk_dir,
                   args.control_disk_dir)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
        args.output.with_suffix(".md").write_text(markdown(report))
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
