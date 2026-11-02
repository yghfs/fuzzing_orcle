#!/usr/bin/env python3
"""Audit per-hook single-worker fixture definitions and real VM evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any

PROJECT = pathlib.Path(__file__).resolve().parents[1]
RESULTS = PROJECT / "results"
MATRIX = PROJECT / "hook-formula-matrix.json"
OUTPUT = RESULTS / "hook-fixture-inventory.json"


def read_json(path: pathlib.Path) -> Any:
    """Read either an object or an array from an evidence JSON file.

    Validation and plan files are objects, while ``formula-events.json`` is
    deliberately an array.  Returning only dictionaries silently discarded
    the formula evidence and made every mapped formula look unclosed.
    """
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value


def read_object(path: pathlib.Path) -> dict[str, Any]:
    value = read_json(path)
    return value if isinstance(value, dict) else {}


def digest(path: pathlib.Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def find_runs(operation: str) -> list[pathlib.Path]:
    base = RESULTS / "hook-fixtures" / operation
    if not base.is_dir():
        return []
    return sorted((path for path in base.glob("vm-*") if path.is_dir()),
                  key=lambda path: path.name)


def load_run(operation: str, path: pathlib.Path,
             matrix_formulas: list[str]) -> dict[str, Any]:
    if operation == "fast_commit_inode_claim":
        validation_path = path / "validation.json"
        trace_path = path / "ext4-counter-trace" / "trace.txt"
        plan_path = path / "fault-plan.json"
    else:
        validation_path = path / "evidence" / "validation.json"
        trace_path = path / "evidence" / "trace.txt"
        plan_path = path / "evidence" / "fault-plan.json"
    report = read_object(validation_path)
    plan = read_object(plan_path)
    formula_events = read_json(path / "evidence" / "formula-events.json") or []
    if operation == "fast_commit_inode_claim":
        formula_events = report.get("inode_formula_events", [])
    if not isinstance(formula_events, list):
        formula_events = []
    stages = report.get("stages", {})
    if not isinstance(stages, dict):
        stages = {}
    # Generic formula fixtures use the v2 names (oracle,
    # persistence_checked/persistence_healthy), while the dedicated
    # fast-commit fixture predates that schema and uses formula/persistence.
    # Normalize both formats here so the inventory reports closure instead of
    # silently turning valid generic evidence into null fields.
    formula_closed = (stages.get("formula") is True or
                      stages.get("oracle") is True)
    persistence_checked = (stages.get("persistence") is True or
                            stages.get("persistence_checked") is True)
    persistence_healthy = (stages.get("persistence") is True or
                            stages.get("persistence_healthy") is True)
    persistence_closed = persistence_checked and persistence_healthy
    plan_formulas = plan.get("formulas", [])
    if not isinstance(plan_formulas, list):
        plan_formulas = []
    plan_formulas = [str(formula) for formula in plan_formulas]
    formulas = [str(formula) for formula in matrix_formulas]
    plan_formula_set = set(plan_formulas)
    matrix_formula_set = set(formulas)

    def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
        """Normalize generic and fast-commit formula event schemas."""
        normalized = dict(event)
        comparison = event.get("comparison_valid")
        if isinstance(comparison, bool):
            comparison_valid = comparison
        else:
            comparison_valid = comparison == 1
        normalized["comparison_valid"] = comparison_valid
        state = event.get("oracle_state")
        if state not in ("consistent", "mismatch_reproduced", "unverified"):
            status = event.get("status")
            if status == 1 and comparison_valid and \
                    event.get("actual") == event.get("expected"):
                state = "consistent"
            elif status == 2 and comparison_valid and \
                    event.get("actual") != event.get("expected"):
                state = "mismatch_reproduced"
            else:
                state = "unverified"
        normalized["oracle_state"] = state
        # Older collectors marked the hook-hit attribution record as relevant;
        # its evidence kind is authoritative and must never count as a formula
        # comparison.
        if event.get("evidence_kind") == "fault_hit_only":
            normalized["oracle_relevant"] = False
        if event.get("status") == 0 and not comparison_valid:
            normalized["oracle_relevant"] = False
        return normalized

    formula_events = [normalize_event(event) for event in formula_events
                      if isinstance(event, dict)]
    formula_checks: dict[str, dict[str, Any]] = {}
    for formula in formulas:
        relevant = [event for event in formula_events
                    if event.get("formula") == formula and
                    event.get("oracle_relevant") is not False]
        comparisons = [event for event in relevant
                       if event.get("comparison_valid") is True and
                       event.get("oracle_state") in
                       ("consistent", "mismatch_reproduced")]
        allowed_list = plan.get("allowed_unverified_formulas", [])
        if not isinstance(allowed_list, list):
            allowed_list = []
        allowed_unverified = (formula in allowed_list and
                              formula in plan_formula_set)
        if formula not in plan_formula_set:
            status = "missing_plan"
        elif comparisons:
            status = "compared"
        elif allowed_unverified:
            status = "allowed_unverified"
        else:
            status = "missing_comparison"
        formula_checks[formula] = {
            "closed": bool(comparisons),
            "comparison_count": len(comparisons),
            "states": sorted({str(event.get("oracle_state")) for event in relevant}),
            "allowed_unverified": allowed_unverified,
            "status": status,
        }
    all_mapped_formulas_closed = bool(formulas) and all(
        item["closed"] for item in formula_checks.values())
    compared_formula_count = sum(item["closed"]
                                 for item in formula_checks.values())
    allowed_unverified_count = sum(
        item["status"] == "allowed_unverified"
        for item in formula_checks.values())
    missing_comparison_count = sum(
        item["status"] in ("missing_comparison", "missing_plan")
        for item in formula_checks.values())
    missing_plan_count = sum(
        item["status"] == "missing_plan" for item in formula_checks.values())
    unexpected_plan_formulas = sorted(plan_formula_set - matrix_formula_set)
    plan_formula_mismatch = (
        plan_formula_set != matrix_formula_set or
        len(plan_formulas) != len(plan_formula_set))
    oracle_contract_satisfied = bool(
        stages.get("formula") is True or stages.get("oracle") is True)
    injected_candidate = (
        report.get("classification") == "injected_persistent_postcondition_violation" or
        report.get("finding_reproduced") is True or
        report.get("persistent_postcondition_mismatch") is True or
        report.get("runtime_formula_mismatch") is True)
    operation_matches = plan.get("operation") == operation
    if operation == "fast_commit_inode_claim":
        operation_matches = operation_matches and report.get("fault_id") == \
            "fast-commit-inode-claim"
    evidence_ready = all(stages.get(stage) is True
                         for stage in ("arm", "hit", "return", "stable"))
    trace_present = trace_path.is_file() and trace_path.stat().st_size > 0
    return {
        "run_dir": path.relative_to(RESULTS).as_posix(),
        "plan_present": bool(plan),
        "operation_matches": operation_matches,
        "validation_present": bool(report),
        "classification": report.get("classification"),
        "passed": report.get("passed") is True,
        "stages": {key: stages.get(key) for key in
                   ("arm", "hit", "return", "stable", "formula", "oracle",
                    "persistence", "persistence_checked", "persistence_healthy")},
        # ``formula_oracle_closed`` is intentionally strict: every formula
        # mapped to this hook needs at least one independently valid
        # comparison.  The validator stage may still pass when a plan allows
        # an unverified formula; expose that as a separate field.
        "formula_oracle_closed": all_mapped_formulas_closed,
        "oracle_stage_passed": formula_closed,
        "oracle_contract_satisfied": all_mapped_formulas_closed,
        "formula_checks": formula_checks,
        "mapped_formula_count": len(formulas),
        "matrix_formulas": formulas,
        "plan_formulas": plan_formulas,
        "plan_formula_mismatch": plan_formula_mismatch,
        "missing_plan_formula_count": missing_plan_count,
        "missing_plan_formulas": [formula for formula, check in formula_checks.items()
                                  if check["status"] == "missing_plan"],
        "unexpected_plan_formulas": unexpected_plan_formulas,
        "compared_formula_count": compared_formula_count,
        "allowed_unverified_formula_count": allowed_unverified_count,
        "missing_comparison_formula_count": missing_comparison_count,
        "all_mapped_formulas_closed": all_mapped_formulas_closed,
        "persistence_evidence_checked": persistence_checked,
        "persistence_healthy": persistence_healthy,
        "persistence_closed": persistence_closed,
        "injected_candidate": injected_candidate,
        "vm_hit_evidence_complete": operation_matches and evidence_ready and trace_present,
        "trace_present": trace_present,
        "trace_sha256": digest(trace_path),
        "issues": report.get("issues", []),
        "validation_path": validation_path.relative_to(RESULTS).as_posix(),
    }


def build() -> dict[str, Any]:
    matrix = json.loads(MATRIX.read_text())
    entries = [entry for entry in matrix["entries"]
               if entry.get("coverage_kind") == "fault"]
    hooks = []
    for entry in entries:
        operation = entry["operation"]
        matrix_formulas = [str(formula) for formula in entry.get("formula_ids", [])]
        runs = [load_run(operation, path, matrix_formulas)
                for path in find_runs(operation)]
        latest_attempt = runs[-1] if runs else None
        # A later interrupted attempt must not erase an earlier complete VM
        # hit record. Keep both views: best verified evidence and last attempt.
        verified_runs = [run for run in runs
                         if run["vm_hit_evidence_complete"]]
        latest = verified_runs[-1] if verified_runs else latest_attempt
        hooks.append({
            "hook_id": entry["hook_id"],
            "operation": operation,
            "fixture_id": entry["fixture_id"],
            "fixture_action": entry["fixture_action"],
            "fixture_syscall_nr": entry["fixture_syscall_nr"],
            "fixture_definition_valid": True,
            "vm_evidence_dir": entry["vm_evidence_dir"],
            "vm_run_count": len(runs),
            "latest_vm_run": latest,
            "latest_attempt": latest_attempt,
            "vm_hit_evidence_complete": bool(
                verified_runs),
            "formula_oracle_closed": bool(
                latest and latest["formula_oracle_closed"]),
            "oracle_stage_passed": bool(
                latest and latest["oracle_stage_passed"]),
            "oracle_contract_satisfied": bool(
                latest and latest["oracle_contract_satisfied"]),
            "formula_checks": latest["formula_checks"] if latest else {},
            "matrix_formulas": matrix_formulas,
            "plan_formulas": latest["plan_formulas"] if latest else [],
            "plan_formula_mismatch": bool(
                latest and latest["plan_formula_mismatch"]),
            "missing_plan_formula_count": int(
                latest["missing_plan_formula_count"])
            if latest else len(matrix_formulas),
            "missing_plan_formulas": latest["missing_plan_formulas"]
            if latest else matrix_formulas,
            "unexpected_plan_formulas": latest["unexpected_plan_formulas"]
            if latest else [],
            "mapped_formula_count": len(matrix_formulas),
            "compared_formula_count": int(latest["compared_formula_count"])
            if latest else 0,
            "allowed_unverified_formula_count": int(
                latest["allowed_unverified_formula_count"])
            if latest else 0,
            "missing_comparison_formula_count": int(
                latest["missing_comparison_formula_count"])
            if latest else 0,
            "all_mapped_formulas_closed": bool(
                latest and latest["all_mapped_formulas_closed"]),
            "persistence_evidence_checked": bool(
                latest and latest["persistence_evidence_checked"]),
            "persistence_healthy": bool(
                latest and latest["persistence_healthy"]),
            "persistence_closed": bool(
                latest and latest["persistence_closed"]),
            "injected_candidate": bool(latest and latest["injected_candidate"]),
        })
    complete = sum(item["vm_hit_evidence_complete"] for item in hooks)
    formula_closed = sum(item["formula_oracle_closed"] for item in hooks)
    oracle_stage_passed = sum(item["oracle_stage_passed"] for item in hooks)
    oracle_contracts = sum(item["oracle_contract_satisfied"] for item in hooks)
    all_formulas_closed = sum(item["all_mapped_formulas_closed"] for item in hooks)
    persistence_checked = sum(item["persistence_evidence_checked"] for item in hooks)
    persistence_healthy = sum(item["persistence_healthy"] for item in hooks)
    persistence_closed = sum(item["persistence_closed"] for item in hooks)
    injected_candidates = sum(item["injected_candidate"] for item in hooks)
    mapped_formulas = sum(item["mapped_formula_count"] for item in hooks)
    compared_formulas = sum(item["compared_formula_count"] for item in hooks)
    allowed_unverified_formulas = sum(
        item["allowed_unverified_formula_count"] for item in hooks)
    missing_comparison_formulas = sum(
        item["missing_comparison_formula_count"] for item in hooks)
    missing_plan_formulas = sum(
        item["missing_plan_formula_count"] for item in hooks)
    plan_formula_mismatches = sum(
        item["plan_formula_mismatch"] for item in hooks)
    return {
        "schema": 2,
        "hook_count": len(hooks),
        "vm_hit_evidence_complete": complete,
        "vm_hit_evidence_missing": len(hooks) - complete,
        "formula_oracle_closed": formula_closed,
        "formula_oracle_missing": len(hooks) - formula_closed,
        "oracle_stage_passed": oracle_stage_passed,
        "oracle_stage_missing": len(hooks) - oracle_stage_passed,
        "oracle_contract_satisfied": oracle_contracts,
        "mapped_formula_count": mapped_formulas,
        "compared_formula_count": compared_formulas,
        "allowed_unverified_formula_count": allowed_unverified_formulas,
        "missing_comparison_formula_count": missing_comparison_formulas,
        "missing_plan_formula_count": missing_plan_formulas,
        "plan_formula_mismatch_count": plan_formula_mismatches,
        "unclosed_formula_count": mapped_formulas - compared_formulas,
        "all_mapped_formulas_closed": all_formulas_closed,
        "persistence_evidence_checked": persistence_checked,
        "persistence_healthy": persistence_healthy,
        "persistence_closed": persistence_closed,
        "persistence_candidates": persistence_checked - persistence_healthy,
        "injected_candidates": injected_candidates,
        "hooks": hooks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--require-formula-closure", action="store_true",
                        help="fail when any mapped hook formula lacks a valid comparison")
    args = parser.parse_args()
    report = build()
    if args.write:
        RESULTS.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: report[key] for key in
                      ("hook_count", "vm_hit_evidence_complete",
                       "vm_hit_evidence_missing", "formula_oracle_closed",
                       "formula_oracle_missing", "oracle_stage_passed",
                       "oracle_stage_missing", "oracle_contract_satisfied",
                       "mapped_formula_count", "compared_formula_count",
                       "allowed_unverified_formula_count",
                       "missing_comparison_formula_count", "missing_plan_formula_count",
                       "plan_formula_mismatch_count", "unclosed_formula_count",
                       "all_mapped_formulas_closed", "persistence_evidence_checked",
                       "persistence_healthy", "persistence_closed",
                       "persistence_candidates", "injected_candidates")}, indent=2))
    if args.check and report["vm_hit_evidence_missing"]:
        missing = [hook["operation"] for hook in report["hooks"]
                   if not hook["vm_hit_evidence_complete"]]
        print("missing_vm_hit_evidence=" + ",".join(missing), file=sys.stderr)
        return 1
    if args.require_formula_closure and report["unclosed_formula_count"]:
        missing = [hook["operation"] for hook in report["hooks"]
                   if not hook["all_mapped_formulas_closed"]]
        print("unclosed_formula_hooks=" + ",".join(missing), file=sys.stderr)
        plan_missing = [
            hook["operation"] + ":" + ",".join(hook["missing_plan_formulas"])
            for hook in report["hooks"] if hook["missing_plan_formulas"]]
        if plan_missing:
            print("missing_formula_plan=" + ";".join(plan_missing),
                  file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
