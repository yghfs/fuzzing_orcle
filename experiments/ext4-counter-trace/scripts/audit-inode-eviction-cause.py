#!/usr/bin/env python3
"""Audit the ext4 inode-eviction bitmap leak from source and campaign evidence.

This report is deliberately conservative.  It proves the source-side state
transition and links fault-hook hits to the persistent image, but it does not
turn a fault-only hit into a natural-syscall finding.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import subprocess
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[3]
CAUSE_SCRIPT = pathlib.Path(__file__).with_name("analyze-offline-causality.py")
_SPEC = importlib.util.spec_from_file_location("offline_causality", CAUSE_SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"cannot load {CAUSE_SCRIPT}")
_CAUSE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CAUSE)


def load_json(path: pathlib.Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def source_lines(path: pathlib.Path) -> list[str]:
    return path.read_text(errors="replace").splitlines()


def line_of(lines: list[str], pattern: str, start: int = 0) -> int | None:
    regex = re.compile(pattern)
    for index in range(start, len(lines)):
        if regex.search(lines[index]):
            return index + 1
    return None


def function_end(lines: list[str], start: int) -> int:
    """Return the exclusive end of a C function using its brace depth."""
    opening = next((index for index in range(start, len(lines))
                    if "{" in lines[index]), None)
    if opening is None:
        return len(lines)
    depth = 0
    in_block_comment = False
    for index in range(opening, len(lines)):
        text = lines[index]
        visible = []
        offset = 0
        while offset < len(text):
            if in_block_comment:
                close = text.find("*/", offset)
                if close < 0:
                    break
                in_block_comment = False
                offset = close + 2
            elif text.startswith("/*", offset):
                in_block_comment = True
                offset += 2
            elif text.startswith("//", offset):
                break
            else:
                visible.append(text[offset])
                offset += 1
        code = "".join(visible)
        depth += code.count("{") - code.count("}")
        if index >= opening and depth == 0:
            return index + 1
    return len(lines)


def snippet(lines: list[str], line: int | None, radius: int = 2) -> dict[str, Any] | None:
    if line is None:
        return None
    first = max(1, line - radius)
    last = min(len(lines), line + radius)
    return {
        "line": line,
        "text": "\n".join(f"{number}: {lines[number - 1]}"
                           for number in range(first, last + 1)),
    }


def git_identity(tree: pathlib.Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(tree)}
    try:
        result["head"] = subprocess.check_output(
            ["git", "-C", str(tree), "rev-parse", "HEAD"], text=True,
            stderr=subprocess.DEVNULL).strip()
        result["subject"] = subprocess.check_output(
            ["git", "-C", str(tree), "show", "-s", "--format=%s", "HEAD"],
            text=True, stderr=subprocess.DEVNULL).strip()
        result["parent"] = subprocess.check_output(
            ["git", "-C", str(tree), "rev-parse", "HEAD^"], text=True,
            stderr=subprocess.DEVNULL).strip()
        result["ext4_eviction_sources_match_parent"] = subprocess.run(
            ["git", "-C", str(tree), "diff", "--quiet", "HEAD^", "HEAD",
             "--", "fs/ext4/inode.c", "fs/ext4/ialloc.c"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.CalledProcessError):
        result["head"] = None
        result["subject"] = None
        result["parent"] = None
        result["ext4_eviction_sources_match_parent"] = None
    return result


def audit_source(tree: pathlib.Path) -> dict[str, Any]:
    inode_path = tree / "fs/ext4/inode.c"
    ialloc_path = tree / "fs/ext4/ialloc.c"
    inode = source_lines(inode_path)
    ialloc = source_lines(ialloc_path)
    evict_start = line_of(inode, r"^void ext4_evict_inode\s*\(")
    free_start = line_of(ialloc, r"^void ext4_free_inode\s*\(")
    if evict_start is None or free_start is None:
        return {
            "status": "source_not_found",
            "tree": git_identity(tree),
            "inode": str(inode_path),
            "ialloc": str(ialloc_path),
        }
    evict_end = function_end(inode, evict_start - 1)
    evict = inode[evict_start - 1:evict_end]
    clear_rel = line_of(evict, r"ext4_clear_inode\s*\(")
    free_rel = line_of(evict, r"ext4_free_inode\s*\(")
    dirty_matches = [index + 1 for index, text in enumerate(evict)
                     if re.search(r"ext4_(?:mark_inode_dirty|mark_inode_dirty_faultable)\s*\(", text)]
    # The eviction function also dirties the inode during truncate.  The last
    # call is the final deletion write whose failure selects the clear-only
    # branch below.
    dirty_rel = dirty_matches[-1] if dirty_matches else None
    dirty_error_rel = None
    if dirty_rel is not None:
        dirty_error_rel = line_of(evict, r"if\s*\([^\n]*mark_inode_dirty", dirty_rel - 1)
    free_end = function_end(ialloc, free_start - 1)
    free_body = ialloc[free_start - 1:free_end]
    update_start = line_of(inode, r"^static int ext4_do_update_inode\s*\(")
    update_body: list[str] = []
    if update_start is not None:
        update_end = function_end(inode, update_start - 1)
        update_body = inode[update_start - 1:update_end]
    raw_update_rel = line_of(update_body, r"ext4_fill_raw_inode\s*\(")
    dirty_metadata_rel = line_of(update_body, r"ext4_handle_dirty_metadata\s*\(")
    raw_update_abs = update_start + raw_update_rel - 1 if update_start and raw_update_rel else None
    dirty_metadata_abs = (update_start + dirty_metadata_rel - 1
                          if update_start and dirty_metadata_rel else None)
    free_features = {
        "clear_inode_bitmap": line_of(free_body, r"ext4_test_and_clear_bit\s*\(") is not None,
        "increment_group_free_inodes": line_of(free_body, r"ext4_free_inodes_set\s*\(") is not None,
        "decrement_group_used_dirs": line_of(free_body, r"ext4_used_dirs_set\s*\(") is not None,
        "increment_global_free_inodes": line_of(free_body, r"s_freeinodes_counter") is not None,
        "dirty_bitmap": line_of(free_body, r"ext4_handle_dirty_metadata\s*\(") is not None,
    }
    source_ok = (
        clear_rel is not None and free_rel is not None and dirty_rel is not None
        and clear_rel < free_rel and all(free_features.values())
    )
    dirty_abs = evict_start + dirty_rel - 1 if dirty_rel is not None else None
    dirty_error_abs = (evict_start + dirty_error_rel - 1
                       if dirty_error_rel is not None else None)
    clear_abs = evict_start + clear_rel - 1 if clear_rel is not None else None
    free_abs = evict_start + free_rel - 1 if free_rel is not None else None
    dtime_rel = line_of(evict, r"i_dtime\s*=")
    orphan_matches = [index + 1 for index, text in enumerate(evict)
                      if re.search(r"ext4_orphan_del\s*\(", text)
                      and not text.lstrip().startswith(("*", "/*"))]
    orphan_rel = orphan_matches[-1] if orphan_matches else None
    bitmap_rel = line_of(free_body, r"ext4_test_and_clear_bit\s*\(")
    free_counter_rel = line_of(free_body, r"ext4_free_inodes_set\s*\(")
    dtime_abs = evict_start + dtime_rel - 1 if dtime_rel is not None else None
    orphan_abs = evict_start + orphan_rel - 1 if orphan_rel is not None else None
    bitmap_abs = free_start + bitmap_rel - 1 if bitmap_rel is not None else None
    free_counter_abs = (free_start + free_counter_rel - 1
                        if free_counter_rel is not None else None)
    used_dirs_rel = line_of(free_body, r"ext4_used_dirs_set\s*\(")
    used_dirs_abs = free_start + used_dirs_rel - 1 if used_dirs_rel is not None else None
    return {
        "status": "verified" if source_ok else "source_pattern_incomplete",
        "tree": git_identity(tree),
        "inode": {
            "path": str(inode_path),
            "function_line": evict_start,
            "dirty_call": snippet(inode, dirty_abs),
            "dirty_error_branch": snippet(inode, dirty_error_abs),
            "dtime_assignment": snippet(inode, dtime_abs),
            "orphan_delete": snippet(inode, orphan_abs),
            "failure_clear": snippet(inode, clear_abs),
            "success_free": snippet(inode, free_abs),
            "clear_precedes_free": bool(clear_rel and free_rel and clear_rel < free_rel),
            "raw_inode_serialization": snippet(inode, raw_update_abs),
            "metadata_dirty": snippet(inode, dirty_metadata_abs),
        },
        "ialloc": {
            "path": str(ialloc_path),
            "function_line": free_start,
            "features": free_features,
            "bitmap_transition": snippet(ialloc, bitmap_abs),
            "counter_transition": snippet(ialloc, free_counter_abs),
            "directory_counter_transition": snippet(ialloc, used_dirs_abs),
        },
        "source_cause_proven": source_ok,
    }


def audit_fault_hook(tree: pathlib.Path) -> dict[str, Any]:
    """Describe the experiment-only wrapper without calling it native evidence."""
    inode_path = tree / "fs/ext4/inode.c"
    jbd2_path = tree / "fs/ext4/ext4_jbd2.c"
    if not inode_path.is_file() or not jbd2_path.is_file():
        return {"status": "hook_source_not_found", "tree": str(tree)}
    inode = source_lines(inode_path)
    jbd2 = source_lines(jbd2_path)
    final_hook = line_of(inode, r"EXT4_FAULT_INODE_EVICT_FINAL_DIRTY")
    wrapper = line_of(inode, r"^int ext4_mark_inode_dirty_faultable\s*\(")
    dirty_hook_start = line_of(jbd2, r"^int __ext4_handle_dirty_metadata_faultable\s*\(")
    dirty_hook_body: list[str] = []
    if dirty_hook_start is not None:
        dirty_hook_end = function_end(jbd2, dirty_hook_start - 1)
        dirty_hook_body = jbd2[dirty_hook_start - 1:dirty_hook_end]
    injector_rel = line_of(dirty_hook_body, r"ext4_fault_maybe_inject\s*\(")
    native_rel = line_of(dirty_hook_body, r"return __ext4_handle_dirty_metadata\s*\(",
                         injector_rel or 0)
    injector = dirty_hook_start + injector_rel - 1 if dirty_hook_start and injector_rel else None
    native_fallback = (dirty_hook_start + native_rel - 1
                       if dirty_hook_start and native_rel else None)
    return {
        "status": "verified" if final_hook and wrapper and injector else "incomplete",
        "tree": str(tree),
        "final_hook": snippet(inode, final_hook),
        "faultable_wrapper": snippet(inode, wrapper),
        "metadata_injection": snippet(jbd2, injector),
        "native_fallback": snippet(jbd2, native_fallback),
        "injection_before_native_jbd2": bool(injector and native_fallback and injector < native_fallback),
        "interpretation": (
            "The experiment returns an allowed helper-style errno before the native "
            "JBD2 call on a selected hit; the following ext4 error branch is real, "
            "but this does not prove the underlying error occurs naturally."
        ),
    }


def parse_fsck(path: pathlib.Path) -> dict[str, Any]:
    text = path.read_text(errors="replace") if path.is_file() else ""
    dtime = sorted({int(value) for value in re.findall(
        r"(?:deleted inode|已删除\s*inode)\s+(\d+).*?(?:dtime|删除时间)",
        text, re.IGNORECASE)})
    bitmap = sorted({int(value) for section in re.findall(
        r"(?:Inode bitmap differences|Inode 位图的差异)\s*[:：]\s*([^\n]+)",
        text, re.IGNORECASE) for value in re.findall(r"-(\d+)", section)})
    directories = []
    for match in re.finditer(
        r"Directories count wrong for group\s*#?(\d+)\s*\((\d+),\s*counted=(\d+)\)"
        r"|组\s*#\s*(\d+)\s*的目录计数错误\s*\((\d+),\s*counted=(\d+)\)",
        text, re.IGNORECASE):
        group, stored, counted = (match.group(1), match.group(2), match.group(3))
        if group is None:
            group, stored, counted = match.group(4), match.group(5), match.group(6)
        directories.append({"group": int(group), "stored": int(stored),
                            "counted": int(counted),
                            "excess": int(stored) - int(counted)})
    return {"path": str(path), "deleted_dtime_zero": dtime,
            "bitmap_clear_candidates": bitmap,
            "directory_count_mismatches": directories,
            "exit_nonzero": bool(dtime or bitmap or directories)}


def inode_types(image: pathlib.Path, inodes: list[int]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not image.is_file():
        return result
    for ino in inodes:
        try:
            output = subprocess.run(
                ["debugfs", "-R", f"stat <{ino}>", str(image)],
                capture_output=True, text=True, check=False).stdout
        except OSError:
            return {}
        type_match = re.search(r"\bType:\s*(\w+)", output)
        links_match = re.search(r"\bLinks:\s*(\d+)", output)
        mode_match = re.search(r"\bMode:\s*([0-7]+)", output)
        result[str(ino)] = {
            "type": type_match.group(1) if type_match else None,
            "links": int(links_match.group(1)) if links_match else None,
            "mode": mode_match.group(1) if mode_match else None,
        }
    return result


def final_disk(run: pathlib.Path, kind: str) -> dict[str, Any]:
    path = run / f"{kind}-disk.json"
    disk = load_json(path, {}) or {}
    lifecycle = disk.get("r_inode_lifecycle") if isinstance(disk, dict) else None
    affected: list[int] = []
    if isinstance(lifecycle, dict):
        for item in lifecycle.get("mismatches", []):
            try:
                affected.append(int(item["ino"]))
            except (KeyError, TypeError, ValueError):
                pass
    fsck = parse_fsck(run / f"{kind}-fsck.txt")
    persistent_affected = sorted(set(fsck["deleted_dtime_zero"]) |
                                 set(fsck["bitmap_clear_candidates"]))
    types = inode_types(run / f"{kind}.raw", persistent_affected)
    deleted_dirs = sum(value.get("type") == "directory" for value in types.values())
    directory_excess = sum(item["excess"] for item in fsck["directory_count_mismatches"])
    return {
        "disk": str(path),
        "status": disk.get("status") if isinstance(disk, dict) else None,
        "valid": disk.get("valid") if isinstance(disk, dict) else None,
        "lifecycle_affected_inodes": sorted(set(affected)),
        "persistent_affected_inodes": persistent_affected,
        "inode_details": types,
        "deleted_directories_from_inode_table": deleted_dirs,
        "fsck": fsck,
        "directory_counter_excess_matches_deleted_directories": (
            deleted_dirs == directory_excess if fsck["directory_count_mismatches"] else None),
        "persistent_mismatch": bool(
            fsck["exit_nonzero"] or disk.get("valid") is False
            if isinstance(disk, dict) else fsck["exit_nonzero"]),
    }


def trace_hook(segment: pathlib.Path) -> dict[str, Any] | None:
    manifest = load_json(segment / "manifest.json", {}) or {}
    fault = manifest.get("fault_injection") or {}
    status = fault.get("status") or {}
    if not fault.get("hit") or status.get("errno_operation") != "inode_evict_final_dirty":
        return None
    trace = _CAUSE.trace_path(segment)
    if trace is None:
        return {"status": "trace_missing"}
    rows = _CAUSE.trace_rows(trace)
    events = [row for row in rows if row.get("kind") == "fault"
              and row.get("operation") == "inode_evict_final_dirty"
              and row.get("injection") == 1]
    if not events:
        return {"status": "fault_event_missing"}
    fault_row = events[0]
    pid = fault_row.get("pid")
    target_before = [row for row in rows if row.get("kind") == "counter"
                     and row.get("pid") == pid
                     and row.get("point") == "INODE.EVICT.TARGET.BEFORE"
                     and int(row.get("line", 0)) <= int(fault_row["line"])
                     and int(fault_row["line"]) - int(row.get("line", 0)) <= 180]
    # There can be another eviction by the same worker immediately after the
    # hook.  The latest BEFORE event is the target for this failure.
    target_row = max(target_before, key=lambda row: int(row.get("line", 0)),
                     default=None)
    target_inodes = ([int(target_row["ino"])] if target_row and target_row.get("ino")
                     else [])
    calls, issues = _CAUSE.correlate_syscalls([row for row in rows if "body" in row])
    enclosing = [call for call in calls if call.get("pid") == pid
                 and call.get("marker_begin_line", 0) < fault_row["line"]
                 < call.get("marker_end_line", 0)]
    syscall = None
    if len(enclosing) == 1:
        syscall = {key: enclosing[0].get(key) for key in (
            "id", "name", "call_index", "user_result", "user_errno", "verified",
            "marker_begin_line", "marker_end_line")}
    return {
        "status": "verified" if target_inodes else "hook_without_inode",
        "segment": int(manifest.get("segment_id", -1)),
        "kind": manifest.get("kind"),
        "operation": status.get("errno_operation"),
        "errno": status.get("errno"),
        "pid": pid,
        "fault_line": fault_row.get("line"),
        "target_inodes": target_inodes,
        "syscall": syscall,
        "syscall_attribution": "exact_ct_syscall_envelope" if syscall else
                               "no_or_ambiguous_ct_syscall_envelope",
        "correlation_issues": issues,
    }


def hook_hits(campaign: pathlib.Path) -> list[dict[str, Any]]:
    hits = []
    for manifest_path in sorted((campaign / "segments").glob("segment-*/manifest.json")):
        hit = trace_hook(manifest_path.parent)
        if hit is not None:
            hits.append(hit)
    return hits


def control_summary(control: pathlib.Path | None, kind: str) -> dict[str, Any]:
    if control is None:
        return {"status": "missing"}
    meta = load_json(control / "campaign.json", {}) or {}
    disk = final_disk(control, kind)
    limits = meta.get("limits", {}) or {}
    return {
        "status": "clean" if disk["valid"] is True and not disk["fsck"]["exit_nonzero"] else "degraded",
        "fault_setting": limits.get("random_fault_injection"),
        "disk": disk,
        "clean_shutdown": meta.get("clean_shutdown"),
        "run_status": meta.get("status"),
        "limits": meta.get("limits", {}),
    }


def build_report(campaign: pathlib.Path, control: pathlib.Path | None,
                 source: pathlib.Path, fault_source: pathlib.Path) -> dict[str, Any]:
    hits = hook_hits(campaign)
    campaign_meta = load_json(campaign / "campaign.json", {}) or {}
    by_kind = {}
    for kind in ("regular", "bigalloc", "inline"):
        final = final_disk(campaign, kind)
        final_inodes = set(final["fsck"]["deleted_dtime_zero"]) | set(
            final["fsck"]["bitmap_clear_candidates"])
        kind_hits = [hit for hit in hits if hit.get("kind") == kind]
        for hit in kind_hits:
            hit["final_inode_intersection"] = sorted(set(hit["target_inodes"]) & final_inodes)
        by_kind[kind] = {
            "final": final,
            "hook_hits": kind_hits,
            "control": control_summary(control, kind),
            "campaign_control_audit": (
                _CAUSE.control_summary(campaign, control, kind,
                                       campaign_meta.get("limits", {}) or {},
                                       campaign_meta) if control else {"interpretation": "control_missing"}),
        }
    source_audit = audit_source(source)
    return {
        "schema": 1,
        "campaign": str(campaign),
        "control": str(control) if control else None,
        "campaign_limits": campaign_meta.get("limits", {}),
        "source_audit": source_audit,
        "fault_hook_audit": audit_fault_hook(fault_source),
        "hook_hits": hits,
        "kinds": by_kind,
        "conclusion": {
            "primary_formula": "R-IEVICT",
            "r_ig_necessarily_violated": False,
            "r_ig_reason": "The inode bit and free-inode counters are both left allocated when ext4_free_inode() is skipped.",
            "source_error_path": bool(source_audit.get("source_cause_proven")),
            "persistent_offline_evidence": any(
                row["final"]["fsck"]["exit_nonzero"] for row in by_kind.values()),
            "natural_syscall_proven": False,
            "reason_natural_syscall_unproven": (
                "All linked hits in this campaign use inode_evict_final_dirty fault injection; "
                "the hook has no syscall errno return. A natural reachability experiment is required."
            ),
            "interpretation": (
                "The source error branch is proven and explains the persistent inode state, "
                "but this report does not classify the fault-only trigger as a natural syscall vulnerability."
            ),
        },
    }


def markdown(report: dict[str, Any]) -> str:
    source = report["source_audit"]
    hook_source = report["fault_hook_audit"]
    inode_source = source.get("inode", {})
    ialloc_source = source.get("ialloc", {})
    tree = source.get("tree", {})
    lines = ["# Inode Eviction Disk-Cause Audit", "",
             f"Campaign: `{report['campaign']}`", "",
             "## Source", "",
             f"- Source pattern: `{source.get('status')}`; source cause proven: `{source.get('source_cause_proven')}`.",
             f"- Tree: `{tree.get('path')}` HEAD `{tree.get('head')}`; parent `{tree.get('parent')}`; eviction source files match parent: `{tree.get('ext4_eviction_sources_match_parent')}`.",
             f"- `inode.c:{inode_source.get('orphan_delete', {}).get('line')}` removes the orphan record, then `inode.c:{inode_source.get('dtime_assignment', {}).get('line')}` assigns `i_dtime` in memory.",
             f"- `inode.c:{inode_source.get('dirty_call', {}).get('line')}` performs the final inode dirty. On failure, `inode.c:{inode_source.get('failure_clear', {}).get('line')}` clears only the in-core inode; the `else` at `inode.c:{inode_source.get('success_free', {}).get('line')}` is the sole call to `ext4_free_inode()`.",
             f"- The dirty helper serializes inode fields into the raw inode at `inode.c:{inode_source.get('raw_inode_serialization', {}).get('line')}`, then journals that buffer at `inode.c:{inode_source.get('metadata_dirty', {}).get('line')}`. A dirty-metadata error means the new `i_dtime` was not committed; the image retains zero.",
             f"- `ialloc.c:{ialloc_source.get('bitmap_transition', {}).get('line')}` clears the allocation bit. `ialloc.c:{ialloc_source.get('counter_transition', {}).get('line')}` increments free-inode count; the same function decrements `bg_used_dirs_count` at `ialloc.c:{ialloc_source.get('directory_counter_transition', {}).get('line')}`. None of these run on the final-dirty error branch.",
             "- The source comment says freeing is unsafe after a failed inode dirty. The conservative result is an allocated, zero-link inode left for filesystem checking; this explains the on-disk state and directory summary drift.",
             f"- Fault hook audit: `{hook_source.get('status')}`. The experiment wrapper injects before the native JBD2 dirty call (`injection_before_native_jbd2={hook_source.get('injection_before_native_jbd2')}`); it exercises the real ext4 error branch but does not establish natural error reachability.",
             "",
             "## Hook Hits", ""]
    for hit in report["hook_hits"]:
        syscall = hit.get("syscall")
        call_text = (f"`{syscall.get('name')}` call `{syscall.get('call_index')}` returned "
                     f"`{syscall.get('user_result')}` errno `{syscall.get('user_errno')}`"
                     if syscall else "syscall envelope unavailable")
        lines.append(
            f"- segment `{hit.get('segment')}` `{hit.get('kind')}`: "
            f"`{hit.get('operation')}` errno `{hit.get('errno')}`, "
            f"inode `{hit.get('target_inodes')}`, {call_text}; "
            f"final intersection `{hit.get('final_inode_intersection', [])}`.")
    if not report["hook_hits"]:
        lines.append("- No linked final-dirty hook hit was found.")
    lines.extend(["", "## Persistent State", ""])
    for kind, row in report["kinds"].items():
        final = row["final"]
        details = ", ".join(
            f"{ino}:{data.get('type')}/nlink={data.get('links')}"
            for ino, data in final.get("inode_details", {}).items()) or "none"
        lines.append(
            f"- `{kind}`: disk checker `{final['status']}`; persistent mismatch `{final['persistent_mismatch']}`; "
                  f"fsck dtime-zero `{final['fsck']['deleted_dtime_zero']}`; "
            f"bitmap candidates `{final['fsck']['bitmap_clear_candidates']}`; "
            f"inode types `{details}`; directory excess `{final['fsck']['directory_count_mismatches']}` "
            f"(matches deleted directory inodes: `{final['directory_counter_excess_matches_deleted_directories']}`); "
            f"control `{row['campaign_control_audit'].get('interpretation')}`.")
        blockers = row["campaign_control_audit"].get("blockers", [])
        if blockers:
            lines.append(f"  Control blockers: `{blockers}`.")
    conclusion = report["conclusion"]
    lines.extend(["", "## Boundary", "",
                  f"- Primary violated postcondition: `{conclusion['primary_formula']}`. `R-IG` is not necessarily violated because both bitmap and free-inode counters remain allocated.",
                  f"- Persistent offline evidence: `{conclusion['persistent_offline_evidence']}`.",
                  "- Natural syscall trigger proven: `False`.",
                  f"- {conclusion['reason_natural_syscall_unproven']}",
                  "- " + conclusion["interpretation"], ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=pathlib.Path)
    parser.add_argument("--control", type=pathlib.Path)
    parser.add_argument("--source", type=pathlib.Path,
                        default=ROOT / "kernel/linux-mainline")
    parser.add_argument("--fault-source", type=pathlib.Path,
                        default=ROOT / "experiments/ext4-counter-trace/dev/linux-6.12.1")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    report = build_report(args.campaign, args.control, args.source, args.fault_source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    args.output.with_suffix(".md").write_text(markdown(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
