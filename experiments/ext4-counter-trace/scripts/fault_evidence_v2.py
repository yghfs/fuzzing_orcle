#!/usr/bin/env python3
"""Validate staged formula-oriented single-worker fault evidence (schema 2)."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
from typing import Any

from fault_errno_catalog import (errno_choices, errno_codes,
                                 load_catalog, normalize_errno_choices)


KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")
PREFIX = re.compile(r"(?P<pid>\d+)\s+.*?:\s*(?P<body>.*)")


def hook_registry(operation: Any) -> tuple[dict[str, Any] | None,
                                               dict[str, Any] | None]:
    """Return the source catalog and matrix entry for *operation*.

    The plan is generated from these two files, so validation must consult the
    same authority instead of maintaining a second, incomplete list of stages
    and caller behaviours.  A malformed local registry is reported as a plan
    error by the caller and never silently treated as an unanchored hook.
    """
    if not isinstance(operation, str) or not operation:
        return None, None
    try:
        catalog = load_catalog()
        matrix_path = pathlib.Path(__file__).resolve().parents[1] / \
            "hook-formula-matrix.json"
        matrix = json.loads(matrix_path.read_text())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None, None
    definition = (catalog.get("hooks", {}) or {}).get(operation)
    entry = next((item for item in matrix.get("entries", [])
                  if isinstance(item, dict) and
                  item.get("coverage_kind") == "fault" and
                  item.get("operation") == operation), None)
    return (definition if isinstance(definition, dict) else None,
            entry if isinstance(entry, dict) else None)


def load(path: pathlib.Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def parse_trace(path: pathlib.Path) -> list[dict[str, Any]]:
    rows = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return rows
    for number, line in enumerate(lines, 1):
        match = PREFIX.search(line.strip())
        if match:
            rows.append({"line": number, "pid": int(match["pid"]),
                         "body": match["body"],
                         "fields": dict(KEY.findall(match["body"]))})
    return rows


def int_field(value: Any, default: int | None = None) -> int | None:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def read_text(path: pathlib.Path, issues: list[str], label: str) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        issues.append(f"persistence:missing_{label}")
        return ""


def required_json(path: pathlib.Path, issues: list[str], label: str) -> dict[str, Any]:
    value = load(path)
    if not isinstance(value, dict):
        issues.append(f"persistence:missing_or_invalid_{label}")
        return {}
    return value


def validate_source_contract(plan: dict[str, Any], issues: list[str]) -> bool:
    """Check a generated plan against the authoritative source registry.

    ``hook-formula-matrix.json`` intentionally contains stages such as
    ``begin``, ``old`` and ``blocks``.  Keeping a hand-written stage allowlist
    here made valid catalog entries fail closed (or encouraged future entries
    to be added in two places).  This function now checks the complete source
    contract, including the selected native errno and fixture syscall, against
    the catalog/matrix pair.
    """
    if int_field(plan.get("contract_schema"), 0) < 1:
        return True
    operation = plan.get("operation")
    definition, entry = hook_registry(operation)
    if definition is None or entry is None:
        issues.append("plan:hook_not_in_source_registry")
    hook = plan.get("hook_contract")
    if not isinstance(hook, dict) or hook.get("status") != "source_anchored":
        issues.append("plan:hook_contract_missing_or_incomplete")
        hook = {}

    if hook.get("operation") != operation:
        issues.append("plan:hook_operation_mismatch")
    if definition is not None and entry is not None:
        if hook.get("kernel_operation") != definition.get("kernel_operation"):
            issues.append("plan:hook_kernel_operation_mismatch")
        if hook.get("mutation_stage") != entry.get("mutation_stage"):
            issues.append("plan:hook_mutation_stage_mismatch")
        if hook.get("source_anchors") != entry.get("source_anchors"):
            issues.append("plan:hook_source_anchors_mismatch")
        if hook.get("catalog_sources") != definition.get("source"):
            issues.append("plan:hook_catalog_sources_mismatch")
        if hook.get("hook_caller_contract") != entry.get(
                "caller_return_contract"):
            issues.append("plan:hook_caller_contract_mismatch")
        if hook.get("return_path") != definition.get("return_path"):
            issues.append("plan:hook_return_path_mismatch")
        if plan.get("action") != entry.get("fixture_action"):
            issues.append("plan:fixture_action_mismatch")
        if int_field(plan.get("syscall_nr")) != int_field(
                entry.get("fixture_syscall_nr")):
            issues.append("plan:fixture_syscall_mismatch")
        matrix_formulas = set(entry.get("formula_ids", []))
        plan_formulas = plan.get("formulas")
        if not isinstance(plan_formulas, list) or not plan_formulas:
            issues.append("plan:formulas_invalid")
        elif not set(plan_formulas) <= matrix_formulas:
            issues.append("plan:formulas_outside_matrix")
        expected_choices = [str(item.get("name")) for item in
                            definition.get("native_errnos", [])
                            if isinstance(item, dict)]
        contract_choices = hook.get("native_errno_choices")
        if not isinstance(contract_choices, list) or [
                item.get("name") if isinstance(item, dict) else None
                for item in contract_choices] != expected_choices:
            issues.append("plan:hook_errno_choices_mismatch")
    else:
        # Keep useful diagnostics for malformed hand-authored contracts even
        # when the registry itself could not be loaded.
        if not isinstance(hook.get("kernel_operation"), str) or \
                not hook.get("kernel_operation"):
            issues.append("plan:hook_kernel_operation_missing")
        for key in ("source_anchors", "catalog_sources"):
            value = hook.get(key)
            if not isinstance(value, list) or not value or any(
                    not isinstance(item, str) or not item for item in value):
                issues.append("plan:hook_" + key + "_invalid")

    native = hook.get("native_errno")
    selected_code = abs(int_field(plan.get("errno"), 0))
    native_valid = (isinstance(native, dict) and
                    int_field(native.get("code")) == selected_code and
                    isinstance(native.get("name"), str) and
                    isinstance(native.get("source"), str) and
                    bool(native.get("source")))
    if not native_valid:
        issues.append("plan:hook_selected_errno_invalid")
    if definition is not None:
        catalog_native = next((item for item in definition.get(
            "native_errnos", []) if isinstance(item, dict) and
            int_field(item.get("code")) == selected_code), None)
        if catalog_native is None:
            issues.append("plan:hook_selected_errno_not_catalogued")
        elif native != {"name": catalog_native.get("name"),
                        "code": catalog_native.get("code"),
                        "source": catalog_native.get("source")}:
            issues.append("plan:hook_selected_errno_mismatch")
    choices = hook.get("native_errno_choices")
    if not isinstance(choices, list) or not choices or not any(
            isinstance(item, dict) and item.get("name") ==
            (native.get("name") if isinstance(native, dict) else None) and
            int_field(item.get("code")) == selected_code
            for item in choices):
        issues.append("plan:hook_errno_choices_invalid")
    caller_contract = hook.get("hook_caller_contract")
    if caller_contract not in {"caller_error_return", "helper_consumed_or_void",
                               "source_defined", "no_native_fault_return"}:
        issues.append("plan:hook_caller_contract_invalid")
    expected_behavior = {
        "caller_error_return": "hook_caller_returns_error",
        "helper_consumed_or_void": "hook_caller_consumes_error_or_is_void",
        "source_defined": "source_path_reviewed_with_recorded_syscall_result",
        "no_native_fault_return": "no_native_return_value",
    }.get(caller_contract)
    if hook.get("hook_caller_behavior") != expected_behavior:
        issues.append("plan:hook_caller_behavior_invalid")
    if hook.get("syscall_return_policy") != plan.get("return_policy"):
        issues.append("plan:hook_syscall_return_policy_mismatch")

    expected_states = plan.get("expected_formula_states")
    allowed_unverified = plan.get("allowed_unverified_formulas")
    formulas = set(plan.get("formulas") or [])
    if not isinstance(expected_states, dict) or not set(expected_states) <= formulas:
        issues.append("plan:expected_formula_states_invalid")
    if not isinstance(allowed_unverified, list) or not set(allowed_unverified) <= formulas:
        issues.append("plan:allowed_unverified_formulas_invalid")
    points = plan.get("oracle_points")
    if not isinstance(points, dict) or not set(points) <= formulas:
        issues.append("plan:oracle_points_invalid")

    recovery = plan.get("recovery")
    if not isinstance(recovery, dict):
        issues.append("plan:recovery_contract_missing")
        return False
    image_policy = recovery.get("image_policy")
    if image_policy != {"primary": "immutable", "repair_target": "copy"}:
        issues.append("plan:recovery_image_policy_invalid")
    if not isinstance(recovery.get("pre_recovery"), dict) or \
            not isinstance(recovery.get("repair"), dict) or \
            not isinstance(recovery.get("post_recovery"), dict) or \
            not isinstance(recovery.get("allowed_delta"), dict):
        issues.append("plan:recovery_contract_incomplete")
    return not any(issue.startswith("plan:") for issue in issues)


def validate_image_integrity(directory: pathlib.Path,
                             issues: list[str]) -> dict[str, Any] | None:
    """Verify that offline repair touched only the explicitly copied image.

    ``test.raw`` is the primary artifact produced by the guest.  The runner
    takes a byte-for-byte copy before any repair and runs ``e2fsck -fy`` only
    on ``recovery.raw``.  Hashes are recorded at each boundary so a clean
    post-repair check cannot hide that the original evidence was rewritten.
    """
    metadata = load(directory / "image-integrity.json")
    if not isinstance(metadata, dict):
        issues.append("persistence:image_integrity_missing")
        return None
    primary = metadata.get("primary_image")
    recovery = metadata.get("recovery_image")
    if not isinstance(primary, dict) or not isinstance(recovery, dict):
        issues.append("persistence:image_integrity_invalid")
        return metadata
    if metadata.get("schema", 1) != 1:
        issues.append("persistence:image_integrity_schema_invalid")
    if metadata.get("repair_target") != "recovery.raw":
        issues.append("persistence:repair_target_not_recovery_copy")
    evidence_parent = directory.parent.resolve()
    resolved_images: dict[str, pathlib.Path] = {}
    for label, item in (("primary", primary), ("recovery", recovery)):
        relative = item.get("path")
        if not isinstance(relative, str) or not relative:
            issues.append(f"persistence:{label}_image_path_missing")
            continue
        image = (directory / relative).resolve()
        try:
            image.relative_to(evidence_parent.resolve())
            with image.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
        except (OSError, ValueError):
            issues.append(f"persistence:{label}_image_unreadable")
            continue
        resolved_images[label] = image
        expected_after_checks = item.get("sha256_after_checks")
        if not isinstance(expected_after_checks, str):
            # A recovery image may use the older name when the final checker
            # was run immediately after repair.  New runners always emit the
            # explicit after_checks field; accepting the alias keeps archived
            # schema-2 evidence readable without weakening the primary check.
            expected_after_checks = item.get("sha256_after_repair")
        if not isinstance(expected_after_checks, str):
            issues.append(f"persistence:{label}_image_hash_missing")
        elif actual != expected_after_checks:
            issues.append(f"persistence:{label}_image_hash_changed")
        after_guest = item.get("sha256_after_guest")
        if label == "primary":
            if not isinstance(after_guest, str):
                issues.append("persistence:primary_image_guest_hash_missing")
            elif actual != after_guest:
                issues.append("persistence:primary_image_modified_by_recovery")
        elif isinstance(after_guest, str) and after_guest != \
                primary.get("sha256_after_guest"):
            issues.append("persistence:recovery_copy_guest_hash_mismatch")
        if label == "recovery" and not isinstance(
                item.get("sha256_before_repair"), str):
            issues.append("persistence:recovery_image_pre_repair_hash_missing")
    if resolved_images.get("primary") == resolved_images.get("recovery"):
        issues.append("persistence:primary_and_recovery_image_same")
    if primary.get("sha256_after_guest") != recovery.get("sha256_before_repair"):
        issues.append("persistence:recovery_copy_did_not_match_primary")
    before_repair = recovery.get("sha256_before_repair")
    after_repair = recovery.get("sha256_after_repair")
    after_checks = recovery.get("sha256_after_checks")
    if isinstance(before_repair, str) and isinstance(after_repair, str) and \
            metadata.get("repair_performed") is True and \
            before_repair == after_repair:
        issues.append("persistence:recovery_repair_did_not_change_copy")
    if isinstance(after_repair, str) and isinstance(after_checks, str) and \
            after_repair != after_checks:
        issues.append("persistence:recovery_hash_changed_after_repair")
    return metadata


def validate_association_index(directory: pathlib.Path, plan: dict[str, Any],
                               workload: dict[str, Any], arm: dict[str, Any],
                               trace: list[dict[str, Any]],
                               hits: list[dict[str, Any]],
                               formula_events: list[dict[str, Any]],
                               issues: list[str]) -> dict[str, Any] | None:
    """Rebuild and verify the explicit causal association index.

    Line numbers are deliberately used as the join key.  PID, operation and
    call index are checked as values, but a copied summary cannot invent a
    syscall or formula event without a corresponding raw trace row.
    """
    index = load(directory / "association-index.json")
    if not isinstance(index, dict):
        issues.append("association:missing_or_invalid_index")
        return None
    if index.get("schema") != 1:
        issues.append("association:schema_invalid")
    if index.get("operation") != plan.get("operation") or \
            index.get("fault_id") != plan.get("fault_id"):
        issues.append("association:plan_identity_mismatch")
    target = index.get("target")
    if not isinstance(target, dict):
        issues.append("association:target_missing")
        target = {}
    target_pid = int_field(arm.get("target_pid"))
    if target.get("pid") != target_pid or target.get("program") != plan.get("program") or \
            target.get("call_index") != plan.get("call_index"):
        issues.append("association:target_selector_mismatch")
    if target.get("generation") != int_field(arm.get("generation")):
        issues.append("association:target_generation_mismatch")

    raw_hits = [row for row in trace if row["pid"] == target_pid and
                "ext4_fault_inject:" in row["body"] and
                row["fields"].get("operation") == plan.get("operation")]
    indexed_hits = index.get("fault_hits")
    if not isinstance(indexed_hits, list) or len(indexed_hits) != len(raw_hits):
        issues.append("association:fault_hit_count_mismatch")
        indexed_hits = indexed_hits if isinstance(indexed_hits, list) else []
    for position, row in enumerate(raw_hits):
        item = indexed_hits[position] if position < len(indexed_hits) else {}
        if not isinstance(item, dict):
            issues.append(f"association:fault_hit[{position}]_invalid")
            continue
        expected = {
            "index": position, "trace_line": row["line"], "pid": target_pid,
            "operation": plan.get("operation"),
            "errno": int_field(row["fields"].get("errno")),
            "match_index": int_field(row["fields"].get("match_index")),
        }
        for field, wanted in expected.items():
            if item.get(field) != wanted:
                issues.append(f"association:fault_hit[{position}]_{field}_mismatch")
        if position >= len(hits) or not isinstance(hits[position], dict) or \
                hits[position].get("trace_line") != row["line"]:
            issues.append(f"association:fault_hit[{position}]_collector_mismatch")

    syscall = index.get("syscall")
    if not isinstance(syscall, dict):
        issues.append("association:syscall_missing")
    else:
        begins = [row for row in trace if row["pid"] == target_pid and
                  "CT_SYSCALL" in row["body"] and
                  row["fields"].get("phase") == "BEGIN"]
        ends = [row for row in trace if row["pid"] == target_pid and
                "CT_SYSCALL" in row["body"] and
                row["fields"].get("phase") == "END"]
        if len(begins) != 1 or len(ends) != 1:
            issues.append("association:syscall_marker_count_mismatch")
        else:
            begin, end = begins[0], ends[0]
            nr = int_field(begin["fields"].get("nr"))
            enters = [row for row in trace if row["pid"] == target_pid and
                      begin["line"] < row["line"] < end["line"] and
                      "sys_enter:" in row["body"] and
                      (nr is None or re.search(r"\bNR\s+" + str(nr) + r"\b",
                                               row["body"]))]
            hit_line = min((row["line"] for row in raw_hits), default=None)
            before_hit = [row for row in enters if hit_line is not None and
                          row["line"] < hit_line]
            target_enter = max(before_hit, key=lambda row: row["line"]) \
                if before_hit else (enters[0] if len(enters) == 1 else None)
            exits = [row for row in trace if target_enter is not None and
                     row["pid"] == target_pid and
                     target_enter["line"] < row["line"] < end["line"] and
                     "sys_exit:" in row["body"] and
                     (nr is None or re.search(r"\bNR\s+" + str(nr) + r"\s*=",
                                              row["body"]))]
            target_exit = exits[-1] if exits else None
            expected = {
                "marker_begin_line": begin["line"],
                "native_enter_line": target_enter["line"] if target_enter else None,
                "fault_hit_lines": [row["line"] for row in raw_hits],
                "native_exit_line": target_exit["line"] if target_exit else None,
                "marker_end_line": end["line"],
                "id": begin["fields"].get("id"), "nr": nr,
                "program": begin["fields"].get("program"),
                "call_index": int_field(begin["fields"].get("call_index")),
            }
            for field, wanted in expected.items():
                if syscall.get(field) != wanted:
                    issues.append(f"association:syscall_{field}_mismatch")
            if target_exit is not None:
                match = re.search(r"\bsys_exit:\s*NR\s+\d+\s*=\s*(-?\d+)",
                                  target_exit["body"])
                raw_return = int_field(match.group(1)) if match else None
                if syscall.get("raw_return") != raw_return:
                    issues.append("association:syscall_raw_return_mismatch")

    indexed_events = index.get("formula_events")
    if not isinstance(indexed_events, list) or len(indexed_events) != len(formula_events):
        issues.append("association:formula_event_count_mismatch")
        indexed_events = indexed_events if isinstance(indexed_events, list) else []
    hit_lines = [row["line"] for row in raw_hits]
    first_hit = min(hit_lines) if hit_lines else None
    for position, event in enumerate(formula_events):
        item = indexed_events[position] if position < len(indexed_events) else {}
        if not isinstance(item, dict):
            issues.append(f"association:formula_event[{position}]_invalid")
            continue
        line = int_field(event.get("trace_line"))
        expected_after = line is not None and first_hit is not None and line > first_hit
        for field, wanted in (("index", position), ("trace_line", line),
                              ("formula", event.get("formula")),
                              ("point", event.get("point")),
                              ("pid", int_field(event.get("pid"))),
                              ("after_fault_hit", expected_after)):
            if item.get(field) != wanted:
                issues.append(f"association:formula_event[{position}]_{field}_mismatch")
        sources = event.get("source_trace_lines", [])
        if item.get("source_trace_lines", []) != sources:
            issues.append(f"association:formula_event[{position}]_source_lines_mismatch")
        if line is not None:
            raw = next((row for row in trace if row["line"] == line), None)
            if raw is None or raw["pid"] != int_field(event.get("pid")):
                issues.append(f"association:formula_event[{position}]_raw_anchor_missing")

    objects = index.get("objects")
    if not isinstance(objects, dict):
        issues.append("association:objects_invalid")
        objects = {}
    for name, archived in objects.items():
        current = workload.get(name)
        if not isinstance(archived, dict) or not isinstance(current, dict):
            issues.append("association:object_" + str(name) + "_missing")
            continue
        for field, value in archived.items():
            if current.get(field) != value:
                issues.append("association:object_" + str(name) + "_" +
                              str(field) + "_mismatch")

    snapshots = index.get("stable_snapshots")
    if not isinstance(snapshots, dict):
        issues.append("association:stable_snapshots_invalid")
        snapshots = {}
    for name, archived in snapshots.items():
        current = load(directory / str(name), {})
        if not isinstance(archived, dict) or not isinstance(current, dict):
            issues.append("association:snapshot_" + str(name) + "_missing")
            continue
        for field in ("snapshot_id", "version", "valid", "relations_valid",
                      "namespace_valid"):
            wanted = int_field(current.get(field)) if field in (
                "snapshot_id", "version") else current.get(field)
            if archived.get(field) != wanted:
                issues.append("association:snapshot_" + str(name) + "_" +
                              field + "_mismatch")
    links = index.get("links")
    if not isinstance(links, list) or len(links) != 1 or not isinstance(links[0], dict):
        issues.append("association:links_invalid")
    else:
        link = links[0]
        expected_event_indices = [position for position, event in enumerate(
            formula_events) if int_field(event.get("trace_line")) is not None and
            first_hit is not None and int_field(event.get("trace_line")) > first_hit]
        if link.get("fault_hit_indices") != list(range(len(raw_hits))):
            issues.append("association:links_fault_hits_mismatch")
        if link.get("formula_event_indices") != expected_event_indices:
            issues.append("association:links_formula_events_mismatch")
        if sorted(link.get("object_names", [])) != sorted(objects):
            issues.append("association:links_objects_mismatch")
        if sorted(link.get("stable_snapshot_names", [])) != sorted(snapshots):
            issues.append("association:links_snapshots_mismatch")
    return index


def clean_fsck_text(text: str) -> bool:
    """Check the final read-only e2fsck output, not a collector summary flag."""
    if not text.strip():
        return False
    bad_markers = ("FILE SYSTEM WAS MODIFIED", "still has errors", "Fix? no",
                   "UNEXPECTED INCONSISTENCY", "UNRECOVERED ERRORS")
    return not any(marker in text for marker in bad_markers)


def disk_json_healthy(value: dict[str, Any]) -> bool:
    """Validate the independent on-disk checker result and its internal totals."""
    if value.get("valid") is not True or value.get("status") != "verified":
        return False
    if value.get("bitmap_descriptor_match") is not True or \
            value.get("superblock_descriptor_match") is not True:
        return False
    if value.get("unverified") not in ([], None):
        return False
    totals = value.get("descriptor_totals")
    superblock = value.get("superblock")
    if not isinstance(totals, dict) or not isinstance(superblock, dict):
        return False
    for field in ("free_clusters", "free_inodes", "dirs"):
        total = int_field(totals.get(field))
        if total is None:
            return False
        if field != "dirs" and int_field(superblock.get(field)) != total:
            return False
    for field in ("r_link", "r_ug", "r_itable_highwater",
                  "r_inode_lifecycle"):
        check = value.get(field)
        if not isinstance(check, dict) or check.get("valid") is not True:
            return False
        if check.get("unverified") not in ([], None):
            return False
    return True


def recovery_config(plan: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized recovery contract.

    Evidence produced before the recovery contract was added is kept
    readable for the two existing closures.  New plans must carry an
    explicit ``recovery.status``; an omitted contract is therefore marked
    unverified except for the historical inode-eviction fixture.
    """
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


def expected_signature(signature: str, target_inode: int | None) -> str:
    if target_inode is None:
        return signature
    return signature.replace("{target_inode}", str(target_inode))


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
    signatures = value.get("required_signatures", [])
    return [str(item) for item in signatures] if isinstance(signatures, list) else []


def contract_allowed_exit(contract: dict[str, Any], section: str,
                          key: str, default: tuple[int, ...]) -> tuple[int, ...]:
    value = contract.get(section, {})
    if not isinstance(value, dict):
        return default
    value = value.get(key, value if key == "command" else {})
    if not isinstance(value, dict):
        return default
    exits = value.get("allowed_exit")
    if not isinstance(exits, list):
        return default
    parsed = tuple(int_field(item) for item in exits)
    return tuple(item for item in parsed if item is not None) or default


def delta_allowed(value: Any, actual: int | None) -> bool:
    if actual is None:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return actual == int(value)
    if isinstance(value, dict):
        if "exact" in value:
            return actual == int_field(value.get("exact"), 10**18)
        minimum = int_field(value.get("min"), -10**18)
        maximum = int_field(value.get("max"), 10**18)
        return minimum <= actual <= maximum
    return False


def resize_transition_allowed(before: dict[str, Any], after: dict[str, Any],
                              field: str, wanted: Any) -> bool:
    """Check one resize result against an independent before/after contract."""
    before_value = int_field(before.get(field))
    after_value = int_field(after.get(field))
    if before_value is None or after_value is None:
        return False
    if isinstance(wanted, dict) and wanted.get("same") is True:
        return after_value == before_value
    if isinstance(wanted, dict) and "delta_from" in wanted:
        source = str(wanted["delta_from"])
        source_before = int_field(before.get(source))
        source_after = int_field(after.get(source))
        return (source_before is not None and source_after is not None and
                after_value - before_value == source_after - source_before)
    if isinstance(wanted, dict) and "delta" in wanted:
        return delta_allowed(wanted["delta"], after_value - before_value)
    return delta_allowed(wanted, after_value)


def native_syscall_result_matches(raw_return: int, libc_result: int | None,
                                  libc_errno: int | None) -> bool:
    """Compare raw_syscalls' kernel return with libc's syscall() result."""
    if libc_result is None or libc_errno is None:
        return False
    # The kernel tracepoint records -errno in the return register. The C
    # syscall() wrapper translates that to -1 and sets errno.
    expected_raw = -libc_errno if libc_result == -1 else libc_result
    return raw_return == expected_raw


SNAPSHOT_SEMANTIC_FIELDS = (
    "valid", "reason", "groups", "scanned", "observed_mismatches",
    "namespace_valid", "namespace_reason", "namespace_compared",
    "namespace_mismatched", "namespace_skipped_dir_nlink",
    "namespace_skipped_inline", "namespace_skipped_ea",
    "namespace_skipped_reserved", "namespace_skipped_nlink0",
    "relations_valid", "relations_reason", "relations_mismatches",
    "xattr_blocks", "orphan_slots", "free_clusters", "free_inodes", "dirs",
    "version",
)
SNAPSHOT_MONOTONIC_FIELDS = ("snapshot_id",)


def validate_snapshot_pair(directory: pathlib.Path, name: str,
                           primary: dict[str, Any], issues: list[str]) -> bool:
    """Recheck the archived two-read stability witness, not its boolean label."""
    first = load(directory / f"{name}.a.json")
    second = load(directory / f"{name}.b.json")
    if not isinstance(first, dict) or not isinstance(second, dict):
        issues.append(f"stable:{name}:raw_pair_missing_or_invalid")
        return False
    valid = True
    for field in SNAPSHOT_SEMANTIC_FIELDS:
        if field not in first or field not in second or first[field] != second[field]:
            issues.append(f"stable:{name}:raw_pair_{field}_mismatch")
            valid = False
        if field not in primary or field not in first or primary.get(field) != first.get(field):
            issues.append(f"stable:{name}:primary_differs_from_raw_pair")
            valid = False
            break
    for field in SNAPSHOT_MONOTONIC_FIELDS:
        first_value, second_value = int_field(first.get(field)), int_field(second.get(field))
        if (first_value is None or second_value is None or
                first_value >= second_value):
            issues.append(f"stable:{name}:raw_pair_{field}_not_increasing")
            valid = False
    return valid


def validate_stable_delta_event(directory: pathlib.Path,
                                event: dict[str, Any],
                                trace: list[dict[str, Any]],
                                target_pid: int | None,
                                issues: list[str]) -> bool:
    """Rebuild R-BDELTA/R-IDELTA from stable counters and raw bit transitions."""
    formula = event.get("formula")
    if formula not in ("R-BDELTA", "R-IDELTA") or \
            event.get("point") != "FIXTURE.STABLE.DELTA":
        issues.append("oracle:delta_event_identity_invalid")
        return False
    if target_pid is None:
        issues.append("oracle:delta_target_pid_missing")
        return False

    begin = [row for row in trace if row["pid"] == target_pid and
             "CT_SYSCALL" in row["body"] and
             row["fields"].get("phase") == "BEGIN"]
    end = [row for row in trace if row["pid"] == target_pid and
           "CT_SYSCALL" in row["body"] and
           row["fields"].get("phase") == "END"]
    if len(begin) != 1 or len(end) != 1 or begin[0]["line"] >= end[0]["line"]:
        issues.append("oracle:delta_action_window_missing")
        return False

    if formula == "R-BDELTA":
        counter = "free_clusters"
        points = {"BLOCK.BITMAP.TRANSITION"}
    else:
        counter = "free_inodes"
        points = {"INODE.ALLOC.BITMAP", "INODE.FREE.BITMAP"}
    before = load(directory / "stable-before.json", {}) or {}
    after = load(directory / "stable-oracle.json", {}) or {}
    before_value = int_field(before.get(counter))
    after_value = int_field(after.get(counter))
    if before_value is None or after_value is None:
        issues.append("oracle:delta_stable_counters_missing")
        return False

    def stable_counter_snapshot(snapshot):
        return (snapshot.get("valid") is True and
                snapshot.get("relations_valid") is True and
                snapshot.get("observation_stable") is True and
                snapshot.get("trace_loss", 0) == 0 and
                snapshot.get("workers") == 1)

    source_events = []
    for row in trace:
        if (row["pid"] != target_pid or
                not begin[0]["line"] < row["line"] < end[0]["line"] or
                "ext4_counter_event:" not in row["body"]):
            continue
        fields = dict(KEY.findall(row["body"].split("ext4_counter_event:", 1)[1]))
        if fields.get("formula") != formula or fields.get("point") not in points:
            continue
        delta = int_field(fields.get("update_delta"))
        if delta is None:
            issues.append("oracle:delta_source_update_missing")
            continue
        source_events.append({"trace_line": row["line"],
                              "point": fields.get("point"),
                              "update_delta": delta,
                              "ino": int_field(fields.get("ino")),
                              "group": int_field(fields.get("group"))})
    actual = after_value - before_value
    expected = sum(item["update_delta"] for item in source_events)
    comparison_valid = (stable_counter_snapshot(before) and
                        stable_counter_snapshot(after))
    state = ("consistent" if actual == expected else "mismatch_reproduced") \
        if comparison_valid else "unverified"
    source_lines = [item["trace_line"] for item in source_events]
    if event.get("actual") != actual or event.get("expected") != expected:
        issues.append("oracle:delta_event_values_disagree_with_raw_evidence")
    if event.get("source_trace_lines") != source_lines:
        issues.append("oracle:delta_event_sources_disagree_with_raw_evidence")
    if event.get("oracle_state") != state or \
            event.get("comparison_valid") is not comparison_valid:
        issues.append("oracle:delta_event_state_invalid")

    archived = load(directory / "stable-delta-checks.json", [])
    matching = [check for check in archived if isinstance(check, dict) and
                check.get("formula") == formula and
                check.get("point") == "FIXTURE.STABLE.DELTA"] \
        if isinstance(archived, list) else []
    if len(matching) != 1:
        issues.append("oracle:delta_raw_check_archive_missing")
    else:
        check = matching[0]
        for field, wanted in (("counter", counter), ("before", before_value),
                              ("after", after_value), ("actual", actual),
                              ("expected", expected),
                              ("source_events", source_events),
                              ("source_trace_lines", source_lines),
                              ("comparison_valid", comparison_valid),
                              ("oracle_state", state),
                              ("stable_before_snapshot_id",
                               int_field(before.get("snapshot_id"))),
                              ("stable_oracle_snapshot_id",
                               int_field(after.get("snapshot_id")))):
            if check.get(field) != wanted:
                issues.append("oracle:delta_archive_" + field + "_mismatch")
                break
    return not any(issue.startswith("oracle:delta_") for issue in issues)


def inline_formula_observation(target: Any) -> dict[str, Any]:
    """Recompute R-INLINE from the fixture's post-action state fields."""
    if not isinstance(target, dict):
        return {"comparison_valid": False, "oracle_state": "unverified",
                "actual": None, "expected": 1,
                "reason": "target_state_missing"}
    required = ("exists", "stat_ok", "inline_data_valid", "inline_data",
                "block0_mapped", "block0_initialized")
    if any(not isinstance(target.get(field), bool) for field in required):
        return {"comparison_valid": False, "oracle_state": "unverified",
                "actual": None, "expected": 1,
                "reason": "inline_observation_invalid"}
    if not target["exists"] or not target["stat_ok"] or not target["inline_data_valid"]:
        return {"comparison_valid": False, "oracle_state": "unverified",
                "actual": None, "expected": 1,
                "reason": "inline_observation_unavailable"}
    actual = int(target["inline_data"] or
                 (target["block0_mapped"] and target["block0_initialized"]))
    return {"comparison_valid": True,
            "oracle_state": "consistent" if actual == 1 else "mismatch_reproduced",
            "actual": actual, "expected": 1,
            "reason": "inline_or_initialized_block0"}


def validate_inline_formula_event(event: dict[str, Any],
                                  workload: dict[str, Any],
                                  trace: list[dict[str, Any]],
                                  target_pid: int | None,
                                  first_hit_line: int | None,
                                  issues: list[str]) -> bool:
    """Validate the R-INLINE comparison and its kernel failure anchor."""
    line = int_field(event.get("trace_line"))
    raw = next((row for row in trace if row["line"] == line), None) \
        if line is not None else None
    valid = True
    if raw is None:
        issues.append("oracle:inline_event_not_backed_by_raw_trace")
        valid = False
    else:
        fields = raw["fields"]
        if fields.get("formula") != "R-INLINE":
            issues.append("oracle:inline_raw_formula_mismatch")
            valid = False
        if fields.get("point") != event.get("point"):
            issues.append("oracle:inline_raw_point_mismatch")
            valid = False
        if fields.get("status") != "3":
            issues.append("oracle:inline_raw_status_not_unverified")
            valid = False
        if fields.get("reason") != "block_write_begin_return":
            issues.append("oracle:inline_raw_reason_mismatch")
            valid = False
        if raw["pid"] != int_field(event.get("pid")) or raw["pid"] != target_pid:
            issues.append("oracle:inline_raw_pid_mismatch")
            valid = False
        if first_hit_line is None or line <= first_hit_line:
            issues.append("oracle:inline_event_not_after_fault_hit")
            valid = False

    before = workload.get("target_before")
    if (not isinstance(before, dict) or before.get("exists") is not True or
            before.get("stat_ok") is not True or
            before.get("inline_data_valid") is not True or
            before.get("inline_data") is not True):
        issues.append("oracle:inline_before_not_inline")
        valid = False

    expected = inline_formula_observation(workload.get("target_after"))
    observed = event.get("observation")
    after = workload.get("target_after")
    if not isinstance(observed, dict) or not isinstance(after, dict):
        issues.append("oracle:inline_observation_archive_missing")
        valid = False
    else:
        for field in ("exists", "stat_ok", "inline_data_valid", "inline_data",
                      "block0_mapped", "block0_initialized", "block0_flags"):
            if observed.get(field) != after.get(field):
                issues.append("oracle:inline_observation_" + field + "_mismatch")
                valid = False
    for field in ("oracle_state", "actual", "expected", "comparison_valid", "reason"):
        if event.get(field) != expected.get(field):
            issues.append("oracle:inline_event_" + field + "_mismatch")
            valid = False
    if event.get("status") != ({"consistent": 1,
                                 "mismatch_reproduced": 2,
                                 "unverified": 3}.get(expected["oracle_state"], 3)):
        issues.append("oracle:inline_event_status_mismatch")
        valid = False
    return valid


def validate(directory: pathlib.Path) -> dict[str, Any]:
    directory = pathlib.Path(directory)
    issues: list[str] = []
    plan = load(directory / "fault-plan.json", {}) or {}
    if plan.get("schema") != 2:
        issues.append("plan:schema_mismatch")
    for field in ("fault_id", "operation", "action", "return_policy", "expected_return",
                  "formulas", "oracle", "expected_oracle_state",
                  "expected_formula_states", "oracle_points", "expected_namespace_states",
                  "program", "call_index", "errno", "count", "syscall_nr"):
        if field not in plan:
            issues.append("plan:missing_" + field)
    validate_source_contract(plan, issues)
    supported_errnos = [-code for code in errno_codes(plan.get("operation", ""))]
    if int_field(plan.get("errno")) not in supported_errnos:
        issues.append("plan:errno_unsupported_for_operation")
    policy = plan.get("return_policy")
    if policy not in ("exact_error", "exact_success", "record_only"):
        issues.append("plan:return_policy_invalid")
    expected = plan.get("expected_return") or {}
    count = int_field(plan.get("count"), 1) or 1
    recovery = recovery_config(plan)
    if recovery.get("status") not in ("specified", "verified"):
        issues.append("plan:recovery_contract_unverified")
    error_semantics = plan.get("error_semantics", {})
    if error_semantics:
        valid_semantics = isinstance(error_semantics, dict)
        if valid_semantics:
            semantic_name = None
            legacy_semantics = (
                error_semantics.get("injected_errno") == "EIO" and
                error_semantics.get("journal_abort") is True and
                error_semantics.get("persistence_expectation") ==
                "clean_without_repair" and
                "forced_shutdown" not in error_semantics)
            try:
                semantic_name = normalize_errno_choices(
                    str(error_semantics.get("injected_errno", "")))[0]
                choices = errno_choices(plan.get("operation", ""))
                codes = errno_codes(plan.get("operation", ""))
                semantic_code = -codes[choices.index(semantic_name)]
                valid_semantics = semantic_code == int_field(plan.get("errno"))
            except (ValueError, IndexError, TypeError):
                valid_semantics = False
            if not legacy_semantics and \
                    error_semantics.get("forced_shutdown") is not False:
                valid_semantics = False
            if semantic_name == "EIO":
                direct_failed_write_cleanup = (
                    error_semantics.get("journal_abort") is False and
                    error_semantics.get("failed_write_cleanup") is True and
                    error_semantics.get("scope") ==
                    "inline-data-to-extent-block-write-begin")
                direct_source_return = (
                    error_semantics.get("journal_abort") is False and
                    error_semantics.get("source_return") is True)
                valid_semantics = valid_semantics and (
                    (error_semantics.get("journal_abort") is True and
                     error_semantics.get("abort_scope") == "journal") or
                    direct_failed_write_cleanup or direct_source_return)
            else:
                direct_native_return = (
                    error_semantics.get("journal_abort") is False and
                    error_semantics.get("caller_contract") in {
                        "void_helper_retry",
                        "void_helper_discards_optional_cache_error",
                        "write_returns_error",
                        "caller_consumed_fallback",
                        "allocation_returns_error",
                    })
                # Catalog entries whose caller contract is ``source_defined``
                # deliberately do not guess whether the surrounding ext4
                # caller consumes the helper error or propagates it.  The
                # generated plan still records the reviewed source path and
                # the concrete native outcome (for example an EROFS handle
                # abort).  Requiring one of the generic caller labels above
                # incorrectly rejected those plans and made a real hit look
                # like a plan/trigger failure.  Accept the source-defined
                # form only when it carries an explicit return/abort witness.
                source_defined_path = (
                    error_semantics.get("caller_contract") in {
                        "source_defined", "source_path_reviewed"} and
                    (error_semantics.get("source_return") is True or
                     error_semantics.get("handle_abort") is True or
                     error_semantics.get("journal_abort") is True))
                valid_semantics = valid_semantics and (direct_native_return or (
                    error_semantics.get("handle_abort") is True and
                    error_semantics.get("ext4_error_handler") ==
                    "ext4_handle_dirty_metadata_error") or
                    source_defined_path)
        if not valid_semantics:
            issues.append("plan:error_semantics_contract_invalid")

    arm = load(directory / "arm.json", {}) or {}
    workload = load(directory / "workload-result.json", {}) or {}
    if arm.get("status") != "armed" or arm.get("enabled") is not True:
        issues.append("arm:not_armed")
    if arm.get("operation") != plan.get("operation"):
        issues.append("arm:operation_mismatch")
    if arm.get("errno") != plan.get("errno") or \
            arm.get("errno_choices") != str(plan.get("errno")) or \
            arm.get("errno_choice_index") != 0:
        issues.append("arm:errno_choice_mismatch")
    if arm.get("remaining") != arm.get("count", count):
        issues.append("arm:remaining_mismatch")
    if arm.get("injected", 0) != 0:
        issues.append("arm:already_injected")
    if arm.get("matches", 0) != 0:
        issues.append("arm:already_matched")
    if arm.get("program") != plan.get("program") or \
            arm.get("call_index") != plan.get("call_index"):
        issues.append("arm:selector_mismatch")
    target_pid = int_field(arm.get("target_pid"))
    if not target_pid or workload.get("pid") != target_pid or workload.get("arm_verified") is not True:
        issues.append("arm:workload_readback_failed")

    hits = load(directory / "hits.json", [])
    if not isinstance(hits, list):
        issues.append("hit:not_array")
        hits = []
    if len(hits) != count:
        issues.append("hit:count_mismatch")
    for index, hit in enumerate(hits):
        if not isinstance(hit, dict):
            issues.append(f"hit[{index}]:not_object")
            continue
        for field in ("operation", "generation", "injection_count", "pid",
                      "program", "target_program", "call_index", "match_index",
                      "errno", "errno_choice_index", "errno_choice_draw"):
            if field not in hit:
                issues.append(f"hit[{index}]:missing_{field}")
        if hit.get("operation") != plan.get("operation"):
            issues.append(f"hit[{index}]:operation_mismatch")
        if hit.get("pid") != target_pid:
            issues.append(f"hit[{index}]:pid_mismatch")
        if hit.get("generation") != arm.get("generation"):
            issues.append(f"hit[{index}]:generation_mismatch")
        if hit.get("program") != plan.get("program") or \
                hit.get("target_program") != plan.get("program"):
            issues.append(f"hit[{index}]:program_mismatch")
        if hit.get("call_index") != plan.get("call_index") or \
                hit.get("match_index") != int_field(plan.get("call_index"), 0) + index:
            issues.append(f"hit[{index}]:call_index_mismatch")
        if hit.get("injection_count") != index + 1:
            issues.append(f"hit[{index}]:injection_order_mismatch")
        if hit.get("errno") != plan.get("errno") or hit.get("errno") not in supported_errnos:
            issues.append(f"hit[{index}]:errno_mismatch_or_unsupported")
        if hit.get("errno_choice_index") != 0 or hit.get("errno_choice_draw") != 0:
            issues.append(f"hit[{index}]:errno_choice_mismatch")

    status_after = workload.get("status_after")
    if isinstance(status_after, str):
        try:
            status_after = json.loads(status_after)
        except ValueError:
            status_after = None
    if not isinstance(status_after, dict):
        issues.append("hit:status_after_invalid")
    else:
        if status_after.get("remaining") != 0:
            issues.append("hit:remaining_not_zero")
        if status_after.get("injected") != count:
            issues.append("hit:injected_count_mismatch")
        if status_after.get("generation") != arm.get("generation"):
            issues.append("hit:generation_changed")
        expected_matches = int_field(plan.get("call_index"), 0) + count
        if status_after.get("matches") != expected_matches:
            issues.append("hit:match_count_mismatch")
        if status_after.get("errno") != plan.get("errno") or \
                status_after.get("errno_choices") != str(plan.get("errno")) or \
                status_after.get("errno_choice_index") != 0:
            issues.append("hit:status_errno_choice_mismatch")

    result = int_field(workload.get("syscall_result", workload.get("syscall_rc")))
    error = int_field(workload.get("syscall_errno", 0), 0)
    if policy == "exact_error" and (result != expected.get("result") or
                                      error != expected.get("errno")):
        issues.append("return:exact_error_mismatch")
    elif policy == "exact_success" and (result != expected.get("result", 0) or
                                          error != expected.get("errno", 0)):
        issues.append("return:exact_success_mismatch")
    if policy != "record_only" and workload.get("return_verified") is False:
        issues.append("return:workload_unverified")

    object_contract = plan.get("expected_objects")
    capture_only_objects = (isinstance(object_contract, dict) and
                            object_contract.get("capture_only") is True)
    if (not isinstance(object_contract, dict) or
            (not object_contract.get("present") and not capture_only_objects)):
        issues.append("objects:contract_missing")
        object_contract = {}
    before_capture_complete = workload.get("before_capture_ok") is True
    after_capture_complete = workload.get("after_capture_ok") is True
    # An EIO journal abort can make the in-mount post-action stat/FIEMAP
    # sample unavailable even though the inode is present after remount. The
    # plan must explicitly declare the abort and the expected unstable
    # post-action snapshot before this exception is allowed. The missing
    # capture remains visible in the report and persistence is still gated by
    # raw image evidence.
    expected_stable_states = plan.get("expected_stable_states") or {}
    expected_after_state = (expected_stable_states.get("stable-after", {})
                            if isinstance(expected_stable_states, dict) else {})
    post_capture_unavailable_allowed = (
        isinstance(error_semantics, dict) and
        error_semantics.get("journal_abort") is True and
        after_capture_complete is False and
        isinstance(expected_after_state, dict) and
        expected_after_state.get("valid") is False)

    def object_after_unavailable(name: Any) -> bool:
        return (post_capture_unavailable_allowed and isinstance(name, str) and
                name.endswith("_after"))

    # Catalog-generated plans deliberately declare capture_only when their
    # source-level return contract has not yet been reduced to an object
    # postcondition.  A failed best-effort post-hit FIEMAP/stat sample must be
    # retained as incomplete evidence, but it must not turn an otherwise
    # verified arm/hit/return/stable fixture into a false semantic failure.
    # Explicit object contracts still require both captures.
    if not before_capture_complete or (not after_capture_complete and
                                       not capture_only_objects and
                                       not post_capture_unavailable_allowed):
        issues.append("objects:state_capture_failed")
    resize_contract = plan.get("expected_resize")
    if resize_contract is not None:
        resize_before = workload.get("resize_before")
        resize_after = workload.get("resize_after")
        if not isinstance(resize_before, dict) or not isinstance(resize_after, dict):
            issues.append("resize:state_missing")
        else:
            for prefix, state in (("before", resize_before), ("after", resize_after)):
                if state.get("captured") is not True:
                    issues.append(f"resize:{prefix}:not_captured")
                for field in ("fs_blocks", "fs_free_blocks", "groups", "free_clusters",
                              "free_inodes", "dirs", "version", "snapshot_id"):
                    if int_field(state.get(field)) is None:
                        issues.append(f"resize:{prefix}:missing_{field}")
            if not isinstance(resize_contract, dict):
                issues.append("resize:contract_invalid")
            else:
                for phase, state in (("before", resize_before), ("after", resize_after)):
                    expected_fields = resize_contract.get(phase, {})
                    if not isinstance(expected_fields, dict):
                        issues.append(f"resize:{phase}_contract_invalid")
                        continue
                    for field, wanted in expected_fields.items():
                        if phase == "after":
                            matched = resize_transition_allowed(
                                resize_before, resize_after, field, wanted)
                        else:
                            actual = int_field(state.get(field))
                            matched = actual is not None and delta_allowed(wanted, actual)
                        if not matched:
                            issues.append(f"resize:{phase}:{field}_mismatch")
    object_names = set()
    for field, wanted in (object_contract.get("present") or {}).items():
        object_names.add(field)
        if object_after_unavailable(field):
            continue
        state = workload.get(field)
        if not isinstance(state, dict):
            issues.append(f"objects:{field}:missing")
        elif state.get("exists") is not wanted:
            issues.append(f"objects:{field}:presence_mismatch")
        elif wanted and state.get("stat_ok") is not True:
            issues.append(f"objects:{field}:stat_unverified")
    state_fields = ("exists", "stat_ok", "size", "blocks", "ino", "nlink",
                    "extent_count", "extent_hash", "xattr_present", "xattr_size",
                    "xattr_hash")
    for relation in object_contract.get("same_state", []):
        if not isinstance(relation, list) or len(relation) != 2:
            issues.append("objects:same_state_contract_invalid")
            continue
        left, right = relation
        object_names.update((left, right))
        if object_after_unavailable(left) or object_after_unavailable(right):
            continue
        a, b = workload.get(left), workload.get(right)
        if not isinstance(a, dict) or not isinstance(b, dict) or any(
                field not in a or field not in b or a[field] != b[field]
                for field in state_fields):
            issues.append(f"objects:{left}:{right}:state_changed")
    for relation_key in ("same_inode", "swapped_inode"):
        for relation in object_contract.get(relation_key, []):
            if not isinstance(relation, list) or len(relation) != 2:
                issues.append(f"objects:{relation_key}_contract_invalid")
                continue
            left, right = relation
            object_names.update((left, right))
            if object_after_unavailable(left) or object_after_unavailable(right):
                continue
            a, b = workload.get(left), workload.get(right)
            if not isinstance(a, dict) or not isinstance(b, dict) or \
                    a.get("exists") is not True or b.get("exists") is not True or \
                    a.get("ino") != b.get("ino"):
                issues.append(f"objects:{left}:{right}:inode_relation_mismatch")
    for relation in object_contract.get("decreased_extent_count", []):
        if not isinstance(relation, list) or len(relation) != 2:
            issues.append("objects:decreased_extent_count_contract_invalid")
            continue
        left, right = relation
        if object_after_unavailable(left) or object_after_unavailable(right):
            continue
        a, b = workload.get(left), workload.get(right)
        if not isinstance(a, dict) or not isinstance(b, dict) or \
                int_field(a.get("extent_count")) is None or \
                int_field(b.get("extent_count")) is None or \
                int_field(b.get("extent_count")) >= int_field(a.get("extent_count")):
            issues.append(f"objects:{left}:{right}:extent_count_not_decreased")

    # A stage may leave a precisely described allocation behind while all
    # other object identity and mapping fields must remain unchanged.  Keep
    # this generic and fail closed on malformed/empty contracts so an
    # expected resource delta cannot silently weaken object validation.
    transitions = object_contract.get("state_transitions", [])
    if not isinstance(transitions, list):
        issues.append("objects:state_transitions_contract_invalid")
        transitions = []
    for index, transition in enumerate(transitions):
        if not isinstance(transition, dict):
            issues.append(f"objects:state_transition[{index}]:contract_invalid")
            continue
        before_name = transition.get("before")
        after_name = transition.get("after")
        unchanged_fields = transition.get("unchanged_fields")
        delta_fields = transition.get("delta_fields")
        if (not isinstance(before_name, str) or not isinstance(after_name, str) or
                not isinstance(unchanged_fields, list) or not unchanged_fields or
                not isinstance(delta_fields, dict) or not delta_fields):
            issues.append(f"objects:state_transition[{index}]:contract_invalid")
            continue
        object_names.update((before_name, after_name))
        if object_after_unavailable(before_name) or object_after_unavailable(after_name):
            continue
        before_state = workload.get(before_name)
        after_state = workload.get(after_name)
        if not isinstance(before_state, dict) or not isinstance(after_state, dict):
            issues.append(f"objects:{before_name}:{after_name}:state_missing")
            continue
        invalid_fields = [field for field in unchanged_fields
                          if not isinstance(field, str) or not field]
        if invalid_fields:
            issues.append(f"objects:state_transition[{index}]:unchanged_fields_invalid")
            continue
        missing = [field for field in unchanged_fields
                   if field not in before_state or field not in after_state]
        if missing:
            issues.append(f"objects:{before_name}:{after_name}:unchanged_field_missing")
        elif any(before_state[field] != after_state[field]
                 for field in unchanged_fields):
            issues.append(f"objects:{before_name}:{after_name}:unchanged_field_changed")
        for field, wanted_delta in delta_fields.items():
            before_value = int_field(before_state.get(field))
            after_value = int_field(after_state.get(field))
            if before_value is None or after_value is None or \
                    not delta_allowed(wanted_delta, after_value - before_value):
                issues.append(f"objects:{before_name}:{after_name}:{field}_delta_mismatch")

    observed_object_fields = plan.get("observed_object_fields", {})
    if not isinstance(observed_object_fields, dict):
        issues.append("objects:observed_fields_contract_invalid")
        observed_object_fields = {}
    observed_object_fields_ok = True
    for object_name, expected_fields in observed_object_fields.items():
        object_names.add(object_name)
        if object_after_unavailable(object_name):
            continue
        observed = workload.get(object_name)
        if not isinstance(expected_fields, dict) or not isinstance(observed, dict):
            issues.append(f"objects:{object_name}:observed_fields_missing")
            observed_object_fields_ok = False
            continue
        for field, wanted in expected_fields.items():
            actual = observed.get(field)
            matches = actual is wanted if isinstance(wanted, bool) else actual == wanted
            if not matches:
                issues.append(f"objects:{object_name}:{field}_observation_mismatch")
                observed_object_fields_ok = False

    trace = parse_trace(directory / "trace.txt")
    trace_hits = [row for row in trace if "ext4_fault_inject:" in row["body"]]
    raw_hit_observed = bool(trace_hits or hits)
    if len(trace_hits) != count:
        issues.append("hit:trace_count_mismatch")
    trace_hit_fields = ("operation", "generation", "injection_count", "pid", "target_pid",
                        "program", "target_program", "call_index", "match_index",
                        "errno", "errno_choice_index", "errno_choice_draw",
                        "result", "group", "block", "len")
    for index, row in enumerate(trace_hits):
        fields = row["fields"]
        normalized = {
            "operation": fields.get("operation"),
            "generation": int_field(fields.get("generation")),
            "injection_count": int_field(fields.get("injection", fields.get("injection_count"))),
            "pid": int_field(fields.get("pid")),
            "target_pid": int_field(fields.get("target_pid")),
            "program": fields.get("program"),
            "target_program": fields.get("target_program"),
            "call_index": int_field(fields.get("call_index")),
            "match_index": int_field(fields.get("match_index")),
            "errno": int_field(fields.get("errno", fields.get("error"))),
            "errno_choice_index": int_field(fields.get("errno_choice_index")),
            "errno_choice_draw": int_field(fields.get("errno_choice_draw")),
            "result": int_field(fields.get("result")),
            "group": int_field(fields.get("group")),
            "block": int_field(fields.get("block")),
            "len": int_field(fields.get("len")),
            "trace_line": row["line"],
        }
        if row["pid"] != target_pid or normalized["pid"] != target_pid:
            issues.append("hit:trace_pid_mismatch")
        expected_trace = {
            "operation": plan.get("operation"),
            "generation": arm.get("generation"),
            "injection_count": index + 1,
            "pid": target_pid,
            "target_pid": target_pid,
            "program": plan.get("program"),
            "target_program": plan.get("program"),
            "call_index": plan.get("call_index"),
            "match_index": int_field(plan.get("call_index"), 0) + index,
            "errno": plan.get("errno"),
            "errno_choice_index": 0,
            "errno_choice_draw": 0,
            "result": plan.get("errno"),
        }
        for field, wanted in expected_trace.items():
            if normalized.get(field) != wanted:
                issues.append(f"hit:trace_{field}_mismatch")
        if index >= len(hits) or not isinstance(hits[index], dict):
            continue
        for field in trace_hit_fields:
            if field not in hits[index]:
                issues.append(f"hit:collector_missing_{field}")
            elif hits[index].get(field) != normalized.get(field):
                issues.append(f"hit:collector_trace_{field}_mismatch")
        if hits[index].get("trace_line") != row["line"]:
            issues.append("hit:collector_trace_line_mismatch")

    syscall_nr = int_field(plan.get("syscall_nr"))
    trace_arm_markers = [row for row in trace if "CT_FAULT_ARM" in row["body"] and
                         row["fields"].get("phase") == "ARM_END" and
                         row["pid"] == target_pid]
    if len(trace_arm_markers) != 1:
        issues.append("arm:trace_arm_marker_count_mismatch")
    elif trace_arm_markers[0]["line"] >= min(
            [row["line"] for row in trace if "CT_SYSCALL" in row["body"] and
             row["fields"].get("phase") == "BEGIN" and row["pid"] == target_pid] or [10**18]):
        issues.append("arm:trace_arm_not_before_call")
    else:
        fields = trace_arm_markers[0]["fields"]
        for field, wanted in (("operation", plan.get("operation")),
                              ("program", plan.get("program")),
                              ("call_index", plan.get("call_index")),
                              ("errno", plan.get("errno")),
                              ("pid", target_pid)):
            if fields.get(field) != str(wanted):
                issues.append(f"arm:trace_{field}_mismatch")
    begins = [row for row in trace if "CT_SYSCALL" in row["body"] and
              row["fields"].get("phase") == "BEGIN" and row["pid"] == target_pid]
    ends = [row for row in trace if "CT_SYSCALL" in row["body"] and
            row["fields"].get("phase") == "END" and row["pid"] == target_pid]
    if len(begins) != 1 or len(ends) != 1:
        issues.append("return:native_envelope_missing")
    else:
        begin, end = begins[0], ends[0]
        begin_fields, end_fields = begin["fields"], end["fields"]
        if syscall_nr is not None and int_field(begin["fields"].get("nr")) != syscall_nr:
            issues.append("return:fixture_syscall_nr_mismatch")
        envelope_fields = ("id", "nr", "name", "program", "call_index", "action", "target_fd")
        for field in envelope_fields:
            if field not in begin_fields or field not in end_fields:
                issues.append(f"return:envelope_missing_{field}")
            elif begin_fields.get(field) != end_fields.get(field):
                issues.append(f"return:envelope_{field}_mismatch")
        if begin_fields.get("program") != plan.get("program") or \
                int_field(begin_fields.get("call_index")) != plan.get("call_index"):
            issues.append("return:envelope_fixture_identity_mismatch")
        if end_fields.get("ret") != str(result) or \
                end_fields.get("errno") != str(error):
            issues.append("return:envelope_result_mismatch")
        target_fd = int_field(begin["fields"].get("target_fd"))
        def is_target_close_enter(row: dict[str, Any]) -> bool:
            if row["pid"] != target_pid or not (begin["line"] < row["line"] < end["line"]):
                return False
            if "sys_enter:" not in row["body"] or not re.search(
                    r"\bNR\s+" + str(syscall_nr) + r"\b", row["body"]):
                return False
            if syscall_nr != 3:
                return True
            match = re.search(r"\bsys_enter:\s*NR\s+3\s+\(([^,]+)", row["body"])
            return bool(match and int_field(match.group(1)) == target_fd)

        native_enter_candidates = [row for row in trace if is_target_close_enter(row)]
        hit_line = min((row["line"] for row in trace_hits), default=None)
        before_hit = ([row for row in native_enter_candidates
                       if hit_line is not None and row["line"] < hit_line])
        # syscall_marker() writes its own trace marker through write(2), so a
        # marker envelope can contain several sys_enter/sys_exit pairs for the
        # same syscall number.  The target syscall is the last matching entry
        # before the kernel fault event, not the marker write preceding it.
        if before_hit:
            native_enter = [max(before_hit, key=lambda row: row["line"])]
        elif len(native_enter_candidates) == 1:
            native_enter = native_enter_candidates
        else:
            native_enter = []
        native_exit_candidates = []
        if len(native_enter) == 1:
            enter_line = native_enter[0]["line"]
            native_exit_candidates = [
                row for row in trace
                if row["pid"] == target_pid and enter_line < row["line"] < end["line"]
                and "sys_exit:" in row["body"] and re.search(
                    r"\bNR\s+" + str(syscall_nr) + r"\s*=", row["body"])]

        def raw_syscall_return(row: dict[str, Any]) -> int | None:
            match = re.search(r"\bsys_exit:\s*NR\s+\d+\s*=\s*(-?\d+)",
                              row["body"])
            return int_field(match.group(1)) if match else None

        matching_exits = [row for row in native_exit_candidates
                          if (hit_line is None or row["line"] > hit_line) and
                          native_syscall_result_matches(
                              raw_syscall_return(row), result, error)]
        native_exit = matching_exits if matching_exits else native_exit_candidates
        if len(native_enter) != 1 or len(native_exit) != 1:
            issues.append("return:native_syscall_pair_missing")
        else:
            enter, exit_ = native_enter[0], native_exit[0]
            if not begin["line"] < enter["line"] < exit_["line"] < end["line"]:
                issues.append("return:native_syscall_outside_marker")
            if not any(enter["line"] < hit["line"] < exit_["line"] for hit in trace_hits):
                issues.append("hit:outside_action_envelope")
            raw_match = re.search(r"\bsys_exit:\s*NR\s+\d+\s*=\s*(-?\d+)", exit_["body"])
            raw_return = int_field(raw_match.group(1)) if raw_match else None
            if raw_return is not None and not native_syscall_result_matches(
                    raw_return, result, error):
                issues.append("return:native_result_mismatch")
            if raw_return is not None and end_fields.get("ret") != str(result):
                issues.append("return:trace_marker_result_mismatch")
        for row in trace_hits:
            if not (begin["line"] < row["line"] < end["line"]):
                issues.append("hit:outside_fixture_syscall_envelope")
            if row["pid"] != begin["pid"]:
                issues.append("hit:envelope_pid_mismatch")

    formula_events = load(directory / "formula-events.json", [])
    if not isinstance(formula_events, list):
        issues.append("oracle:formula_events_not_array")
        formula_events = []
    formulas = set(plan.get("formulas") or [])
    observed = {event.get("formula") for event in formula_events if isinstance(event, dict)}
    for formula in sorted(formulas - observed):
        issues.append("oracle:formula_not_evaluated:" + formula)
    raw_formula_rows = {}
    for row in trace:
        if "ext4_counter_event:" not in row["body"]:
            continue
        fields = dict(KEY.findall(row["body"].split("ext4_counter_event:", 1)[1]))
        raw_formula_rows[row["line"]] = (row, fields)
    hit_lines = [row["line"] for row in trace_hits]
    first_hit_line = min(hit_lines) if hit_lines else None
    allowed_states = {"consistent", "mismatch_expected", "mismatch_reproduced", "unverified"}
    expected_by_formula = plan.get("expected_formula_states") or {}
    expected_state = plan.get("expected_oracle_state", "unverified")
    expected_points = plan.get("oracle_points") or {}
    allowed_unverified = set(plan.get("allowed_unverified_formulas") or [])
    if expected_state not in allowed_states:
        issues.append("oracle:expected_state_invalid")
    if not isinstance(expected_by_formula, dict) or not isinstance(expected_points, dict):
        issues.append("oracle:plan_expectations_invalid")
        expected_by_formula, expected_points = {}, {}
    observed_points: dict[str, set[str]] = {}
    comparable_formulas: set[str] = set()
    for event in formula_events:
        if not isinstance(event, dict):
            issues.append("oracle:event_not_object")
            continue
        formula = event.get("formula")
        point = event.get("point")
        observed_points.setdefault(str(formula), set()).add(str(point))
        if event.get("evidence_kind") == "fixture_inline_formula":
            validate_inline_formula_event(event, workload, trace, target_pid,
                                          first_hit_line, issues)
        elif event.get("evidence_kind") == "stable_delta_aggregate":
            validate_stable_delta_event(directory, event, trace, target_pid,
                                        issues)
        elif event.get("evidence_kind") == "fault_hit_only":
            hit_line = int_field(event.get("trace_line"))
            if hit_line is None or first_hit_line is None or hit_line != first_hit_line:
                issues.append("oracle:fault_hit_anchor_mismatch")
            if event.get("oracle_state") != "unverified" or \
                    event.get("comparison_valid") is not False:
                issues.append("oracle:fault_hit_only_must_be_unverified")
        else:
            line = int_field(event.get("trace_line"))
            raw = raw_formula_rows.get(line) if line is not None else None
            if raw is None:
                issues.append("oracle:event_not_backed_by_raw_trace")
            else:
                row, fields = raw
                for field, wanted in (("formula", formula), ("point", point),
                                      ("status", str(event.get("status"))),
                                      ("actual", str(event.get("actual"))),
                                      ("expected", str(event.get("expected")))):
                    if fields.get(field) != wanted:
                        issues.append("oracle:raw_event_" + field + "_mismatch")
                if row["pid"] != int_field(event.get("pid")):
                    issues.append("oracle:raw_event_pid_mismatch")
                if first_hit_line is None or line <= first_hit_line:
                    issues.append("oracle:event_not_after_fault_hit")
        if event.get("oracle_relevant") is False:
            continue
        wanted = expected_by_formula.get(formula, expected_state)
        got = event.get("oracle_state")
        allowed_formula_states = (set(wanted) if isinstance(wanted, list)
                                  else {wanted})
        if not allowed_formula_states <= allowed_states:
            issues.append("oracle:expected_formula_state_invalid:" + str(formula))
        # ``allowed_unverified`` is a fallback for a missing source-specific
        # terminal event.  If the collector has a valid comparison, accept
        # its observed state and let the formula evidence become stronger;
        # otherwise an optional formula would remain permanently blocked by
        # the plan's historical ``unverified`` default.
        optional_valid_observation = (
            formula in allowed_unverified and
            got in ("consistent", "mismatch_reproduced") and
            event.get("comparison_valid") is True)
        if got not in allowed_formula_states and not optional_valid_observation:
            rendered_wanted = ",".join(sorted(str(item)
                                               for item in allowed_formula_states))
            issues.append(f"oracle:state_mismatch:{formula}:{got}:{rendered_wanted}")
        if (event.get("comparison_valid") is True and
                got in ("consistent", "mismatch_reproduced")):
            comparable_formulas.add(str(formula))
    for formula in formulas:
        wanted_points = set(expected_points.get(formula, []))
        missing_points = wanted_points - observed_points.get(formula, set())
        if missing_points:
            issues.append("oracle:point_not_evaluated:" + str(formula) + ":" +
                          ",".join(sorted(missing_points)))
    for formula in sorted(formulas - comparable_formulas - allowed_unverified):
        issues.append("oracle:formula_no_valid_comparison:" + formula)
    required_comparisons = formulas - allowed_unverified
    if required_comparisons and not (comparable_formulas & required_comparisons):
        issues.append("oracle:no_valid_formula_comparison")
    if not formula_events and not (not formulas and
                                    plan.get("oracle") == "stable_snapshot_only"):
        issues.append("oracle:not_evaluated")

    association_index = None
    if int_field(plan.get("contract_schema"), 0) >= 1:
        association_index = validate_association_index(
            directory, plan, workload, arm, trace, hits, formula_events, issues)

    stable_oracle = load(directory / "stable-oracle.json", {}) or {}
    stable_oracle_ok = validate_snapshot_pair(
        directory, "stable-oracle.json", stable_oracle, issues)
    if (stable_oracle.get("observation_stable") is not True or
            stable_oracle.get("trace_loss", 0) != 0 or
            stable_oracle.get("workers") != 1):
        stable_oracle_ok = False
        issues.append("stable:stable-oracle:invalid")
    if "R-BDELTA" in formulas and int_field(stable_oracle.get("free_clusters")) is None:
        stable_oracle_ok = False
        issues.append("stable:stable-oracle:free_clusters_missing")
    if "R-IDELTA" in formulas and int_field(stable_oracle.get("free_inodes")) is None:
        stable_oracle_ok = False
        issues.append("stable:stable-oracle:free_inodes_missing")

    stable_ok = stable_oracle_ok
    stable_records = []
    expected_namespace = plan.get("expected_namespace_states") or {}
    expected_stable = plan.get("expected_stable_states") or {}
    namespace_match = True
    for name, plan_name in (("stable-before.json", "stable-before"),
                            ("stable-after.json", "stable-after"),
                            ("remount.json", "remount")):
        value = load(directory / name, {}) or {}
        stable_records.append(value)
        if not validate_snapshot_pair(directory, name, value, issues):
            stable_ok = False
        if (value.get("observation_stable") is not True or
                value.get("trace_loss", 0) != 0 or value.get("workers") != 1):
            stable_ok = False
            issues.append("stable:" + name + ":invalid")
        expected_values = expected_stable.get(plan_name, {}) \
            if isinstance(expected_stable, dict) else {}
        if not isinstance(expected_values, dict):
            expected_values = {}
        for field in ("valid", "relations_valid", "namespace_valid"):
            # Preserve three states in the plan: an explicit null means this
            # snapshot property is intentionally unspecified; an omitted
            # field keeps the historical defaults. In particular, do not
            # turn an explicit null into the generic `True` expectation.
            if field in expected_values:
                wanted = expected_values[field]
            elif field == "namespace_valid":
                wanted = expected_namespace.get(plan_name)
            else:
                wanted = True
            if wanted is not None and value.get(field) is not wanted:
                stable_ok = False
                if field == "namespace_valid":
                    namespace_match = False
                issues.append("stable:" + plan_name + ":" + field + "_mismatch")

    persistence = load(directory / "persistence.json", {}) or {}
    # Independently validate archived command status, fsck text and disk JSON.
    # The collector's normalized persistence flags are cross-checked below,
    # never accepted as the sole proof of recovery.
    metadata = required_json(directory / "run-meta.json", issues, "run_meta")
    image_integrity = None
    if int_field(plan.get("contract_schema"), 0) >= 1:
        image_integrity = validate_image_integrity(directory, issues)
    disk_before = required_json(directory / "disk-before-recovery.json", issues,
                                "pre_recovery_disk")
    disk_after = required_json(directory / "disk.json", issues, "post_recovery_disk")
    before_fsck_text = read_text(directory / "fsck-before-recovery.txt", issues,
                                 "pre_recovery_fsck")
    recovery_fsck_text = read_text(directory / "fsck-recovery.txt", issues,
                                   "recovery_fsck")
    final_fsck_text = read_text(directory / "fsck.txt", issues, "final_fsck")
    run_status_text = read_text(directory / "run-status.txt", issues, "run_status")
    unmount_status = read_text(directory / "unmount-status.txt", issues,
                               "unmount_status").strip()
    status_fields = dict(re.findall(r"(?m)^([A-Za-z_]+)=([^\s]+)$", run_status_text))
    pre_fsck_rc = int_field(metadata.get("pre_recovery_fsck_exit"))
    pre_disk_rc = int_field(metadata.get("pre_recovery_disk_exit"))
    recovery_rc = int_field(metadata.get("recovery_exit"))
    final_fsck_rc = int_field(metadata.get("fsck_exit"))
    final_disk_rc = int_field(metadata.get("disk_exit"))
    if any(value is None for value in
           (pre_fsck_rc, pre_disk_rc, recovery_rc, final_fsck_rc, final_disk_rc)):
        issues.append("persistence:run_meta_exit_codes_missing")
    if int_field(metadata.get("guest_exit")) != 0 or \
            status_fields.get("workload_rc") != "0" or \
            status_fields.get("after_snapshot_rc") != "0":
        issues.append("persistence:guest_workload_or_snapshot_failed")
    if status_fields.get("unmount_rc") != "0" or unmount_status != "unmount_ok":
        issues.append("persistence:unmount_not_verified")
    if status_fields.get("remount_rc") != "0" or \
            status_fields.get("remount_umount_rc") != "0":
        issues.append("persistence:remount_not_verified")
    if status_fields.get("trace_loss") != "0" or status_fields.get("workers") != "1":
        issues.append("persistence:run_status_loss_or_worker_mismatch")

    target_before = workload.get("target_before")
    target_inode = int_field(target_before.get("ino")) if isinstance(target_before, dict) else None

    if pre_fsck_rc != persistence.get("pre_recovery_fsck_exit") or \
            pre_disk_rc != persistence.get("pre_recovery_disk_exit") or \
            recovery_rc != persistence.get("recovery_exit") or \
            final_fsck_rc != persistence.get("fsck_exit") or \
            final_disk_rc != persistence.get("disk_exit"):
        issues.append("persistence:collector_exit_codes_disagree_with_run_meta")
    pre_fsck_state = contract_state(recovery, "pre_recovery", "fsck", "unverified")
    pre_disk_state = contract_state(recovery, "pre_recovery", "disk", "unverified")
    repair_kind = str((recovery.get("repair") or {}).get("kind", "unverified"))
    post_fsck_state = contract_state(recovery, "post_recovery", "fsck", "unverified")
    post_disk_state = contract_state(recovery, "post_recovery", "disk", "unverified")

    expected_pre_failure = False
    if not before_fsck_text.strip():
        issues.append("persistence:pre_recovery_fsck_missing")
    elif pre_fsck_state == "clean":
        if pre_fsck_rc != 0 or not clean_fsck_text(before_fsck_text):
            issues.append("persistence:pre_recovery_fsck_not_clean")
    elif pre_fsck_state == "known_issue":
        allowed = contract_allowed_exit(recovery, "pre_recovery", "fsck", (1, 2, 4, 8))
        if pre_fsck_rc not in allowed:
            issues.append("persistence:pre_recovery_fsck_exit_unexpected")
        signatures = contract_signatures(recovery, "pre_recovery", "fsck")
        expected_pre_failure = bool(signatures)
        for signature in signatures:
            rendered = expected_signature(signature, target_inode)
            if rendered not in before_fsck_text:
                issues.append("persistence:pre_recovery_fsck_signature_missing")
    elif pre_fsck_state == "inspect":
        if pre_fsck_rc == 0:
            if not clean_fsck_text(before_fsck_text):
                issues.append("persistence:pre_recovery_fsck_inspection_inconsistent")
        elif pre_fsck_rc not in contract_allowed_exit(
                recovery, "pre_recovery", "fsck", (1, 2, 4, 8)):
            issues.append("persistence:pre_recovery_fsck_exit_unexpected")
        else:
            # `inspect` means the fixture allows either a clean image or a
            # measured inconsistency that the declared repair step must
            # resolve. A non-zero read-only e2fsck result is the independent
            # evidence that this run exercised the repair branch.
            expected_pre_failure = True
            signatures = contract_signatures(recovery, "pre_recovery", "fsck")
            for signature in signatures:
                rendered = expected_signature(signature, target_inode)
                if rendered not in before_fsck_text:
                    issues.append("persistence:pre_recovery_fsck_signature_missing")
    else:
        issues.append("persistence:pre_recovery_fsck_contract_invalid")

    pre_disk_healthy = disk_json_healthy(disk_before)
    if pre_disk_state == "verified":
        if pre_disk_rc != 0 or not pre_disk_healthy:
            issues.append("persistence:pre_recovery_disk_not_verified")
    elif pre_disk_state == "mismatch":
        expected_pre_failure = True
        if pre_disk_rc == 0 or disk_before.get("valid") is not False:
            issues.append("persistence:pre_recovery_disk_mismatch_not_observed")
    elif pre_disk_state == "inspect":
        if pre_disk_healthy:
            pass
        elif pre_disk_rc != 0 and disk_before.get("valid") is False:
            expected_pre_failure = True
        else:
            issues.append("persistence:pre_recovery_disk_inspection_inconsistent")
    else:
        issues.append("persistence:pre_recovery_disk_contract_invalid")

    if repair_kind == "none":
        allowed_repair_exit = contract_allowed_exit(recovery, "repair", "command", (0,))
        repair_ok = recovery_rc in allowed_repair_exit
        if pre_fsck_state == "clean" and recovery_rc == 0 and \
                clean_fsck_text(recovery_fsck_text):
            pass
        if any(signature not in recovery_fsck_text
               for signature in contract_signatures(recovery, "repair", "command")):
            repair_ok = False
    elif repair_kind == "e2fsck-fy":
        allowed_repair_exit = contract_allowed_exit(recovery, "repair", "command", (0, 1))
        repair_ok = recovery_rc in allowed_repair_exit and bool(recovery_fsck_text.strip())
        if any(signature not in recovery_fsck_text
               for signature in contract_signatures(recovery, "repair", "command")):
            repair_ok = False
    else:
        repair_ok = False
        issues.append("persistence:repair_contract_unsupported")
    if not repair_ok:
        issues.append("persistence:repair_result_not_proven")

    if post_fsck_state == "clean":
        if final_fsck_rc != 0 or not clean_fsck_text(final_fsck_text):
            issues.append("persistence:final_fsck_not_clean")
    else:
        issues.append("persistence:post_recovery_fsck_contract_invalid")
    if post_disk_state == "verified":
        if final_disk_rc != 0 or not disk_json_healthy(disk_after):
            issues.append("persistence:post_recovery_disk_not_independently_verified")
    else:
        issues.append("persistence:post_recovery_disk_contract_invalid")

    if recovery_rc not in (0, 1) or not recovery_fsck_text.strip():
        issues.append("persistence:recovery_result_missing_or_invalid")

    remount = load(directory / "remount.json", {}) or {}
    before_totals = disk_before.get("descriptor_totals", {})
    after_totals = disk_after.get("descriptor_totals", {})
    collector_disk_totals = persistence.get("disk_counters", {})
    remount_counter_state = contract_state(
        recovery, "pre_recovery", "remount_counters", "must_match")
    remount_comparison_ok = False
    if remount_counter_state == "must_match":
        remount_comparison_ok = persistence.get("matches_remount") is True
        for field in ("free_clusters", "free_inodes", "dirs"):
            if not isinstance(before_totals, dict) or \
                    remount.get(field) != before_totals.get(field):
                issues.append(f"persistence:remount_pre_recovery_{field}_mismatch")
        if persistence.get("remount_counter_state", "must_match") != "must_match":
            issues.append("persistence:remount_counter_contract_disagrees")
        if persistence.get("matches_remount") is not True:
            issues.append("persistence:remount_counters_not_matched")
    elif remount_counter_state == "unverified":
        # The plan explicitly says the remount scan cannot serve as a raw
        # disk-counter measurement. Require the collector to preserve that
        # distinction; pre/post-repair disk and fsck checks still gate
        # recovery below.
        remount_comparison_ok = (
            persistence.get("matches_remount") is None and
            persistence.get("remount_counter_state") == "unverified")
        if not remount_comparison_ok:
            issues.append("persistence:remount_counters_should_be_unverified")
    else:
        issues.append("plan:recovery_remount_counter_state_invalid")
    for field in ("free_clusters", "free_inodes", "dirs"):
        if not isinstance(after_totals, dict):
            issues.append(f"persistence:post_recovery_{field}_missing")
        elif not isinstance(collector_disk_totals, dict) or \
                collector_disk_totals.get(field) != after_totals.get(field):
            issues.append(f"persistence:collector_post_recovery_{field}_mismatch")
    allowed_deltas = recovery.get("allowed_delta", {})
    if not isinstance(allowed_deltas, dict):
        allowed_deltas = {}
        issues.append("persistence:allowed_delta_contract_invalid")
    for field in ("free_clusters", "free_inodes", "dirs"):
        if not isinstance(before_totals, dict) or not isinstance(after_totals, dict):
            continue
        delta = int_field(after_totals.get(field)) - int_field(before_totals.get(field)) \
            if int_field(after_totals.get(field)) is not None and \
            int_field(before_totals.get(field)) is not None else None
        if field not in allowed_deltas or not delta_allowed(allowed_deltas.get(field), delta):
            issues.append(f"persistence:recovery_{field}_delta_unexpected")

    inspect_clean_path = (
        repair_kind == "e2fsck-fy" and
        pre_fsck_state == "inspect" and pre_disk_state == "inspect" and
        pre_fsck_rc == 0 and clean_fsck_text(before_fsck_text) and
        pre_disk_healthy and pre_disk_rc == 0)
    recovery_failure_expected = (
        expected_pre_failure or inspect_clean_path
        if repair_kind != "none" else not expected_pre_failure)
    persistence_checked = (recovery.get("status") in ("specified", "verified") and
                           recovery_failure_expected and
                           not any(issue.startswith(("persistence:", "plan:recovery"))
                                   for issue in issues))
    persistence_ok = (persistence_checked and persistence.get("valid") is True and
                      remount_comparison_ok and
                      post_fsck_state == "clean" and post_disk_state == "verified" and
                      clean_fsck_text(final_fsck_text) and disk_json_healthy(disk_after))
    if not persistence_ok and not any(issue.startswith("persistence:") for issue in issues):
        issues.append("persistence:raw_evidence_contract_mismatch")

    runtime_formula_mismatch = any(
        event.get("oracle_state") == "mismatch_reproduced"
        and event.get("comparison_valid") is True
        for event in formula_events if isinstance(event, dict))
    has_observed_finding = expected_pre_failure or runtime_formula_mismatch
    persistent_postcondition_mismatch = (
        expected_pre_failure or pre_fsck_rc != 0 or
        not clean_fsck_text(before_fsck_text) or pre_disk_rc != 0 or
        not pre_disk_healthy)
    plan_valid = not any(x.startswith("plan:") for x in issues)
    arm_valid = not any(x.startswith("arm:") for x in issues)
    hit_contract_valid = not any(x.startswith("hit:") for x in issues)
    association_valid = not any(x.startswith("association:") for x in issues)
    if not plan_valid:
        classification = "plan_invalid"
    elif not arm_valid:
        classification = "not_armed"
    elif not raw_hit_observed:
        classification = "not_triggered"
    elif not hit_contract_valid:
        classification = "hit_invalid"
    elif not association_valid:
        classification = "association_invalid"
    elif any(x.startswith("return:") for x in issues):
        classification = "wrong_return"
    elif not stable_ok:
        classification = "unstable"
    elif any(x.startswith("oracle:") for x in issues):
        classification = "oracle_unverified"
    elif error_semantics and persistent_postcondition_mismatch:
        classification = "injected_persistent_postcondition_violation"
    elif not persistence_ok:
        classification = "persistence_mismatch"
    elif error_semantics:
        classification = ("injected_error_no_persistent_damage"
                          if error_semantics.get("journal_abort") is False
                          else "injected_journal_abort_no_persistent_damage")
    elif has_observed_finding:
        classification = "finding_reproduced_recovered"
    else:
        classification = "passed"
    finding_reproduced = (persistent_postcondition_mismatch
                          if error_semantics else has_observed_finding)
    runtime_difference = bool(runtime_formula_mismatch or
                              any(issue.startswith("stable:") and
                                  "mismatch" in issue for issue in issues))
    disk_difference = bool(persistent_postcondition_mismatch or
                           pre_fsck_rc not in (None, 0) or
                           pre_disk_rc not in (None, 0))
    return {"schema": 2, "experiment": plan.get("fault_id"),
            "classification": classification, "passed": not issues,
            "plan_valid": plan_valid,
            "hit_observed": raw_hit_observed,
            "hit_contract_valid": hit_contract_valid,
            "association_valid": association_valid,
            "finding_reproduced": finding_reproduced,
            "persistent_postcondition_mismatch": persistent_postcondition_mismatch,
            "runtime_formula_mismatch": runtime_formula_mismatch,
            "runtime_difference": runtime_difference,
            "disk_difference": disk_difference,
            "image_integrity": image_integrity,
            "association_index": association_index,
            "observed_object_fields_valid": observed_object_fields_ok,
            "object_capture": {
                "before_complete": workload.get("before_capture_ok") is True,
                "after_complete": workload.get("after_capture_ok") is True,
                "after_required": not capture_only_objects,
            },
            "error_semantics": error_semantics,
            "syscall_result": result, "syscall_errno": error,
            "pre_recovery_fsck_clean": pre_fsck_rc == 0 and
            clean_fsck_text(before_fsck_text),
            "pre_recovery_disk_verified": pre_disk_rc == 0 and pre_disk_healthy,
            "recovery_verified": persistence_ok and stable_ok,
            "stable_namespace_valid": {key: value.get("namespace_valid")
                                        for key, value in zip(
                                            ("stable-before", "stable-after", "remount"),
                                            stable_records)},
            "issues": issues, "hit_count": len(hits),
            "stages": {"plan_valid": plan_valid,
                       "arm": arm_valid,
                       "hit_observed": raw_hit_observed,
                       "hit": hit_contract_valid and raw_hit_observed,
                       "association": association_valid,
                       "return": not any(x.startswith("return:") for x in issues),
                       "stable": stable_ok,
                       "namespace_expectation": namespace_match,
                       "oracle": not any(x.startswith("oracle:") for x in issues),
                       "persistence_checked": persistence_checked,
                       "remount_counters": remount_comparison_ok,
                       "persistence_healthy": persistence_ok,
                       "disk_difference": disk_difference,
                       "recovery": persistence_ok}}


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
