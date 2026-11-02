#!/usr/bin/env python3
"""Build and validate the formula-to-ext4 fault-hook registry.

The errno catalog is the kernel operation authority.  This registry adds the
formula, source contract, and single-worker fixture identity that the catalog
intentionally does not contain.  A fixture id is unique even when two hooks
use the same syscall-shaped C workload: the operation, source phase, and
caller contract remain independently recorded in its evidence directory.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any

PROJECT = pathlib.Path(__file__).resolve().parents[1]
CATALOG = PROJECT / "fault-errno-catalog.json"
MATRIX = PROJECT / "hook-formula-matrix.json"
SOURCE = PROJECT / "dev/linux-6.12.1"
FAULT_HEADER = SOURCE / "fs/ext4/fault_inject.h"
FAULT_INJECTOR = SOURCE / "fs/ext4/fault_inject.c"
FIXTURE_C = PROJECT / "tests/fault-formula-single.c"

# x86_64 syscall numbers used by the single-worker fixture. Keep this explicit
# in the matrix so a hook cannot silently inherit a workload that invokes a
# different syscall from the source path it is meant to reach.
FIXTURE_SYSCALLS = {
    "alloc-mballoc": 285,
    "free-punch": 285,
    "mkdir": 258,
    "rmdir": 84,
    "unlink": 263,
    "evict-close": 3,
    "evict-close-prev": 3,
    "xattr-evict-close": 3,
    "xattr-single-evict-close": 3,
    "rename": 316,
    "rename-exchange": 316,
    "rename-whiteout": 316,
    "rename-dir": 316,
    "rename-dir-inline": 316,
    "rename-replace": 316,
    "extent-split": 285,
    "extent-grow-depth": 285,
    "resize-extend": 16,
    "resize-add-group": 16,
    "migrate-extents": 16,
    "xattr-set": 188,
    "ea-ref-set": 188,
    "acl-mode": 188,
    "inline-dir": 258,
    "inline-file-write": 1,
    "fileattr-flags": 16,
    "fileattr-project": 16,
    # The replay fixture deliberately faults the mount-time replay operation.
    "fast-commit-replay": 165,
}

FORMULAS = {
    "M-B", "M-I", "M-U", "R-BG", "R-BSUM", "R-IG", "R-ISUM",
    "R-USUM", "R-UG", "R-BDELTA", "R-IDELTA", "R-UDELTA", "R-LINK",
    "R-IH", "R-IEVICT", "R-IDELTA-EVICT", "O-LIFE", "O-LIFE-DELTA",
    "XATTR-REF", "XATTR-REF-DELTA", "P-SB", "P-DISK", "R-RESV",
    "EA-REF", "EA-REF-DELTA", "R-EXTENT", "R-INLINE", "R-FLEX",
    "R-ES", "R-MB-PENDING", "EA-INODE-REF", "R-DELALLOC", "R-MB-BUDDY",
    "R-MIGRATE", "R-MKDIR-ATOMIC", "R-XATTR-ATOMIC", "R-ACL-MODE",
    "R-INLINE-DIR", "R-FILEATTR",
}

# These formula families have no error-returning helper at the exact state
# transition.  They stay explicit so a missing fault hook cannot be mistaken
# for coverage.  R-MB-PENDING is a void journal-commit bookkeeping path.
OBSERVATION_ONLY = {
    "M-B", "M-I", "M-U", "R-IH", "P-DISK", "R-MB-PENDING",
}


def _groups(*names: str) -> set[str]:
    return set(names)


def formulas_for(operation: str) -> tuple[str, ...]:
    if operation in {"mballoc_mark", "mballoc_free_mark", "mb_buddy_load"}:
        return ("R-BG", "R-BSUM", "R-BDELTA", "R-MB-BUDDY")
    if operation.startswith("inode_evict"):
        if operation in {"inode_evict_final_access", "inode_evict_final_dirty"}:
            return ("R-IG", "R-ISUM", "R-IDELTA", "R-IEVICT", "R-IDELTA-EVICT")
        return ("R-IG", "R-ISUM", "R-IDELTA", "R-IEVICT", "R-IDELTA-EVICT")
    if operation.startswith("orphan_file"):
        return ("O-LIFE", "O-LIFE-DELTA")
    if operation.startswith("xattr_release"):
        return ("XATTR-REF", "XATTR-REF-DELTA", "EA-REF", "EA-REF-DELTA")
    if operation.startswith(("rmdir_", "unlink_", "rename_", "mkdir_")):
        result = ["R-LINK", "R-UG", "R-USUM", "R-UDELTA"]
        if operation.startswith(("mkdir_", "unlink_")):
            result.append("R-IDELTA")
        if operation.startswith("mkdir_"):
            result.append("R-MKDIR-ATOMIC")
        return tuple(result)
    if operation.startswith(("resize_", "group_add_")):
        return ("P-SB", "R-BG", "R-BSUM", "R-BDELTA", "R-FLEX")
    if operation == "fast_commit_inode_claim":
        return ("R-IG", "R-ISUM", "R-IDELTA")
    if operation == "migrate_swap_inode_data":
        return ("R-MIGRATE", "R-EXTENT")
    if operation == "xattr_set_inode_dirty":
        return ("R-XATTR-ATOMIC", "XATTR-REF", "XATTR-REF-DELTA")
    if operation == "ea_inode_ref_dirty":
        return ("EA-REF", "EA-REF-DELTA", "EA-INODE-REF")
    if operation == "acl_mode_dirty":
        return ("R-ACL-MODE",)
    if operation.startswith("inline_dir"):
        return ("R-INLINE", "R-INLINE-DIR")
    if operation == "inline_data_block_write_begin":
        return ("R-INLINE",)
    if operation.startswith("fileattr_"):
        return ("R-FILEATTR",)
    if operation == "es_insert_alloc":
        return ("R-ES",)
    if operation == "delalloc_reserve":
        return ("R-RESV", "R-DELALLOC")
    if operation == "extent_insert_dirty":
        return ("R-EXTENT",)
    raise ValueError(f"unmapped_hook:{operation}")


def fixture_action(operation: str) -> str:
    if operation == "fast_commit_inode_claim":
        return "fast-commit-replay"
    if operation in {"mballoc_free_mark"}:
        return "free-punch"
    if operation in {"mballoc_mark", "mb_buddy_load"}:
        return "alloc-mballoc"
    if operation.startswith("inode_evict"):
        return "evict-close"
    if operation.startswith("orphan_file"):
        return "evict-close"
    if operation.startswith("xattr_release"):
        return "xattr-single-evict-close" if "bitmap" in operation or "group_desc" in operation else "xattr-evict-close"
    if operation.startswith("rmdir_"):
        return "rmdir"
    if operation.startswith("unlink_"):
        return "unlink"
    if operation.startswith("rename_exchange"):
        return "rename-exchange"
    if operation in {"rename_dir_prepare_access", "rename_dir_finish"}:
        return "rename-dir"
    if operation == "rename_dir_finish_inode_access":
        return "rename-dir-inline"
    if operation in {"rename_delete_old", "rename_delete_old_dirty"}:
        return "rename-replace"
    if operation.startswith("rename_"):
        return "rename"
    if operation.startswith("mkdir_"):
        return "inline-dir" if "inline" in operation else "mkdir"
    if operation.startswith(("resize_", "group_add_")):
        return "resize-add-group" if operation.startswith("group_add_") else "resize-extend"
    if operation == "migrate_swap_inode_data":
        return "migrate-extents"
    if operation == "xattr_set_inode_dirty":
        return "xattr-set"
    if operation == "ea_inode_ref_dirty":
        return "ea-ref-set"
    if operation == "acl_mode_dirty":
        return "acl-mode"
    if operation == "inline_data_block_write_begin":
        return "inline-file-write"
    if operation == "inline_dir_block_dirty" or operation == "inline_dir_inode_dirty":
        return "inline-dir"
    if operation == "fileattr_flags_dirty":
        return "fileattr-flags"
    if operation == "fileattr_project_dirty":
        return "fileattr-project"
    if operation in {"es_insert_alloc", "extent_insert_dirty"}:
        return "extent-split"
    if operation == "delalloc_reserve":
        return "inline-file-write"
    raise ValueError(f"unmapped_fixture:{operation}")


def return_contract(definition: dict[str, Any]) -> str:
    text = definition.get("return_path", "").lower()
    if "void" in text or "ignored" in text or "continues" in text:
        return "helper_consumed_or_void"
    if "returns" in text or "return" in text:
        return "caller_error_return"
    return "source_defined"


def mutation_stage(operation: str) -> str:
    # These hooks inject into dirty-metadata helpers even though their ids do
    # not end in ``_dirty``. Keep their errno/abort contract aligned with the
    # actual ext4 call chain.
    if operation in {"rename_dir_finish", "migrate_swap_inode_data"}:
        return "dirty"
    for suffix in ("_access", "_dirty", "_read", "_alloc", "_load", "_claim"):
        if operation.endswith(suffix):
            return suffix[1:]
    return operation.rsplit("_", 1)[-1]


def build() -> dict[str, Any]:
    catalog = json.loads(CATALOG.read_text())
    hooks = catalog["hooks"]
    entries = []
    for operation, definition in hooks.items():
        formulas = list(formulas_for(operation))
        source = list(definition["source"])
        contract = return_contract(definition)
        stage = mutation_stage(operation)
        entries.append({
            "hook_id": operation,
            "operation": operation,
            "kernel_operation": definition["kernel_operation"],
            "formula_ids": formulas,
            "source_anchors": source,
            "mutation_stage": stage,
            "caller_return_contract": contract,
            "native_errno_choices": [x["name"] for x in definition["native_errnos"]],
            "fixture_id": f"fixture-{operation}-single-worker",
            "fixture_action": fixture_action(operation),
            "fixture_syscall_nr": FIXTURE_SYSCALLS[fixture_action(operation)],
            "fixture_kind": ("fast-commit-replay" if operation == "fast_commit_inode_claim"
                             else "single-worker-c"),
            "fixture_workers": 1,
            "vm_evidence_dir": f"results/hook-fixtures/{operation}",
            "vm_evidence_schema": 2,
            "evidence_required": True,
            "random_admission": operation in catalog.get("random_admission", []),
            "coverage_kind": "fault",
            "canonical_key": f"{operation}|{stage}|{contract}",
        })
    for formula in sorted(OBSERVATION_ONLY):
        entries.append({
            "hook_id": None,
            "operation": None,
            "kernel_operation": None,
            "formula_ids": [formula],
            "source_anchors": [],
            "mutation_stage": "observation-only",
            "caller_return_contract": "no_native_fault_return",
            "native_errno_choices": [],
            "fixture_id": None,
            "fixture_action": None,
            "fixture_workers": 1,
            "vm_evidence_dir": None,
            "vm_evidence_schema": 2,
            "evidence_required": False,
            "random_admission": False,
            "coverage_kind": "observation-only",
            "canonical_key": f"observation-only|{formula}",
        })
    entries.sort(key=lambda x: (x["coverage_kind"], x["hook_id"] or x["formula_ids"][0]))
    return {
        "schema": 1,
        "source": "fault-errno-catalog.json + 公式.md",
        "kernel_source": "Linux 6.12.1 ext4 development tree",
        "hook_count": len(hooks),
        "observation_only_formula_count": len(OBSERVATION_ONLY),
        "entries": entries,
    }


def _parse_fault_registration() -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Return enum-name, parse-array mappings and diagnostics from kernel C."""
    issues: list[str] = []
    try:
        header = FAULT_HEADER.read_text()
        injector = FAULT_INJECTOR.read_text()
    except OSError as exc:
        return {}, {}, [f"fault_registration_source_unreadable:{exc}"]

    enum_match = re.search(r"enum\s+ext4_fault_operation\s*\{(.*?)\};",
                           header, re.S)
    if not enum_match:
        return {}, {}, ["fault_operation_enum_missing"]
    enum_tokens = re.findall(r"\b(EXT4_FAULT_[A-Z0-9_]+)\b", enum_match.group(1))
    enum_hooks = [token for token in enum_tokens
                  if token not in {"EXT4_FAULT_OFF", "EXT4_FAULT_OPERATION_MAX"}]
    if len(enum_hooks) != len(set(enum_hooks)):
        issues.append("duplicate:fault_operation_enum")

    name_match = re.search(
        r"static\s+const\s+char\s*\*\s*ext4_fault_operation_name\s*"
        r"\([^)]*\)\s*\{(.*?)\n\}\s*\n\s*static\s+int\s+"
        r"ext4_fault_operation_parse", injector, re.S)
    if not name_match:
        issues.append("fault_operation_name_switch_missing")
        name_switch: dict[str, str] = {}
    else:
        cases = re.findall(
            r"case\s+(EXT4_FAULT_[A-Z0-9_]+)\s*:\s*"
            r"return\s+\"([^\"]+)\"\s*;", name_match.group(1))
        name_switch = dict(cases)
        if len(cases) != len(name_switch):
            issues.append("duplicate:fault_operation_name_case")
        values = [value for _, value in cases]
        if len(values) != len(set(values)):
            issues.append("duplicate:fault_operation_name_value")

    parse_match = re.search(
        r"static\s+int\s+ext4_fault_operation_parse\s*\([^)]*\)\s*\{\s*"
        r"static\s+const\s+char\s*\*\s*const\s+names\[\]\s*=\s*\{(.*?)\n\s*\};",
        injector, re.S)
    if not parse_match:
        issues.append("fault_operation_parse_array_missing")
        parse_array: dict[str, str] = {}
    else:
        entries = re.findall(
            r"\[(EXT4_FAULT_[A-Z0-9_]+)\]\s*=\s*\"([^\"]+)\"\s*,?",
            parse_match.group(1))
        parse_array = dict(entries)
        if len(entries) != len(parse_array):
            issues.append("duplicate:fault_operation_parse_enum")
        values = [value for _, value in entries]
        if len(values) != len(set(values)):
            issues.append("duplicate:fault_operation_parse_name")
    return name_switch, parse_array, issues + ["enum:" + token for token in enum_hooks]


def _source_symbol_exists(source: pathlib.Path, symbol: str) -> bool:
    text = source.read_text(errors="replace")
    # Source anchors name functions or helpers, not arbitrary prose. Accept a
    # declaration, definition, or call, while requiring the identifier to be
    # followed by an opening parenthesis.
    return re.search(r"\b" + re.escape(symbol) + r"\s*\(", text) is not None


def validate(matrix: dict[str, Any], *, check_stored: bool = False) -> list[str]:
    issues: list[str] = []
    catalog = json.loads(CATALOG.read_text())
    entries = matrix.get("entries", [])
    fault_entries = [e for e in entries if e.get("coverage_kind") == "fault"]
    if len(fault_entries) != len(catalog["hooks"]):
        issues.append("matrix_hook_count_mismatch")
    if {e.get("operation") for e in fault_entries} != set(catalog["hooks"]):
        issues.append("matrix_hook_set_mismatch")
    for field in ("hook_id", "operation", "kernel_operation", "fixture_id",
                  "canonical_key", "vm_evidence_dir"):
        values = [e.get(field) for e in fault_entries]
        if len(values) != len(set(values)):
            issues.append("duplicate:" + field)
    formula_ids = {f for e in entries for f in e.get("formula_ids", [])}
    bad = sorted(formula_ids - FORMULAS)
    if bad:
        issues.append("unknown_formula:" + ",".join(bad))
    covered = {f for e in fault_entries for f in e.get("formula_ids", [])}
    observation_entries = [e for e in entries
                           if e.get("coverage_kind") == "observation-only"]
    declared_observation = {f for e in observation_entries
                            for f in e.get("formula_ids", [])}
    if len(declared_observation) != len(observation_entries):
        issues.append("duplicate:observation_only_formula")
    if covered & declared_observation:
        issues.append("formula_fault_and_observation_overlap:" +
                      ",".join(sorted(covered & declared_observation)))
    if declared_observation != OBSERVATION_ONLY:
        issues.append("observation_only_set_mismatch")
    for formula in sorted(FORMULAS - covered - declared_observation):
        issues.append("formula_without_hook_or_observation:" + formula)
    action_match = re.search(
        r"static\s+int\s+prepare_action\s*\([^)]*\)\s*\{(.*?)\n\}\s*\n\s*static\s+long\s+run_action",
        FIXTURE_C.read_text(), re.S)
    if not action_match:
        fixture_actions: set[str] = set()
        issues.append("fixture_prepare_action_missing")
    else:
        fixture_actions = set(re.findall(
            r"!strcmp\s*\(\s*name\s*,\s*\"([^\"]+)\"\s*\)",
            action_match.group(1)))

    name_switch, parse_array, registration_issues = _parse_fault_registration()
    enum_tokens = {item[5:] for item in registration_issues if item.startswith("enum:")}
    issues.extend(item for item in registration_issues if not item.startswith("enum:"))
    expected_names = {definition["kernel_operation"]: operation
                      for operation, definition in catalog["hooks"].items()}
    expected_enums = set(expected_names)
    if enum_tokens != expected_enums:
        issues.append("fault_operation_enum_catalog_mismatch")
    for label, mapping in (("name_switch", name_switch), ("parse_array", parse_array)):
        if mapping != expected_names:
            missing = sorted(set(expected_names.items()) - set(mapping.items()))
            extra = sorted(set(mapping.items()) - set(expected_names.items()))
            issues.append(f"fault_{label}_catalog_mismatch:missing={missing}:extra={extra}")

    for e in fault_entries:
        definition = catalog["hooks"][e["operation"]]
        operation = e["operation"]
        if e["kernel_operation"] != definition["kernel_operation"]:
            issues.append("kernel_operation_mismatch:" + operation)
        if e.get("formula_ids") != list(formulas_for(operation)):
            issues.append("formula_mapping_mismatch:" + operation)
        if e.get("source_anchors") != definition.get("source"):
            issues.append("source_anchor_mismatch:" + operation)
        if e.get("caller_return_contract") != return_contract(definition):
            issues.append("caller_return_contract_mismatch:" + operation)
        expected_errnos = [item["name"] for item in definition["native_errnos"]]
        if e.get("native_errno_choices") != expected_errnos:
            issues.append("native_errno_contract_mismatch:" + operation)
        source_token = definition["kernel_operation"]
        source_token_found = False
        for anchor in e.get("source_anchors", []):
            path, sep, symbol = anchor.partition(":")
            source_path = SOURCE / path
            if not source_path.is_file():
                issues.append("missing_source:" + operation + ":" + path)
                continue
            source_text = source_path.read_text(errors="replace")
            if source_token in source_text:
                source_token_found = True
            if not sep:
                issues.append("source_anchor_missing_symbol:" + operation + ":" + anchor)
            elif not _source_symbol_exists(source_path, symbol):
                issues.append("missing_source_symbol:" + operation + ":" + anchor)
        if not source_token_found:
            issues.append("hook_token_missing_from_source_anchors:" + operation)
        if not e.get("evidence_required") or e.get("fixture_workers") != 1:
            issues.append("fixture_contract_missing:" + operation)
        if not e.get("native_errno_choices"):
            issues.append("errno_contract_missing:" + operation)
        expected_fixture_dir = f"results/hook-fixtures/{e['operation']}"
        if e.get("vm_evidence_dir") != expected_fixture_dir:
            issues.append("fixture_evidence_path_mismatch:" + operation)
        if e.get("fixture_syscall_nr") != FIXTURE_SYSCALLS.get(e.get("fixture_action")):
            issues.append("fixture_syscall_contract_mismatch:" + operation)
        action = e.get("fixture_action")
        if action == "fast-commit-replay":
            if e.get("fixture_kind") != "fast-commit-replay":
                issues.append("special_fixture_kind_missing:" + operation)
        elif action not in fixture_actions:
            issues.append("fixture_action_not_implemented:" + operation + ":" + str(action))
        elif e.get("fixture_kind") != "single-worker-c":
            issues.append("fixture_kind_mismatch:" + operation)
        if e.get("caller_return_contract") not in {
                "helper_consumed_or_void", "caller_error_return", "source_defined"}:
            issues.append("caller_return_contract_invalid:" + operation)
        if len(e.get("formula_ids", [])) != len(set(e.get("formula_ids", []))):
            issues.append("duplicate_formula_in_hook:" + operation)

    if check_stored:
        try:
            stored = json.loads(MATRIX.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"matrix_missing_or_invalid:{exc}")
        else:
            if stored != matrix:
                issues.append("stored_matrix_differs_from_catalog_and_source")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    matrix = build()
    if args.write:
        MATRIX.write_text(json.dumps(matrix, indent=2, ensure_ascii=False) + "\n")
    issues = validate(matrix, check_stored=args.check or not args.write)
    if args.check or not args.write:
        print(json.dumps({"passed": not issues, "issues": issues,
                          "hooks": matrix["hook_count"],
                          "observation_only": matrix["observation_only_formula_count"]},
                         indent=2))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
