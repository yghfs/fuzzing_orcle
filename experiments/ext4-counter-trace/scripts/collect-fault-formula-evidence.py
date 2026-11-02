#!/usr/bin/env python3
"""Collect and normalize the deterministic formula-fault guest evidence."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import sys
from typing import Any


PREFIX = re.compile(
    r"(?P<comm>.+)-(?P<pid>\d+)\s+\[\d+\]\s+\S+\s+"
    r"(?P<time>\d+\.\d+):\s*(?P<body>.*)"
)
KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")
LOSS = re.compile(r"^(?:overrun|commit overrun|dropped events):\s*(\d+)$", re.M)


def recovery_contract(plan: dict[str, Any]) -> dict[str, Any]:
    configured = plan.get("recovery")
    if isinstance(configured, dict):
        return configured
    if plan.get("fault_id") == "inode-evict-final-dirty-eio":
        return {
            "status": "verified",
            "pre_recovery": {
                "fsck": {"state": "known_issue",
                         "required_signatures": [
                             "Deleted inode {target_inode} has zero dtime",
                             "Inode bitmap differences:"]},
                "disk": {"state": "verified"},
            },
            "repair": {"kind": "e2fsck-fy", "allowed_exit": [0, 1],
                       "required_signatures": ["FILE SYSTEM WAS MODIFIED"]},
            "post_recovery": {"fsck": "clean", "disk": "verified"},
            "allowed_delta": {"free_clusters": 0, "free_inodes": 1,
                              "dirs": 0},
        }
    return {"status": "unverified"}


def contract_state(contract: dict[str, Any], section: str,
                   key: str, default: str) -> str:
    value = contract.get(section, {})
    if not isinstance(value, dict):
        return default
    value = value.get(key, {})
    if isinstance(value, dict):
        return str(value.get("state", default))
    return str(value)


def contract_signatures(contract: dict[str, Any], section: str,
                        key: str) -> list[str]:
    value = contract.get(section, {})
    if not isinstance(value, dict):
        return []
    value = value.get(key, value if key == "command" else {})
    if not isinstance(value, dict):
        return []
    items = value.get("required_signatures", [])
    return [str(item) for item in items] if isinstance(items, list) else []


def load(path: pathlib.Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save(directory: pathlib.Path, name: str, value: Any) -> None:
    (directory / name).write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def integer(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except ValueError:
            pass
    return {}


def build_association_index(plan: dict[str, Any], workload: dict[str, Any],
                            rows: list[dict[str, Any]], hit_rows: list[dict[str, Any]],
                            formula_events: list[dict[str, Any]],
                            snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Create the auditable hit -> syscall -> oracle -> object chain.

    A collector summary is useful for browsing but is not a causal proof.  The
    index therefore stores only line numbers and values that can be rebuilt
    from the raw trace and archived JSON files.  The v2 validator re-derives
    each link and rejects a hand-edited index.
    """
    pid = integer(workload.get("pid"))
    begin_rows = [row for row in rows if pid is not None and row["pid"] == pid
                  and "CT_SYSCALL" in row["body"]
                  and row["fields"].get("phase") == "BEGIN"]
    end_rows = [row for row in rows if pid is not None and row["pid"] == pid
                and "CT_SYSCALL" in row["body"]
                and row["fields"].get("phase") == "END"]
    begin = begin_rows[0] if len(begin_rows) == 1 else None
    end = end_rows[0] if len(end_rows) == 1 else None
    syscall_window = None
    if begin is not None and end is not None:
        syscall_nr = integer(begin["fields"].get("nr"))
        enters = [row for row in rows if pid is not None and row["pid"] == pid
                  and begin["line"] < row["line"] < end["line"]
                  and "sys_enter:" in row["body"] and
                  (syscall_nr is None or re.search(
                      r"\bNR\s+" + str(syscall_nr) + r"\b", row["body"]))]
        hit_line = min((row["line"] for row in hit_rows), default=None)
        before_hit = [row for row in enters
                      if hit_line is not None and row["line"] < hit_line]
        target_enter = max(before_hit, key=lambda row: row["line"]) \
            if before_hit else (enters[0] if len(enters) == 1 else None)
        exits = []
        if target_enter is not None:
            exits = [row for row in rows if pid is not None and row["pid"] == pid
                     and target_enter["line"] < row["line"] < end["line"]
                     and "sys_exit:" in row["body"] and
                     (syscall_nr is None or re.search(
                         r"\bNR\s+" + str(syscall_nr) + r"\s*=", row["body"]))]
        target_exit = exits[-1] if exits else None
        syscall_window = {
            "marker_begin_line": begin["line"],
            "native_enter_line": target_enter["line"] if target_enter else None,
            "fault_hit_lines": [row["line"] for row in hit_rows],
            "native_exit_line": target_exit["line"] if target_exit else None,
            "marker_end_line": end["line"],
            "id": begin["fields"].get("id"),
            "nr": syscall_nr,
            "program": begin["fields"].get("program"),
            "call_index": integer(begin["fields"].get("call_index")),
            "raw_return": (integer(re.search(
                r"\bsys_exit:\s*NR\s+\d+\s*=\s*(-?\d+)",
                target_exit["body"]).group(1)) if target_exit and re.search(
                    r"\bsys_exit:\s*NR\s+\d+\s*=\s*(-?\d+)",
                    target_exit["body"]) else None),
        }

    object_names: set[str] = set()
    expected_objects = plan.get("expected_objects")
    if isinstance(expected_objects, dict):
        present = expected_objects.get("present", {})
        if isinstance(present, dict):
            object_names.update(str(name) for name in present)
        for key in ("same_state", "same_inode", "swapped_inode",
                    "decreased_extent_count"):
            for relation in expected_objects.get(key, []) or []:
                if isinstance(relation, list):
                    object_names.update(str(name) for name in relation)
        for relation in expected_objects.get("state_transitions", []) or []:
            if isinstance(relation, dict):
                for key in ("before", "after"):
                    if isinstance(relation.get(key), str):
                        object_names.add(relation[key])
    observed_contract = plan.get("observed_object_fields")
    if isinstance(observed_contract, dict):
        object_names.update(str(name) for name in observed_contract)
    objects = {}
    for name in sorted(object_names):
        value = workload.get(name)
        if isinstance(value, dict):
            objects[name] = {field: value.get(field) for field in (
                "exists", "stat_ok", "ino", "nlink", "size", "blocks",
                "extent_count", "extent_hash", "xattr_present", "xattr_size",
                "xattr_hash") if field in value}

    event_links = []
    hit_lines = {row["line"] for row in hit_rows}
    for index, event in enumerate(formula_events):
        line = integer(event.get("trace_line"))
        event_links.append({
            "index": index,
            "trace_line": line,
            "formula": event.get("formula"),
            "point": event.get("point"),
            "pid": integer(event.get("pid")),
            "after_fault_hit": line is not None and bool(hit_lines) and
            line > min(hit_lines),
            "source_trace_lines": event.get("source_trace_lines", []),
        })
    stable_links = {}
    for name, value in snapshots.items():
        stable_links[name] = {
            "path": name,
            "snapshot_id": integer(value.get("snapshot_id")),
            "version": integer(value.get("version")),
            "valid": value.get("valid"),
            "relations_valid": value.get("relations_valid"),
            "namespace_valid": value.get("namespace_valid"),
        }
    return {
        "schema": 1,
        "operation": plan.get("operation"),
        "fault_id": plan.get("fault_id"),
        "target": {"pid": pid, "program": plan.get("program"),
                    "call_index": plan.get("call_index"),
                    "generation": integer(
                        json_object(workload.get("status_before")).get("generation"))},
        "fault_hits": [{"index": index, "trace_line": row["line"],
                        "pid": row["pid"], "operation": row["fields"].get("operation"),
                        "errno": integer(row["fields"].get("errno")),
                        "match_index": integer(row["fields"].get("match_index"))}
                       for index, row in enumerate(hit_rows)],
        "syscall": syscall_window,
        "formula_events": event_links,
        "objects": objects,
        "stable_snapshots": stable_links,
        "links": [{"fault_hit_indices": list(range(len(hit_rows))),
                   "formula_event_indices": [item["index"] for item in event_links
                                              if item["after_fault_hit"]],
                   "object_names": sorted(objects),
                   "stable_snapshot_names": sorted(stable_links)}],
    }


def collect_control(project: pathlib.Path, run: pathlib.Path, guest_exit: int,
                    fsck_exit: int, disk_exit: int,
                    pre_recovery_fsck_exit: int,
                    pre_recovery_disk_exit: int, recovery_exit: int) -> int:
    """Collect a matched no-injection workload as a separate evidence kind."""
    evidence = run / "evidence"
    raw = run / "fault-formula"
    evidence.mkdir(parents=True, exist_ok=True)
    plan = load(evidence / "fault-plan.json", {}) or {}
    plan["control_mode"] = True
    save(evidence, "fault-plan.json", plan)
    workload = load(raw / "workload-result.json", {}) or {}
    rows = read_rows(raw / "trace.txt")
    pid = integer(workload.get("pid"))
    losses = []
    for stats_path in sorted(raw.glob("cpu*-stats.txt")):
        losses.extend(int(value) for value in
                      LOSS.findall(stats_path.read_text(errors="replace")))
    trace_loss = sum(losses) if losses else 1
    hit_rows = [row for row in rows if pid is not None and row["pid"] == pid and
                "ext4_fault_inject:" in row["body"]]
    snapshots = {}
    for name in ("stable-before.json", "stable-after.json", "stable-oracle.json",
                 "remount.json"):
        source = raw / name
        snapshot = load(source, {}) or {}
        snapshot = dict(snapshot)
        snapshot["trace_loss"] = trace_loss
        snapshot["workers"] = 1
        snapshots[name] = dict(snapshot)
        save(evidence, name, snapshots[name])
        for suffix in ("a.json", "b.json"):
            pair = raw / f"{name}.{suffix}"
            if pair.is_file():
                shutil.copy2(pair, evidence / pair.name)
    # The control has no fault-derived formula event.  Its association index
    # still binds the native syscall window and records the object/snapshot
    # state, making it comparable to a fault run without treating absence of a
    # hit as a failed injection.
    save(evidence, "formula-events.json", [])
    save(evidence, "hits.json", [])
    association = build_association_index(plan, workload, rows, hit_rows, [],
                                          snapshots)
    save(evidence, "association-index.json", association)
    for name in ("trace.txt", "cpu0-stats.txt", "unmount-status.txt", "run-status.txt",
                 "workload.txt", "workload-result.json", "remount-target.json",
                 "remount-target.err"):
        source = raw / name
        if source.is_file():
            shutil.copy2(source, evidence / name)
    for name in ("baseline-fsck.txt", "fsck-before-recovery.txt", "fsck-recovery.txt",
                 "fsck.txt", "disk-before-recovery.json", "disk.json",
                 "image-integrity.json"):
        source = run / name
        if source.is_file():
            shutil.copy2(source, evidence / name)
    persistence = {
        "kind": "matched_control",
        "checked": guest_exit == 0 and fsck_exit == 0 and disk_exit == 0,
        "valid": guest_exit == 0 and fsck_exit == 0 and disk_exit == 0,
        "guest_exit": guest_exit,
        "pre_recovery_fsck_exit": pre_recovery_fsck_exit,
        "pre_recovery_disk_exit": pre_recovery_disk_exit,
        "recovery_exit": recovery_exit,
        "fsck_exit": fsck_exit,
        "disk_exit": disk_exit,
        "fault_hit_count": len(hit_rows),
    }
    save(evidence, "persistence.json", persistence)
    save(evidence, "control-result.json", {
        "schema": 1, "classification": "matched_control",
        "fault_hit_count": len(hit_rows), "pid": pid,
        "syscall_result": workload.get("syscall_result"),
        "syscall_errno": workload.get("syscall_errno"),
    })
    save(evidence, "run-meta.json", {
        "fixture": "fault-formula-single", "workers": 1,
        "control_mode": True, "guest_exit": guest_exit,
        "fsck_exit": fsck_exit, "disk_exit": disk_exit,
        "pre_recovery_fsck_exit": pre_recovery_fsck_exit,
        "pre_recovery_disk_exit": pre_recovery_disk_exit,
        "recovery_exit": recovery_exit, "trace_loss": 0,
        "pid": pid,
    })
    return 0


def inline_formula_observation(target: Any) -> dict[str, Any]:
    """Evaluate R-INLINE from the fixture's independent post-action reads.

    The kernel event only identifies the failed conversion boundary.  The
    postcondition is observed from FS_IOC_GETFLAGS and FIEMAP, so the raw
    counter event is kept as the source anchor while this function performs
    the actual formula comparison.
    """
    if not isinstance(target, dict):
        return {
            "comparison_valid": False,
            "oracle_state": "unverified",
            "actual": None,
            "expected": 1,
            "reason": "target_state_missing",
        }
    required = ("exists", "stat_ok", "inline_data_valid", "inline_data",
                "block0_mapped", "block0_initialized")
    if any(not isinstance(target.get(field), bool) for field in required):
        return {
            "comparison_valid": False,
            "oracle_state": "unverified",
            "actual": None,
            "expected": 1,
            "reason": "inline_observation_invalid",
        }
    if not target["exists"] or not target["stat_ok"] or not target["inline_data_valid"]:
        return {
            "comparison_valid": False,
            "oracle_state": "unverified",
            "actual": None,
            "expected": 1,
            "reason": "inline_observation_unavailable",
        }
    actual = int(target["inline_data"] or
                 (target["block0_mapped"] and target["block0_initialized"]))
    return {
        "comparison_valid": True,
        "oracle_state": "consistent" if actual == 1 else "mismatch_reproduced",
        "actual": actual,
        "expected": 1,
        "reason": "inline_or_initialized_block0",
    }


def read_rows(path: pathlib.Path) -> list[dict[str, Any]]:
    rows = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return rows
    for number, line in enumerate(lines, 1):
        match = PREFIX.search(line.strip())
        if not match:
            continue
        body = match["body"]
        rows.append({"line": number, "pid": int(match["pid"]),
                     "comm": match["comm"].strip(), "body": body,
                     "fields": dict(KEY.findall(body))})
    return rows


def collect(project: pathlib.Path, run: pathlib.Path, guest_exit: int,
            fsck_exit: int, disk_exit: int, pre_recovery_fsck_exit: int,
            pre_recovery_disk_exit: int, recovery_exit: int) -> int:
    evidence = run / "evidence"
    raw = run / "fault-formula"
    evidence.mkdir(parents=True, exist_ok=True)
    plan = load(evidence / "fault-plan.json", {}) or {}
    workload = load(raw / "workload-result.json", {}) or {}
    before_status = json_object(workload.get("status_before"))
    after_status = json_object(workload.get("status_after"))
    pid = integer(workload.get("pid"))

    arm = {
        "schema": 2,
        "status": "armed" if workload.get("arm_verified") is True else "failed",
        "enabled": before_status.get("enabled"),
        "operation": plan.get("operation"),
        "errno": plan.get("errno", -5),
        "errno_choices": before_status.get("errno_choices"),
        "errno_choice_index": integer(before_status.get("errno_choice_index")),
        "count": plan.get("count", 1),
        "program": plan.get("program"),
        "call_index": plan.get("call_index"),
        "generation": integer(before_status.get("generation")),
        "target_pid": integer(before_status.get("target_pid", pid)),
        "remaining": integer(before_status.get("remaining")),
        "injected": integer(before_status.get("injected")),
        "matches": integer(before_status.get("matches")),
    }
    save(evidence, "arm.json", arm)

    trace_path = raw / "trace.txt"
    rows = read_rows(trace_path)
    hit_rows = [row for row in rows
                if "ext4_fault_inject:" in row["body"] and
                row["fields"].get("operation") == plan.get("operation")]
    hits = []
    for row in hit_rows:
        fields = row["fields"]
        item = {"operation": fields.get("operation"),
                "generation": integer(fields.get("generation")),
                "injection_count": integer(fields.get("injection",
                                                         fields.get("injection_count"))),
                "pid": integer(fields.get("pid")),
                "target_pid": integer(fields.get("target_pid")),
                "program": fields.get("program"),
                "target_program": fields.get("target_program"),
                "call_index": integer(fields.get("call_index")),
                "match_index": integer(fields.get("match_index")),
                "errno": integer(fields.get("errno")),
                "errno_choice_index": integer(fields.get("errno_choice_index")),
                "errno_choice_draw": integer(fields.get("errno_choice_draw")),
                "result": integer(fields.get("result")),
                "group": integer(fields.get("group")),
                "block": integer(fields.get("block")),
                "len": integer(fields.get("len")),
                "trace_line": row["line"]}
        hits.append(item)
    save(evidence, "hits.json", hits)

    target = workload.get("target_before", {})
    target_after = workload.get("target_after", {})
    target_ino = integer(target.get("ino")) if isinstance(target, dict) else None
    if not target_ino:
        for key in ("source_before", "destination_before"):
            candidate = workload.get(key, {})
            if isinstance(candidate, dict) and integer(candidate.get("ino")):
                target_ino = integer(candidate.get("ino"))
                break
    allowed_formulas = set(plan.get("formulas") or [])
    target_xattr_block = integer(workload.get("xattr_block"))
    terminal_points = plan.get("oracle_points") or {
        "R-IEVICT": ["INODE.EVICT.TARGET.EXIT"],
        "R-IDELTA-EVICT": ["INODE.EVICT.DESCRIPTOR.EXIT",
                           "INODE.EVICT.GLOBAL.EXIT"],
    }
    formula_events = []
    pid = integer(workload.get("pid"))
    call_begins = [row for row in rows if row["pid"] == pid and
                   "CT_SYSCALL" in row["body"] and
                   row["fields"].get("phase") == "BEGIN"]
    call_ends = [row for row in rows if row["pid"] == pid and
                 "CT_SYSCALL" in row["body"] and
                 row["fields"].get("phase") == "END"]
    action_window = ((call_begins[0]["line"], call_ends[0]["line"])
                     if len(call_begins) == 1 and len(call_ends) == 1 else None)
    oracle_begins = [row for row in rows if row["pid"] == pid and
                     "CT_ORACLE" in row["body"] and
                     row["fields"].get("phase") == "BEGIN"]
    oracle_ends = [row for row in rows if row["pid"] == pid and
                   "CT_ORACLE" in row["body"] and
                   row["fields"].get("phase") == "END"]
    oracle_window = ((oracle_begins[0]["line"], oracle_ends[0]["line"])
                     if len(oracle_begins) == 1 and len(oracle_ends) == 1
                     else action_window)
    for row in rows:
        in_action = (action_window is not None and
                     action_window[0] < row["line"] < action_window[1])
        in_oracle = (oracle_window is not None and
                     oracle_window[0] < row["line"] < oracle_window[1])
        if pid is None or row["pid"] != pid or not (in_action or in_oracle):
            continue
        if "ext4_counter_event:" not in row["body"]:
            continue
        fields = dict(KEY.findall(row["body"].split("ext4_counter_event:", 1)[1]))
        formula = fields.get("formula")
        if formula not in allowed_formulas:
            continue
        # Runtime delta formulas are validated from the stable counter
        # snapshots and bitmap-transition events below. Their raw trace rows
        # are evidence inputs, not completed comparisons on their own.
        if formula in ("R-BDELTA", "R-IDELTA"):
            continue
        # R-INLINE is evaluated from the fixture's independent GETFLAGS and
        # FIEMAP reads below.  The kernel event is only its failure-boundary
        # anchor and therefore must not be treated as a completed comparison.
        if formula == "R-INLINE":
            continue
        point = fields.get("point")
        relevant_points = terminal_points.get(formula)
        # An empty point list in a catalog plan means that the source-specific
        # terminal point is not restricted yet.  It must not suppress valid
        # post-hit events: several hooks emit their only independent
        # comparison at a hook-specific point that is discovered from the
        # trace.  A non-empty list remains an explicit allow-list.
        terminal_match = (relevant_points is not None and
                          bool(relevant_points) and point in relevant_points)
        oracle_relevant = (relevant_points is None or
                           not relevant_points or terminal_match)
        if not oracle_relevant:
            continue
        if row["line"] <= max((hit["line"] for hit in hit_rows), default=0):
            # Events preceding the injected failure describe fixture setup,
            # not the target operation's post-fault oracle state.
            continue
        event_ino = integer(fields.get("ino"))
        event_block = integer(fields.get("block"))
        # Shared-xattr ownership is tracked by the external block because its
        # surviving reference belongs to the fixture's other inode.
        xattr_block_match = (formula and formula.startswith("XATTR-REF") and
                             target_xattr_block and event_block == target_xattr_block)
        # Some hooks operate on a referenced metadata inode rather than the
        # userspace target inode.  An explicitly planned terminal point inside
        # this syscall window is the source-level attribution for that object
        # (for example EA-REF-DELTA on the EA inode during setxattr).
        if (target_ino and event_ino not in (target_ino, 0) and
                not xattr_block_match and not terminal_match):
            continue
        status = integer(fields.get("status"))
        comparison_valid = integer(fields.get("comparison_valid")) == 1
        actual, expected = integer(fields.get("actual")), integer(fields.get("expected"))
        state = {0: "raw", 1: "consistent", 2: "mismatch_reproduced",
                 3: "unverified"}.get(status, "unverified")
        if status in (1, 2) and not comparison_valid:
            state = "unverified"
        elif status == 1 and actual != expected:
            state = "unverified"
        elif status == 2 and actual == expected:
            state = "unverified"
        # Status 0 is an intermediate transition sample.  Preserve it in the
        # evidence stream for source context, but do not let it participate in
        # the formula oracle: it is neither a comparison nor an explicit
        # unverified terminal result.
        event_oracle_relevant = oracle_relevant and status != 0
        formula_events.append({
            "formula": formula, "point": point,
            "oracle_state": state, "status": status,
            "comparison_valid": comparison_valid,
            "oracle_relevant": event_oracle_relevant,
            "actual": actual,
            "expected": expected,
            "delta": integer(fields.get("delta")),
            "update_delta": integer(fields.get("update_delta")),
            "reason": fields.get("reason"), "ino": event_ino,
            "block": event_block,
            "group": integer(fields.get("group")),
            "op": integer(fields.get("op")),
            "seq": integer(fields.get("seq")), "pid": row["pid"],
            "trace_line": row["line"],
        })

    # Every catalog hook gets an explicit formula evidence record even when
    # its source-specific terminal event has not been implemented yet.  The
    # record is anchored to the exact kernel hit and remains unverified; it
    # proves attribution without pretending that a formula comparison exists.
    if str(plan.get("oracle", "")).startswith("catalog_hook_") and hit_rows and allowed_formulas:
        hit = hit_rows[0]
        observed_formula_names = {event.get("formula") for event in formula_events}
        for formula in sorted(allowed_formulas - observed_formula_names):
            formula_events.append({
                "formula": formula,
                "point": "FAULT.HOOK.HIT",
                "oracle_state": "unverified",
                "status": 3,
                "comparison_valid": False,
                # This event attributes the injected hook only. It is not an
                # evaluation of the mapped formula and must not conflict with
                # a later source-backed or stable aggregate comparison.
                "oracle_relevant": False,
                "actual": None,
                "expected": None,
                "delta": None,
                "update_delta": None,
                "reason": "hook_hit_without_source_specific_terminal_oracle",
                "ino": None,
                "block": None,
                "group": None,
                "op": None,
                "seq": None,
                "pid": hit["pid"],
                "trace_line": hit["line"],
                "evidence_kind": "fault_hit_only",
            })

    if "R-INLINE" in allowed_formulas:
        inline_points = set(terminal_points.get(
            "R-INLINE", ["FAULT.INLINE.DATA.BLOCK.WRITE_BEGIN"]))
        observation = inline_formula_observation(target_after)
        for row in rows:
            in_action = (action_window is not None and
                         action_window[0] < row["line"] < action_window[1])
            in_oracle = (oracle_window is not None and
                         oracle_window[0] < row["line"] < oracle_window[1])
            if (pid is None or row["pid"] != pid or not (in_action or in_oracle) or
                    "ext4_counter_event:" not in row["body"] or
                    row["line"] <= max((hit["line"] for hit in hit_rows), default=0)):
                continue
            fields = dict(KEY.findall(row["body"].split(
                "ext4_counter_event:", 1)[1]))
            if (fields.get("formula") != "R-INLINE" or
                    fields.get("point") not in inline_points):
                continue
            formula_events.append({
                "formula": "R-INLINE",
                "point": fields.get("point"),
                "oracle_state": observation["oracle_state"],
                "status": 1 if observation["oracle_state"] == "consistent" else
                          2 if observation["oracle_state"] == "mismatch_reproduced" else 3,
                "comparison_valid": observation["comparison_valid"],
                "oracle_relevant": True,
                "actual": observation["actual"],
                "expected": observation["expected"],
                "delta": (observation["actual"] - observation["expected"]
                          if observation["actual"] is not None else None),
                "update_delta": None,
                "reason": observation["reason"],
                "ino": integer(fields.get("ino")),
                "block": integer(fields.get("block")),
                "group": integer(fields.get("group")),
                "op": integer(fields.get("op")),
                "seq": integer(fields.get("seq")),
                "pid": row["pid"],
                "trace_line": row["line"],
                "evidence_kind": "fixture_inline_formula",
                "observation": {
                    key: target_after.get(key) if isinstance(target_after, dict)
                    else None
                    for key in ("exists", "stat_ok", "inline_data_valid",
                                "inline_data", "block0_mapped",
                                "block0_initialized", "block0_flags")
                },
            })

    delta_checks = []
    delta_sources = {
        "R-BDELTA": {"BLOCK.BITMAP.TRANSITION"},
        "R-IDELTA": {"INODE.ALLOC.BITMAP", "INODE.FREE.BITMAP"},
    }
    before_snapshot = load(raw / "stable-before.json", {}) or {}
    oracle_snapshot = load(raw / "stable-oracle.json", {}) or {}

    def stable_counter_snapshot(snapshot):
        return (snapshot.get("valid") is True and
                snapshot.get("relations_valid") is True and
                snapshot.get("observation_stable") is True and
                snapshot.get("trace_loss", 0) == 0 and
                snapshot.get("workers") == 1)

    for formula, points in delta_sources.items():
        if formula not in allowed_formulas:
            continue
        counter = "free_clusters" if formula == "R-BDELTA" else "free_inodes"
        before_value = integer(before_snapshot.get(counter))
        after_value = integer(oracle_snapshot.get(counter))
        source_events = []
        if action_window is not None:
            for row in rows:
                if (row["pid"] != pid or
                        not action_window[0] < row["line"] < action_window[1] or
                        "ext4_counter_event:" not in row["body"]):
                    continue
                fields = dict(KEY.findall(
                    row["body"].split("ext4_counter_event:", 1)[1]))
                if fields.get("formula") != formula or fields.get("point") not in points:
                    continue
                delta = integer(fields.get("update_delta"))
                if delta is None:
                    continue
                source_events.append({"trace_line": row["line"],
                                      "point": fields.get("point"),
                                      "update_delta": delta,
                                      "ino": integer(fields.get("ino")),
                                      "group": integer(fields.get("group"))})
        actual = (after_value - before_value
                  if before_value is not None and after_value is not None else None)
        expected = sum(item["update_delta"] for item in source_events)
        comparison_valid = (actual is not None and
                            stable_counter_snapshot(before_snapshot) and
                            stable_counter_snapshot(oracle_snapshot))
        state = ("consistent" if actual == expected else "mismatch_reproduced") \
            if comparison_valid else "unverified"
        check = {
            "formula": formula,
            "point": "FIXTURE.STABLE.DELTA",
            "counter": counter,
            "before": before_value,
            "after": after_value,
            "actual": actual,
            "expected": expected,
            "source_events": source_events,
            "source_trace_lines": [item["trace_line"] for item in source_events],
            "comparison_valid": comparison_valid,
            "oracle_state": state,
            "stable_before_snapshot_id": integer(before_snapshot.get("snapshot_id")),
            "stable_oracle_snapshot_id": integer(oracle_snapshot.get("snapshot_id")),
        }
        delta_checks.append(check)
        formula_events.append({
            "formula": formula,
            "point": "FIXTURE.STABLE.DELTA",
            "oracle_state": state,
            "status": 1 if state == "consistent" else 2 if state == "mismatch_reproduced" else 3,
            "comparison_valid": comparison_valid,
            "oracle_relevant": True,
            "actual": actual,
            "expected": expected,
            "delta": actual - expected if actual is not None else None,
            "update_delta": expected,
            "reason": "stable_counter_delta_vs_bitmap_transitions",
            "ino": target_ino,
            "block": None,
            "group": None,
            "op": None,
            "seq": None,
            "pid": pid,
            "evidence_kind": "stable_delta_aggregate",
            "source_trace_lines": check["source_trace_lines"],
            "trace_line": None,
        })
    save(evidence, "stable-delta-checks.json", delta_checks)
    save(evidence, "formula-events.json", formula_events)

    expected_return = plan.get("expected_return") or {}
    syscall_rc = integer(workload.get("syscall_result", workload.get("syscall_rc")))
    syscall_errno = integer(workload.get("syscall_errno", workload.get("action_errno", 0)))
    return_matches = (syscall_rc == expected_return.get("result") and
                      syscall_errno == expected_return.get("errno"))
    # The selected helper has a native error return, while its caller can
    # consume it and retry/fall back.  record_only deliberately verifies the
    # hook trace and formula events without inventing a syscall errno.
    return_verified = bool(workload.get("call_performed") and
                           (plan.get("return_policy") == "record_only" or
                            return_matches))
    workload.update({
        "schema": 2,
        "pid": pid,
        "arm_verified": workload.get("arm_verified") is True,
        "status_after": after_status,
        "syscall_rc": syscall_rc,
        "syscall_errno": syscall_errno,
        "return_verified": return_verified,
    })
    save(evidence, "workload-result.json", workload)

    losses = []
    for stats_path in sorted(raw.glob("cpu*-stats.txt")):
        losses.extend(int(value) for value in
                      LOSS.findall(stats_path.read_text(errors="replace")))
    trace_loss = sum(losses) if losses else 1
    snapshots = {}
    for name in ("stable-before.json", "stable-after.json", "stable-oracle.json",
                 "remount.json"):
        snapshot = load(raw / name, {}) or {}
        snapshot = dict(snapshot)
        snapshot["trace_loss"] = trace_loss
        snapshot["workers"] = 1
        snapshots[name] = snapshot
        save(evidence, name, snapshot)
        for suffix in ("a.json", "b.json"):
            pair_source = raw / f"{name}.{suffix}"
            if pair_source.is_file():
                shutil.copy2(pair_source, evidence / pair_source.name)

    association = build_association_index(plan, workload, rows, hit_rows,
                                          formula_events, snapshots)
    save(evidence, "association-index.json", association)

    disk = load(run / "disk.json", {}) or {}
    disk_before = load(run / "disk-before-recovery.json", {}) or {}
    remount = snapshots["remount.json"]
    counter_fields = ("free_clusters", "free_inodes", "dirs")
    recovery = recovery_contract(plan)
    remount_contract = recovery.get("pre_recovery", {}).get(
        "remount_counters", {}) if isinstance(recovery.get("pre_recovery"), dict) else {}
    remount_counter_state = (remount_contract.get("state", "must_match")
                             if isinstance(remount_contract, dict) else "must_match")
    if remount_counter_state == "must_match":
        matches_remount = all(
            remount.get(key) == (disk_before.get("descriptor_totals") or {}).get(key)
            for key in counter_fields)
    else:
        # A corrupt/unhealthy mount may expose partial or stale counters. Do
        # not compare those to the raw image; offline pre/post-repair checks
        # remain the persistence proof for this contract.
        matches_remount = None
    unmount_status_path = raw / "unmount-status.txt"
    unmount_status = (unmount_status_path.read_text(errors="replace").strip()
                      if unmount_status_path.is_file() else "missing")
    pre_recovery_fsck_path = run / "fsck-before-recovery.txt"
    pre_recovery_fsck = (pre_recovery_fsck_path.read_text(errors="replace")
                         if pre_recovery_fsck_path.is_file() else "")
    target_inode = integer(target.get("ino")) if isinstance(target, dict) else None
    pre_fsck_state = contract_state(recovery, "pre_recovery", "fsck", "unverified")
    pre_disk_state = contract_state(recovery, "pre_recovery", "disk", "unverified")
    repair = recovery.get("repair", {})
    repair_kind = str(repair.get("kind", "unverified")) if isinstance(repair, dict) else "unverified"
    post_fsck_state = contract_state(recovery, "post_recovery", "fsck", "unverified")
    post_disk_state = contract_state(recovery, "post_recovery", "disk", "unverified")
    pre_fsck_ok = (
        pre_fsck_state == "clean" and pre_recovery_fsck_exit == 0 or
        pre_fsck_state == "known_issue" and pre_recovery_fsck_exit != 0 and
        all(signature.replace("{target_inode}", str(target_inode)) in pre_recovery_fsck
            for signature in contract_signatures(recovery, "pre_recovery", "fsck")) or
        pre_fsck_state == "inspect" and
        ((pre_recovery_fsck_exit == 0 and
          not any(marker in pre_recovery_fsck for marker in
                  ("FILE SYSTEM WAS MODIFIED", "still has errors",
                   "UNEXPECTED INCONSISTENCY", "UNRECOVERED ERRORS"))) or
         (pre_recovery_fsck_exit in (1, 2, 4, 8) and
          bool(pre_recovery_fsck.strip()) and
          all(signature.replace("{target_inode}", str(target_inode))
              in pre_recovery_fsck
              for signature in contract_signatures(
                  recovery, "pre_recovery", "fsck")))))
    pre_disk_ok = (
        pre_disk_state == "verified" and pre_recovery_disk_exit == 0 and
        disk_before.get("valid") is True and disk_before.get("status") == "verified" or
        pre_disk_state == "mismatch" and pre_recovery_disk_exit != 0 and
        disk_before.get("valid") is False or
        pre_disk_state == "inspect" and
        ((pre_recovery_disk_exit == 0 and disk_before.get("valid") is True and
          disk_before.get("status") == "verified") or
         (pre_recovery_disk_exit != 0 and disk_before.get("valid") is False)))
    recovery_fsck_path = run / "fsck-recovery.txt"
    recovery_fsck_text = (recovery_fsck_path.read_text(errors="replace")
                          if recovery_fsck_path.is_file() else "")
    repair_exits = repair.get("allowed_exit", [0, 1]) if isinstance(repair, dict) else []
    repair_ok = (
        repair_kind == "none" and recovery_exit == 0 or
        repair_kind == "e2fsck-fy" and recovery_exit in repair_exits and
        all(signature in recovery_fsck_text
            for signature in contract_signatures(recovery, "repair", "command")))
    final_fsck_path = run / "fsck.txt"
    final_fsck_text = (final_fsck_path.read_text(errors="replace")
                       if final_fsck_path.is_file() else "")
    post_fsck_ok = (post_fsck_state == "clean" and fsck_exit == 0 and
                    bool(final_fsck_text.strip()) and not any(marker in final_fsck_text for marker in
                        ("FILE SYSTEM WAS MODIFIED", "still has errors",
                         "UNEXPECTED INCONSISTENCY", "UNRECOVERED ERRORS")))
    post_disk_ok = (post_disk_state == "verified" and disk_exit == 0 and
                    disk.get("valid") is True and disk.get("status") == "verified")
    recovery_verified = (recovery.get("status") in ("specified", "verified") and pre_fsck_ok and
                         pre_disk_ok and repair_ok and post_fsck_ok and post_disk_ok)
    expected_pre_recovery_issue = (
        pre_fsck_state == "known_issue" or pre_disk_state == "mismatch" or
        pre_fsck_state == "inspect" and pre_recovery_fsck_exit != 0 or
        pre_disk_state == "inspect" and pre_recovery_disk_exit != 0)
    persistence = {
        "checked": (guest_exit == 0 and recovery_verified and
                    unmount_status == "unmount_ok" and
                    (raw / "remount.json").is_file()),
        "valid": recovery_verified,
        "matches_remount": matches_remount,
        "fsck_exit": fsck_exit, "disk_exit": disk_exit,
        "pre_recovery_fsck_exit": pre_recovery_fsck_exit,
        "pre_recovery_disk_exit": pre_recovery_disk_exit,
        "pre_recovery_disk_valid": pre_disk_ok,
        "remount_counter_state": remount_counter_state,
        "recovery_exit": recovery_exit,
        "recovery_attempted": recovery_exit != 99,
        "recovery_contract_status": recovery.get("status", "unverified"),
        "pre_recovery_expected_issue": expected_pre_recovery_issue,
        "pre_recovery_fsck_contract_ok": pre_fsck_ok,
        "pre_recovery_disk_contract_ok": pre_disk_ok,
        "repair_contract_ok": repair_ok,
        "post_recovery_fsck_contract_ok": bool(post_fsck_ok),
        "post_recovery_disk_contract_ok": post_disk_ok,
        "pre_recovery_fsck_text": pre_recovery_fsck,
        "recovery_verified": recovery_verified,
        "unmount_status": unmount_status,
        "disk_status": disk.get("status"),
        "remount_counters": {key: remount.get(key) for key in counter_fields},
        "pre_recovery_disk_counters": disk_before.get("descriptor_totals", {}),
        "disk_counters": disk.get("descriptor_totals", {}),
    }
    before_totals = disk_before.get("descriptor_totals", {})
    after_totals = disk.get("descriptor_totals", {})
    persistence["recovery_deltas"] = {
        key: (integer(after_totals.get(key)) - integer(before_totals.get(key))
              if integer(after_totals.get(key)) is not None and
              integer(before_totals.get(key)) is not None else None)
        for key in ("free_clusters", "free_inodes", "dirs")}
    save(evidence, "persistence.json", persistence)

    for name in ("trace.txt", "cpu0-stats.txt", "unmount-status.txt",
                 "stable-oracle.json",
                 "run-status.txt", "workload.txt", "remount-target.json",
                 "remount-target.err"):
        source = raw / name
        if source.is_file():
            shutil.copy2(source, evidence / name)
    for source_name in ("baseline-fsck.txt", "fsck-before-recovery.txt",
                        "fsck-recovery.txt", "fsck.txt", "disk-before-recovery.json",
                        "disk.json", "image-integrity.json"):
        source = run / source_name
        if source.is_file():
            shutil.copy2(source, evidence / source_name)
    save(evidence, "run-meta.json", {
        "fixture": "fault-formula-single", "workers": 1,
        "guest_exit": guest_exit, "fsck_exit": fsck_exit,
        "disk_exit": disk_exit,
        "pre_recovery_fsck_exit": pre_recovery_fsck_exit,
        "pre_recovery_disk_exit": pre_recovery_disk_exit,
        "recovery_exit": recovery_exit, "trace_loss": trace_loss,
        "status_before": before_status, "status_after": after_status,
        "target_inode": target_ino,
    })
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=pathlib.Path)
    parser.add_argument("run", type=pathlib.Path)
    parser.add_argument("--guest-exit", required=True, type=int)
    parser.add_argument("--fsck-exit", required=True, type=int)
    parser.add_argument("--disk-exit", required=True, type=int)
    parser.add_argument("--pre-recovery-fsck-exit", required=True, type=int)
    parser.add_argument("--pre-recovery-disk-exit", required=True, type=int)
    parser.add_argument("--recovery-exit", required=True, type=int)
    parser.add_argument("--control", action="store_true",
                        help="collect a matched no-injection control")
    args = parser.parse_args()
    if args.control:
        return collect_control(args.project.resolve(), args.run.resolve(),
                               args.guest_exit, args.fsck_exit, args.disk_exit,
                               args.pre_recovery_fsck_exit,
                               args.pre_recovery_disk_exit, args.recovery_exit)
    return collect(args.project.resolve(), args.run.resolve(), args.guest_exit,
                   args.fsck_exit, args.disk_exit,
                   args.pre_recovery_fsck_exit, args.pre_recovery_disk_exit,
                   args.recovery_exit)


if __name__ == "__main__":
    raise SystemExit(main())
