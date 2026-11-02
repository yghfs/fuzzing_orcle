#!/usr/bin/env python3
"""Validate one deterministic single-worker fault-injection experiment.

The validator consumes an immutable evidence directory.  A successful result
requires a matching arm record, an observed kernel injection, the expected
syscall return, two valid stable counter snapshots, and an offline persistence
check.  It never treats a missing or incomplete stage as a clean rollback.
"""
from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import re
from typing import Any

from fault_errno_catalog import errno_codes


SCHEMA = 1
STAGES = ("arm", "hit", "return", "stable", "rollback", "persistence")
KEY = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>[^\s]+)")
TRACE_PREFIX = re.compile(
    r"(?P<comm>.+)-(?P<pid>\d+)\s+\[(?P<cpu>\d+)\]\s+\S+\s+"
    r"(?P<time>\d+\.\d+):\s*(?P<body>.*)"
)
SYSCALL_ENTER = re.compile(r"(?:raw_syscalls:)?sys_enter:\s+NR\s+(-?\d+)\s+\(([^)]*)\)")
SYSCALL_EXIT = re.compile(r"(?:raw_syscalls:)?sys_exit:\s+NR\s+(-?\d+)\s+=\s+(-?\d+)")


def _json(path: pathlib.Path) -> Any:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def _first(directory: pathlib.Path, *names: str) -> pathlib.Path | None:
    for name in names:
        path = directory / name
        if path.is_file():
            return path
    return None


def _number(value: Any, name: str, issues: list[str], integer: bool = True):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        issues.append(f"{name}:not_numeric")
        return None
    if integer and not isinstance(value, int):
        issues.append(f"{name}:not_integer")
        return None
    return value


def _required_object(path: pathlib.Path | None, name: str,
                     issues: list[str]) -> dict[str, Any] | None:
    if path is None:
        issues.append(f"{name}:missing")
        return None
    try:
        value = _json(path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        issues.append(f"{name}:invalid_json:{error}")
        return None
    if not isinstance(value, dict):
        issues.append(f"{name}:not_object")
        return None
    return value


def _matches(record: dict[str, Any], plan: dict[str, Any], fields: tuple[str, ...],
             label: str, issues: list[str]) -> None:
    for field in fields:
        if field not in record:
            issues.append(f"{label}:missing_{field}")
        elif field in plan and record[field] != plan[field]:
            issues.append(f"{label}:{field}_mismatch")


def _trace_fields(directory: pathlib.Path) -> tuple[list[dict[str, str]], list[dict[str, str]],
                                                       list[dict[str, str]]]:
    """Return arm, kernel-hit, and syscall-end records from optional trace text."""
    path = _first(directory, "trace.txt", "trace.txt.gz")
    if path is None:
        return [], [], []
    opener = gzip.open if path.suffix == ".gz" else open
    arms: list[dict[str, str]] = []
    hits: list[dict[str, str]] = []
    returns: list[dict[str, str]] = []
    with opener(path, "rt", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            body = line.split(": ", 1)[-1].strip()
            values = {match["key"]: match["value"] for match in KEY.finditer(body)}
            if "CT_FAULT_ARM" in body and values.get("status") == "armed":
                arms.append(values)
            elif "ext4_fault_inject:" in body:
                hits.append(values)
            elif "CT_SYSCALL" in body and values.get("phase") == "END":
                returns.append(values)
    return arms, hits, returns


def audit_trace_file(path: pathlib.Path, plan: dict[str, Any], arm: dict[str, Any],
                     target_pid: int | None) -> dict[str, Any]:
    """Prove the armed fault, kernel hit, and native syscall belong to one fixture call."""
    audit: dict[str, Any] = {
        "issues": [], "hits": [], "workload_pid": target_pid,
        "marker_id": None, "arm_line": None, "marker_begin_line": None,
        "native_enter_line": None, "kernel_hit_line": None,
        "native_exit_line": None, "marker_end_line": None,
        "native_raw_result": None, "native_syscall_nr": plan.get("syscall_nr"),
        "native_verified": False, "marker_verified": False,
    }
    path = pathlib.Path(path)
    if not path.is_file():
        audit["issues"].append("return:native_trace_missing")
        return audit
    if isinstance(target_pid, bool) or not isinstance(target_pid, int) or target_pid <= 0:
        audit["issues"].append("return:workload_pid_missing")
        return audit
    try:
        expected_errno = int(plan["expected_return"]["errno"])
    except (KeyError, TypeError, ValueError):
        expected_errno = None

    lines: list[dict[str, Any]] = []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as stream:
        for number, raw in enumerate(stream, 1):
            match = TRACE_PREFIX.search(raw.strip())
            if not match:
                continue
            body = match["body"]
            values = {item["key"]: item["value"] for item in KEY.finditer(body)}
            lines.append({"line": number, "pid": int(match["pid"]),
                          "body": body, "fields": values})

    def marker_rows(kind: str, phase: str | None = None) -> list[dict[str, Any]]:
        token = f"tracing_mark_write: {kind} "
        return [line for line in lines if token in line["body"] and
                (phase is None or line["fields"].get("phase") == phase)]

    arm_rows = [row for row in marker_rows("CT_FAULT_ARM", "ARM_END")
                if row["pid"] == target_pid]
    if len(arm_rows) != 1:
        audit["issues"].append("arm:trace_arm_marker_count_mismatch")
    else:
        arm_row = arm_rows[0]
        audit["arm_line"] = arm_row["line"]
        fields = arm_row["fields"]
        if fields.get("status") != "armed":
            audit["issues"].append("arm:trace_arm_not_armed")
        for key, expected in (("operation", plan.get("operation")),
                              ("program", plan.get("program")),
                              ("call_index", plan.get("call_index")),
                              ("errno", plan.get("errno")),
                              ("pid", target_pid)):
            if fields.get(key) != str(expected):
                audit["issues"].append(f"arm:trace_{key}_mismatch")

    expected_nr = plan.get("syscall_nr")
    try:
        expected_nr = int(expected_nr)
    except (TypeError, ValueError):
        expected_nr = None
    call_begins = [row for row in marker_rows("CT_SYSCALL", "BEGIN")
                   if row["pid"] == target_pid and
                   row["fields"].get("program") == plan.get("program") and
                   row["fields"].get("call_index") == str(plan.get("call_index")) and
                   row["fields"].get("nr") == str(expected_nr)]
    if len(call_begins) != 1:
        audit["issues"].append("return:syscall_begin_marker_count_mismatch")
        begin = None
    else:
        begin = call_begins[0]
        audit["marker_begin_line"] = begin["line"]
        audit["marker_id"] = begin["fields"].get("id")
    call_ends = [row for row in marker_rows("CT_SYSCALL", "END")
                 if row["pid"] == target_pid and
                 row["fields"].get("program") == plan.get("program") and
                 row["fields"].get("call_index") == str(plan.get("call_index")) and
                 row["fields"].get("nr") == str(expected_nr)]
    if len(call_ends) != 1:
        audit["issues"].append("return:syscall_end_marker_count_mismatch")
        end = None
    else:
        end = call_ends[0]
        audit["marker_end_line"] = end["line"]

    if begin is not None and end is not None:
        begin_fields, end_fields = begin["fields"], end["fields"]
        marker_id = begin_fields.get("id")
        if not marker_id or end_fields.get("id") != marker_id:
            audit["issues"].append("return:syscall_marker_id_mismatch")
        if begin["line"] >= end["line"]:
            audit["issues"].append("return:syscall_marker_order_invalid")
        if begin["line"] <= (audit["arm_line"] or 0):
            audit["issues"].append("return:arm_not_before_syscall")
        try:
            expected_result = int(plan["expected_return"]["result"])
        except (KeyError, TypeError, ValueError):
            expected_result = None
        if end_fields.get("ret") != str(expected_result) or \
                end_fields.get("errno") != str(expected_errno):
            audit["issues"].append("return:syscall_end_marker_result_mismatch")
        audit["marker_verified"] = not any(
            issue.startswith("return:syscall_") or issue == "return:arm_not_before_syscall"
            for issue in audit["issues"]
        )

    pairs: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    for row in lines:
        if row["pid"] != target_pid:
            continue
        enter = SYSCALL_ENTER.search(row["body"])
        exit_ = SYSCALL_EXIT.search(row["body"])
        if enter:
            if pending is not None:
                pending = None
            pending = {"line": row["line"], "nr": int(enter.group(1))}
        elif exit_:
            if pending is not None and pending["nr"] == int(exit_.group(1)):
                pairs.append({"enter_line": pending["line"], "exit_line": row["line"],
                              "nr": pending["nr"], "result": int(exit_.group(2))})
            pending = None

    selected_pairs = []
    if begin is not None and end is not None:
        selected_pairs = [pair for pair in pairs
                          if pair["nr"] == expected_nr and
                          begin["line"] < pair["enter_line"] < pair["exit_line"] < end["line"]]
    if len(selected_pairs) != 1:
        audit["issues"].append("return:native_syscall_pair_count_mismatch")
        native_pair = None
    else:
        native_pair = selected_pairs[0]
        audit["native_enter_line"] = native_pair["enter_line"]
        audit["native_exit_line"] = native_pair["exit_line"]
        audit["native_raw_result"] = native_pair["result"]
        if expected_errno is None or native_pair["result"] != -expected_errno:
            audit["issues"].append("return:native_syscall_result_mismatch")

    hit_rows = [row for row in lines if "ext4_fault_inject:" in row["body"] and
                row["pid"] == target_pid and
                row["fields"].get("operation") == plan.get("operation")]
    for row in hit_rows:
        values = row["fields"]
        hit = {
            "operation": values.get("operation"),
            "errno": _as_int(values.get("errno", values.get("error"))),
            "errno_choice_index": _as_int(values.get("errno_choice_index")),
            "errno_choice_draw": _as_int(values.get("errno_choice_draw")),
            # The kernel event has no program string. Its program label is
            # inherited from the only plan-matching CT_SYSCALL envelope after
            # PID and native-event ordering have been proven below.
            "program": plan.get("program"),
            "call_index": _as_int(values.get("call_index")),
            "generation": _as_int(values.get("generation")),
            "injection_count": _as_int(values.get("injection", values.get("injection_count"))),
            "pid": row["pid"],
            "target_pid": _as_int(values.get("target_pid")),
            "group": _as_int(values.get("group")),
            "block": _as_int(values.get("block")),
            "len": _as_int(values.get("len")),
            "result": _as_int(values.get("result")),
            "trace_line": row["line"],
        }
        audit["hits"].append(hit)
    if len(audit["hits"]) != plan.get("count"):
        audit["issues"].append("hit:trace_hit_count_mismatch")
    for hit in audit["hits"]:
        for key, expected in (("errno", plan.get("errno")),
                              ("program", plan.get("program")),
                              ("call_index", plan.get("call_index")),
                              ("pid", target_pid), ("target_pid", target_pid),
                              ("generation", arm.get("generation"))):
            if hit.get(key) != expected:
                audit["issues"].append(f"hit:trace_{key}_mismatch")
        if hit.get("injection_count") is None or not 1 <= hit["injection_count"] <= plan.get("count", 1):
            audit["issues"].append("hit:trace_injection_count_invalid")
        if hit.get("result") != plan.get("errno"):
            audit["issues"].append("hit:trace_result_mismatch")
        if hit.get("errno_choice_index") != 0 or hit.get("errno_choice_draw") != 0:
            audit["issues"].append("hit:trace_errno_choice_mismatch")
        if hit.get("errno") not in [-code for code in errno_codes(plan.get("operation", ""))]:
            audit["issues"].append("hit:trace_errno_unsupported")
        if native_pair is None or not (
                native_pair["enter_line"] < hit["trace_line"] < native_pair["exit_line"]):
            audit["issues"].append("hit:outside_native_syscall")
        else:
            audit["kernel_hit_line"] = hit["trace_line"]

    audit["native_verified"] = (native_pair is not None and not any(
        issue.startswith("return:native_") for issue in audit["issues"]))
    return audit


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def validate(directory: pathlib.Path) -> dict[str, Any]:
    """Validate evidence and return a machine-readable result."""
    directory = pathlib.Path(directory).resolve()
    issues: list[str] = []
    plan = _required_object(_first(directory, "fault-plan.json"), "plan", issues) or {}
    if plan.get("schema") != SCHEMA:
        issues.append("plan:schema_mismatch")
    for field in ("fault_id", "operation", "errno", "count", "program", "call_index",
                  "expected_return", "syscall_nr", "rollback_fields"):
        if field not in plan:
            issues.append("plan:missing_" + field)
    plan_errno = _as_int(plan.get("errno"))
    if plan_errno not in [-code for code in errno_codes(plan.get("operation", ""))]:
        issues.append("plan:errno_unsupported_for_operation")
    count = _number(plan.get("count"), "plan.count", issues)
    if count is not None and count < 1:
        issues.append("plan:count_not_positive")
    call_index = _number(plan.get("call_index"), "plan.call_index", issues)
    if call_index is not None and call_index < 0:
        issues.append("plan:call_index_negative")
    expected = plan.get("expected_return")
    if not isinstance(expected, dict):
        issues.append("plan:expected_return_not_object")
        expected = {}
    for field in ("result", "errno"):
        if field not in expected:
            issues.append("plan:expected_return_missing_" + field)

    arm = _required_object(_first(directory, "arm.json"), "arm", issues) or {}
    _matches(arm, plan, ("operation", "errno", "count", "program", "call_index"),
             "arm", issues)
    if arm.get("status") != "armed":
        issues.append("arm:status_not_armed")
    if arm.get("enabled") is not True:
        issues.append("arm:not_enabled")
    if arm.get("remaining") != count:
        issues.append("arm:remaining_mismatch")
    if arm.get("injected") != 0:
        issues.append("arm:already_injected")
    if "generation" not in arm:
        issues.append("arm:missing_generation")
    elif _number(arm["generation"], "arm.generation", issues) is not None and arm["generation"] < 1:
        issues.append("arm:generation_not_positive")
    target_pid = _number(arm.get("target_pid"), "arm.target_pid", issues)
    if target_pid is not None and target_pid <= 0:
        issues.append("arm:target_pid_not_positive")
    for field in ("program", "call_index"):
        if arm.get(field) != plan.get(field):
            issues.append(f"arm:{field}_mismatch")

    workload = _required_object(_first(directory, "workload-result.json"),
                                "workload", issues) or {}
    if workload.get("pid") != target_pid:
        issues.append("arm:workload_pid_mismatch")
    if workload.get("arm_verified") is not True:
        issues.append("arm:workload_arm_readback_failed")
    if workload.get("call_performed") is not True:
        issues.append("return:target_syscall_not_performed")
    if workload.get("disable_rc") != 0:
        issues.append("hit:fault_disable_failed")
    raw_status: dict[str, dict[str, Any]] = {}
    for label, field in (("before", "status_before"), ("after", "status_after")):
        try:
            value = json.loads(workload[field])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict):
            issues.append(f"{label == 'before' and 'arm' or 'hit'}:status_readback_invalid")
            value = {}
        raw_status[label] = value
    for field in ("enabled", "operation", "errno", "errno_choices", "errno_choice_index",
                  "remaining", "injected", "generation", "target_pid", "program",
                  "call_index"):
        if raw_status["before"].get(field) != arm.get(field):
            issues.append(f"arm:status_{field}_disagrees")
    expected_after = {
        "enabled": True, "operation": plan.get("operation"),
        "errno": plan.get("errno"), "remaining": 0,
        "errno_choices": str(plan.get("errno")), "errno_choice_index": 0,
        "injected": count, "generation": arm.get("generation"),
        "target_pid": target_pid, "program": plan.get("program"),
        "call_index": plan.get("call_index"),
    }
    for field, expected_value in expected_after.items():
        if raw_status["after"].get(field) != expected_value:
            issues.append(f"hit:status_after_{field}_mismatch")

    hit_path = _first(directory, "hits.json", "hit.json")
    hits: list[dict[str, Any]] = []
    if hit_path is None:
        issues.append("hit:missing")
    else:
        try:
            raw_hits = _json(hit_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            issues.append(f"hit:invalid_json:{error}")
            raw_hits = []
        if hit_path.name == "hit.json" and isinstance(raw_hits, dict):
            raw_hits = [raw_hits]
        if not isinstance(raw_hits, list):
            issues.append("hit:not_array")
            raw_hits = []
        hits = [row for row in raw_hits if isinstance(row, dict)]
        if len(hits) != len(raw_hits):
            issues.append("hit:non_object_record")
    if count is not None and len(hits) != count:
        issues.append("hit:count_mismatch")
    generations: set[int] = set()
    for index, hit in enumerate(hits):
        label = f"hit[{index}]"
        _matches(hit, plan, ("operation", "errno", "program", "call_index"), label, issues)
        for field in ("generation", "injection_count"):
            if field not in hit:
                issues.append(f"{label}:missing_{field}")
            elif _number(hit[field], f"{label}.{field}", issues) is not None:
                if field == "generation":
                    generations.add(hit[field])
                elif not 1 <= hit[field] <= (count or 1):
                    issues.append(f"{label}:injection_count_out_of_range")
        if arm.get("generation") is not None and hit.get("generation") != arm.get("generation"):
            issues.append(f"{label}:generation_mismatch")
        if arm.get("target_pid") not in (None, 0) and hit.get("pid") != arm.get("target_pid"):
            issues.append(f"{label}:target_pid_mismatch")
    if len(generations) > 1:
        issues.append("hit:multiple_generations")

    ret = _required_object(_first(directory, "return.json"), "return", issues) or {}
    _matches(ret, plan, ("program", "call_index"), "return", issues)
    result = ret.get("result", ret.get("ret"))
    if result != expected.get("result"):
        issues.append("return:result_mismatch")
    if ret.get("errno") != expected.get("errno"):
        issues.append("return:errno_mismatch")
    if ret.get("verified") is not True:
        issues.append("return:not_verified")
    if workload.get("syscall_rc") != expected.get("result") or \
            workload.get("syscall_errno") != expected.get("errno") or \
            workload.get("return_matches_expected") is not True:
        issues.append("return:workload_result_mismatch")
    if ret.get("native_verified") is not True:
        issues.append("return:native_not_verified")
    trace_path = _first(directory, "trace.txt", "trace.txt.gz")
    if trace_path is None:
        issues.append("return:native_trace_missing")
        trace_audit = {"issues": ["return:native_trace_missing"], "hits": [],
                       "native_verified": False, "marker_verified": False}
    else:
        trace_audit = audit_trace_file(trace_path, plan, arm, target_pid)
        issues.extend(trace_audit["issues"])
    if trace_audit.get("native_verified") is not True:
        issues.append("return:trace_native_unverified")
    if trace_audit.get("marker_verified") is not True:
        issues.append("return:trace_marker_unverified")
    if ret.get("workload_pid") != target_pid:
        issues.append("return:workload_pid_mismatch")
    for field in ("marker_id", "marker_begin_line", "native_enter_line",
                  "kernel_hit_line", "native_exit_line", "marker_end_line",
                  "native_raw_result", "native_syscall_nr"):
        if ret.get(field) != trace_audit.get(field):
            issues.append(f"return:{field}_trace_mismatch")
    if ret.get("native_syscall_nr") != plan.get("syscall_nr"):
        issues.append("return:native_syscall_nr_mismatch")
    if [dict(row) for row in hits] != [dict(row) for row in trace_audit.get("hits", [])]:
        issues.append("hit:json_trace_disagreement")

    before = _required_object(_first(directory, "stable-before.json"),
                              "stable_before", issues) or {}
    after = _required_object(_first(directory, "stable-after.json"),
                             "stable_after", issues) or {}
    stable_valid = True
    for label, snapshot in (("stable_before", before), ("stable_after", after)):
        for field in ("valid", "relations_valid"):
            if snapshot.get(field) is not True:
                issues.append(f"{label}:{field}_false")
                stable_valid = False
        if snapshot.get("trace_loss", 0) != 0:
            issues.append(f"{label}:trace_loss")
            stable_valid = False
        if snapshot.get("workers") != 1:
            issues.append(f"{label}:worker_count_mismatch")
            stable_valid = False
    rollback_valid = True
    rollback_fields = plan.get("rollback_fields", [])
    if not isinstance(rollback_fields, list) or not rollback_fields:
        issues.append("rollback:fields_missing")
        rollback_valid = False
        rollback_fields = []
    for field in rollback_fields:
        if field not in before or field not in after:
            issues.append(f"rollback:missing_{field}")
            rollback_valid = False
        elif before[field] != after[field]:
            issues.append(f"rollback:{field}_changed")
            rollback_valid = False
    persistence = _required_object(_first(directory, "persistence.json"),
                                  "persistence", issues) or {}
    for field in ("checked", "valid", "matches_stable_after", "matches_remount", "fsck_exit"):
        if field not in persistence:
            issues.append("persistence:missing_" + field)
    persistence_valid = (persistence.get("checked") is True and
                          persistence.get("valid") is True and
                          persistence.get("matches_stable_after") is True and
                          persistence.get("matches_remount") is True and
                          persistence.get("fsck_exit") == 0)
    remount = _required_object(_first(directory, "remount.json"),
                               "remount", issues) or {}
    disk = _required_object(_first(directory, "disk.json"), "disk", issues) or {}
    disk_totals = disk.get("descriptor_totals", {})
    if not isinstance(disk_totals, dict):
        disk_totals = {}
    for label, snapshot in (("remount", remount), ("disk", disk_totals)):
        if label == "remount" and (snapshot.get("valid") is not True or
                                     snapshot.get("relations_valid") is not True):
            issues.append("persistence:remount_invalid")
            persistence_valid = False
        for field in ("free_clusters", "free_inodes", "dirs"):
            if snapshot.get(field) != after.get(field):
                issues.append(f"persistence:{label}_{field}_mismatch")
                persistence_valid = False
    if disk.get("valid") is not True or disk.get("status") != "verified":
        issues.append("persistence:disk_not_verified")
        persistence_valid = False
    if persistence.get("target_state_remount") != after.get("target_file"):
        issues.append("persistence:target_file_remount_mismatch")
        persistence_valid = False
    if not all(remount.get(field) == after.get(field)
               for field in ("free_clusters", "free_inodes", "dirs")):
        persistence_valid = False
    if persistence.get("checked") is not True:
        issues.append("persistence:not_checked")

    trace_arms, trace_hits, trace_returns = _trace_fields(directory)

    if (not hits or any(issue.startswith(("plan:", "arm:", "hit:")) for issue in issues)):
        classification = "not_triggered"
    elif any(issue.startswith("return:") for issue in issues):
        classification = "wrong_return"
    elif not stable_valid:
        classification = "unstable"
    elif not rollback_valid:
        classification = "rollback_mismatch"
    elif not persistence_valid:
        classification = "persistence_mismatch"
    else:
        classification = "passed"
    blockers = bool(issues)
    return {
        "schema": SCHEMA,
        "experiment": plan.get("fault_id"),
        "directory": str(directory),
        "stages": {"arm": not any(x.startswith("arm:") or x.startswith("plan:") for x in issues),
                   "hit": bool(hits) and not any(x.startswith("hit:") for x in issues),
                   "return": not any(x.startswith("return:") for x in issues),
                   "stable": stable_valid, "rollback": rollback_valid,
                   "persistence": persistence_valid},
        "classification": classification,
        "passed": not blockers and classification == "passed",
        "issues": issues,
        "hit_count": len(hits),
        "trace_audit": trace_audit,
        "trace_markers": {"arm": len(trace_arms), "hit": len(trace_hits),
                          "return": len(trace_returns)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    report = validate(args.directory)
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
