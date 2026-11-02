#!/usr/bin/env python3
"""Fail-closed source/config preflight for ext4 fast-commit replay faults.

This command verifies the constrained mount-time pre-arm source interface and
its placement before journal recovery. It deliberately does not claim a VM hit.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
from typing import Any


HOOKS = {
    "fast-commit-inode-claim": {
        "operation": "fast_commit_inode_claim",
        "enum": "EXT4_FAULT_FAST_COMMIT_INODE_CLAIM",
        "source": "ialloc.c",
        "caller": "ext4_fc_replay_inode/ext4_fc_replay_create",
        "effect": "before inode bitmap claim; claim helper returns errno",
        "caveat": (
            "ext4_fc_replay_inode() routes this helper error to out but "
            "unconditionally returns 0; ext4_fc_replay_create() propagates it"
        ),
    },
}


def line_of(lines: list[str], predicate) -> int | None:
    for index, line in enumerate(lines, start=1):
        if predicate(line):
            return index
    return None


def occurrences(lines: list[str], token: str) -> list[int]:
    return [index for index, line in enumerate(lines, start=1) if token in line]


def function_body(lines: list[str], signature: str) -> tuple[int | None, list[tuple[int, str]]]:
    """Return physical line numbers for one C function body, if found."""
    start = None
    for candidate, line in enumerate(lines):
        if signature not in line:
            continue
        # Skip forward declarations and find the next occurrence that opens
        # a body before its terminating semicolon.
        saw_body = False
        for probe in range(candidate, len(lines)):
            if "{" in lines[probe]:
                saw_body = True
                break
            if ";" in lines[probe]:
                break
        if saw_body:
            start = candidate
            break
    if start is None:
        return None, []
    body: list[tuple[int, str]] = []
    depth = 0
    opened = False
    for index in range(start, len(lines)):
        line = lines[index]
        # These source functions contain no braces in comments/strings that
        # affect the simple lexical count around the call expressions sought.
        depth += line.count("{") - line.count("}")
        opened |= "{" in line
        if opened:
            body.append((index + 1, line))
            if depth == 0:
                break
    return start + 1, body


def body_call(body: list[tuple[int, str]], token: str) -> int | None:
    return next((number for number, line in body if token in line), None)


def read_text(path: pathlib.Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def image_features(image: pathlib.Path | None) -> dict[str, Any]:
    if image is None:
        return {"checked": False, "reason": "no_image_argument"}
    try:
        result = subprocess.run(["dumpe2fs", "-h", str(image)], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"checked": False, "image": str(image),
                "reason": f"dumpe2fs_unavailable_or_failed:{exc.__class__.__name__}"}
    features_line = next((line.split(":", 1)[1].strip()
                          for line in result.stdout.splitlines()
                          if line.startswith("Filesystem features:")), "")
    journal_features = next((line.split(":", 1)[1].strip()
                             for line in result.stdout.splitlines()
                             if line.startswith("Journal features:")), "")
    return {
        "checked": result.returncode == 0,
        "image": str(image),
        "dumpe2fs_exit": result.returncode,
        "filesystem_features": features_line.split(),
        "journal_features": journal_features.split(),
        "fast_commit_feature_present": (
            "fast_commit" in features_line.split() or
            "fast_commit" in journal_features.split()),
        "journal_needs_recovery_visible": any(
            "needs_recovery" in line.lower() for line in result.stdout.splitlines()),
        "raw_header": result.stdout[:1600],
    }


def inspect(project: pathlib.Path, image: pathlib.Path | None) -> dict[str, Any]:
    source = project / "dev/linux-6.12.1/fs/ext4"
    super_path = source / "super.c"
    sysfs_path = source / "sysfs.c"
    fault_path = source / "fault_inject.c"
    ialloc_path = source / "ialloc.c"
    fc_path = source / "fast_commit.c"
    jbd2_journal_path = project / "dev/linux-6.12.1/fs/jbd2/journal.c"
    jbd2_recovery_path = project / "dev/linux-6.12.1/fs/jbd2/recovery.c"
    header_path = source / "fault_inject.h"
    fault_trace_path = project / "dev/linux-6.12.1/include/trace/events/ext4_fault.h"
    config_path = project / "build/linux-counter-trace/.config"

    sources = {str(path.relative_to(project / "dev/linux-6.12.1")): read_text(path)
               for path in (super_path, sysfs_path, fault_path, ialloc_path,
                            fc_path, jbd2_journal_path, jbd2_recovery_path,
                            header_path, fault_trace_path)}
    missing = [name for name, value in sources.items() if value is None]
    source_lines = {name: value.splitlines() for name, value in sources.items()
                    if value is not None}

    super_lines = source_lines.get("fs/ext4/super.c", [])
    sysfs_lines = source_lines.get("fs/ext4/sysfs.c", [])
    fault_lines = source_lines.get("fs/ext4/fault_inject.c", [])
    header = "\n".join(source_lines.get("fs/ext4/fault_inject.h", []))
    config = read_text(config_path)
    sbi_alloc, alloc_body = function_body(super_lines, "static struct ext4_sb_info *ext4_alloc_sbi(")
    fill_start, fill_body = function_body(super_lines, "static int ext4_fill_super(")
    impl_start, impl_body = function_body(super_lines, "static int __ext4_fill_super(")
    journal_fn_start, journal_body = function_body(super_lines, "static int ext4_load_journal(")
    init_journal_start, init_journal_body = function_body(
        super_lines, "static int ext4_load_and_init_journal(")
    fault_init = body_call(alloc_body, "ext4_fault_init(sb);")
    mount_prearm_call = body_call(impl_body,
                                  "ext4_fault_prearm_fast_commit(sb, ctx->fault_prearm);")
    journal_load = body_call(journal_body, "err = jbd2_journal_load(journal);")
    load_journal_call = body_call(init_journal_body, "ext4_load_journal(sb, es,")
    sysfs_register = body_call(impl_body, "err = ext4_register_sysfs(sb);")
    call_init_journal = body_call(impl_body, "err = ext4_load_and_init_journal(sb, es,")
    call_alloc = body_call(fill_body, "sbi = ext4_alloc_sbi(sb);")
    call_impl = body_call(fill_body, "ret = __ext4_fill_super(fc, sb);")
    fast_lines = source_lines.get("fs/ext4/fast_commit.c", [])
    replay_cb = line_of(fast_lines,
                        lambda line: "j_fc_replay_callback = ext4_fc_replay" in line)
    fault_store = line_of(fault_lines, lambda line: "ssize_t ext4_fault_store(" in line)
    fault_show = line_of(fault_lines, lambda line: "ssize_t ext4_fault_show(" in line)
    sysfs_fault_attr = line_of(sysfs_lines,
                               lambda line: "ATTR_LIST(fault_inject)," in line)

    hook_reports: dict[str, Any] = {}
    for plan_name, spec in HOOKS.items():
        file_lines = source_lines.get(f"fs/ext4/{spec['source']}", [])
        call_token = f"{spec['enum']},"
        call_lines = occurrences(file_lines, call_token)
        enum_present = spec["enum"] in header
        operation_present = any(f'"{spec["operation"]}"' in line
                                for line in fault_lines)
        hook_reports[plan_name] = {
            "operation": spec["operation"],
            "enum_present": enum_present,
            "sysfs_operation_parser_present": operation_present,
            "hook_source": str(source / spec["source"]),
            "hook_call_lines": call_lines,
            "hook_call_count": len(call_lines),
            "caller": spec["caller"],
            "hook_effect": spec["effect"],
            "propagation_caveat": spec["caveat"],
            "hook_present": enum_present and operation_present and bool(call_lines),
        }

    built_config = {
        "path": str(config_path),
        "available": config is not None,
        "fault_injection_enabled": bool(config and
                                          re.search(r"^CONFIG_EXT4_FAULT_INJECTION=y$",
                                                    config, re.MULTILINE)),
        "fast_commit_enabled_in_kernel": bool(config and
                                                re.search(r"^CONFIG_EXT4_FS=y$",
                                                          config, re.MULTILINE)),
    }
    lifecycle_order_valid = bool(
        fault_init is not None and call_alloc is not None and call_impl is not None and
        call_init_journal is not None and sysfs_register is not None and
        call_alloc < call_impl and call_init_journal < sysfs_register and
        journal_load is not None and load_journal_call is not None)
    blockers = []
    if missing:
        blockers.append("source_tree_incomplete:" + ",".join(missing))
    if not built_config["fault_injection_enabled"]:
        blockers.append("built_kernel_fault_injection_not_enabled")
    prearm_api = bool(
        "fault_prearm" in "\n".join(super_lines) and
        "ext4_fault_prearm_fast_commit" in "\n".join(fault_lines) and
        "mount_prearmed" in header and
        "TRACE_EVENT(ext4_fault_prearm," in "\n".join(source_lines.get(
            "include/trace/events/ext4_fault.h", [])))
    mount_prearm_before_recovery = bool(
        mount_prearm_call is not None and journal_load is not None and
        mount_prearm_call < call_init_journal and call_init_journal < journal_load)
    if not lifecycle_order_valid:
        blockers.append("could_not_prove_fault_state_init_before_journal_and_sysfs_after_replay")
    elif not mount_prearm_before_recovery or not prearm_api:
        blockers.append("fault_sysfs_is_registered_after_journal_recovery")
    if not prearm_api:
        blockers.append("mount_time_fast_commit_prearm_interface_missing")
    if not mount_prearm_before_recovery:
        blockers.append("mount_time_fast_commit_prearm_not_before_journal_replay")
    jbd2_journal_lines = source_lines.get("fs/jbd2/journal.c", [])
    jbd2_recovery_lines = source_lines.get("fs/jbd2/recovery.c", [])
    jbd2_load_start, jbd2_load_body = function_body(
        jbd2_journal_lines, "int jbd2_journal_load(journal_t *journal)")
    jbd2_recovery_call = body_call(jbd2_load_body, "jbd2_journal_recover(journal);")
    fc_pass_start, fc_pass_body = function_body(
        jbd2_recovery_lines, "static int fc_do_one_pass(")
    fc_callback_call = body_call(fc_pass_body, "journal->j_fc_replay_callback(journal,")
    if jbd2_recovery_call is None or fc_callback_call is None:
        blockers.append("could_not_prove_jbd2_fast_commit_callback_in_journal_recovery")
    for name, report in hook_reports.items():
        if not report["hook_present"]:
            blockers.append("hook_or_operation_missing:" + name)
    image_report = image_features(image)
    if image is not None and not image_report.get("checked"):
        blockers.append("image_feature_check_failed")
    if image is not None and not image_report.get("fast_commit_feature_present"):
        blockers.append("image_has_no_fast_commit_feature")
    if image is not None and image_report.get("fast_commit_feature_present"):
        blockers.append("image_feature_does_not_prove_pending_fast_commit_replay_log")

    return {
        "schema": 1,
        "experiment": "ext4-fast-commit-replay-fault-preflight",
        "status": "blocked" if blockers else "preflight_only",
        "passed": False,
        "vm_attempted": False,
        "fault_injection_claimed": False,
        "source_root": str(source),
        "source_lifecycle": {
            "ext4_alloc_sbi_line": sbi_alloc,
            "ext4_fault_init_line": fault_init,
            "ext4_load_journal_function_line": journal_fn_start,
            "ext4_load_and_init_journal_function_line": init_journal_start,
            "jbd2_journal_load_line": journal_load,
            "ext4_register_sysfs_line": sysfs_register,
            "fault_store_line": fault_store,
            "fault_status_show_line": fault_show,
            "sysfs_fault_attribute_line": sysfs_fault_attr,
            "fast_commit_replay_callback_line": replay_cb,
            "jbd2_journal_load_function_line": jbd2_load_start,
            "jbd2_journal_recover_call_line": jbd2_recovery_call,
            "jbd2_fast_commit_pass_function_line": fc_pass_start,
            "jbd2_fast_commit_callback_call_line": fc_callback_call,
            "ext4_fill_super_function_line": fill_start,
            "__ext4_fill_super_function_line": impl_start,
            "ext4_fill_super_alloc_call_line": call_alloc,
            "ext4_fill_super_body_call_line": call_impl,
            "__ext4_fill_super_journal_call_line": call_init_journal,
            "ext4_load_and_init_journal_load_call_line": load_journal_call,
            "state_initialized_before_recovery": bool(fault_init and call_alloc and
                                                       call_impl and call_alloc < call_impl),
            "mount_prearm_line": mount_prearm_call,
            "mount_prearm_api_present": prearm_api,
            "mount_prearm_before_journal_load": mount_prearm_before_recovery,
            "arm_interface_available_before_recovery": mount_prearm_before_recovery,
            "sysfs_arm_interface_available_only_after_recovery": lifecycle_order_valid,
            "mount_lifecycle_barrier": (
                "sysfs fault_inject is registered after recovery; the opt-in "
                "fault_prearm mount option is copied into the new per-sb state "
                "before ext4_load_and_init_journal invokes jbd2_journal_load"
            ),
        },
        "hooks": hook_reports,
        "kernel_config": built_config,
        "image": image_report,
        "mount_prearm_contract": {
            "option": "fault_prearm=operation:-EIO:1:pid:comm:call_index",
            "allowed_operations": ["fast_commit_inode_claim"],
            "errno": "-EIO",
            "count": 1,
            "target_pid_required": True,
            "target_comm_required": True,
            "call_index_max": 1000000,
            "new_mount_only": True,
            "requires_pending_fast_commit_recovery": True,
            "sysfs_readback_field": "mount_prearmed",
            "prearm_trace_event": "ext4_fault_prearm",
        },
        "required_fixture_protocol": [
            "prepare baseline ext4 image with journal fast_commit feature",
            "create a pending fast-commit transaction and crash without clean unmount",
            "arm the target operation before ext4/jbd2 replay begins",
            "mount once and prove the precise hook hit and replay return behavior",
            "collect stable counter/namespace observations and preserve raw trace",
            "unmount/remount after disabling the fault, then verify fsck and disk formulas",
        ],
        "blockers": blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().parents[1])
    parser.add_argument("--image", type=pathlib.Path,
                        help="optional ext4 image to inspect with dumpe2fs -h")
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    report = inspect(args.project.resolve(), args.image.resolve() if args.image else None)
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
    return 2 if report["status"] == "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
