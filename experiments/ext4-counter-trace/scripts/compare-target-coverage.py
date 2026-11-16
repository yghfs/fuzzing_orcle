#!/usr/bin/env python3
"""Build a target-PC comparison from one custom run and one syz-manager run.

The two runners intentionally keep their native artifact formats.  This
reporter joins only the fields that have the same meaning: target ext4/JBD2
PCs from a verified custom corpus and covered PCs from syz-manager's corpus.
All-sample custom coverage remains visible as a diagnostic metric.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any


def load(path: pathlib.Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def sha256(path: pathlib.Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def custom_result(run: pathlib.Path, target_scope: str) -> dict[str, Any]:
    campaign = load(run / "campaign.json", {}) or {}
    report = load(run / "campaign-report.json", {}) or {}
    feedback = report.get("kcov_feedback") or campaign.get("kcov_feedback") or {}
    limits = campaign.get("limits", {})
    coverage_present = "corpus_target_pc_coverage" in feedback
    return {
        "run_dir": str(run),
        "status": campaign.get("status"),
        "passed": bool(report.get("passed")),
        "coverage_complete": bool(report.get("coverage_complete")),
        "kcov_feedback_complete": bool(report.get("kcov_feedback_complete")),
        "stop_reason": campaign.get("stop_reason"),
        "collection_elapsed_seconds": campaign.get("collection_elapsed_seconds"),
        "requested_target_syscalls": int(limits.get("target_syscalls", 0) or 0),
        "feedback_mode": limits.get("feedback_mode", feedback.get("feedback_mode")),
        "target_scope": feedback.get("target_scope", limits.get("target_scope")),
        "coverage_scope": feedback.get("coverage_scope", "all_complete_samples"),
        "corpus_coverage_scope": feedback.get("corpus_coverage_scope"),
        "all_sample_pc_coverage": int(feedback.get("total_coverage", 0) or 0),
        "all_sample_target_pc_coverage": int(feedback.get("target_pc_coverage", 0) or 0),
        "corpus_pc_coverage": int(feedback.get("corpus_pc_coverage", 0) or 0),
        "corpus_target_pc_coverage": int(
            feedback.get("corpus_target_pc_coverage", 0) or 0),
        "corpus_target_pc_coverage_present": coverage_present,
        "ext4_pc_coverage": int(feedback.get("ext4_pc_coverage", 0) or 0),
        "jbd2_pc_coverage": int(feedback.get("jbd2_pc_coverage", 0) or 0),
        "generated_syscalls": int(report.get("generated_syscalls", 0) or 0),
        "native_verified": int(report.get("native_verified", 0) or 0),
        "rootfs_sha256": (campaign.get("dependencies", {}) or {}).get("rootfs_sha256"),
        "kernel_sha256": (campaign.get("build_sha256", {}) or {}).get("kernel.bzImage"),
        "regular_image_sha256": (campaign.get("start_images", {}) or {}).get("regular", {}).get("sha256"),
        "target_kind": limits.get("target_kind", "all"),
        "seed": limits.get("seed"),
        "syzkaller_calls": limits.get("syz_calls"),
        "resource_valid_every": limits.get("resource_valid_every"),
        "fresh_generate_every": limits.get("fresh_generate_every"),
        "resource_mutation_mode": limits.get("resource_mutation_mode"),
        "resource_seed_refresh_every": limits.get("resource_seed_refresh_every"),
        "feedback_edge_scope": feedback.get("feedback_edge_scope"),
        "scheduler_policy": limits.get("scheduler_policy"),
        "mutation_budget_policy": limits.get("mutation_budget_policy"),
        "requested_target_scope": target_scope,
        "duration_seconds": limits.get("duration_seconds"),
        "segments": limits.get("segments"),
        "segment_seconds": limits.get("segment_seconds"),
        "max_batches": limits.get("max_batches"),
        "batch_timeout": limits.get("batch_timeout"),
        "workers": limits.get("workers"),
        "random_fault_injection": bool(limits.get("random_fault_injection")),
        "random_fault_probability_ppm": limits.get("random_fault_probability_ppm"),
        "random_fault_operations": limits.get("random_fault_operations"),
        "random_fault_errnos": limits.get("random_fault_errnos"),
        "fault_isolation": limits.get("fault_isolation"),
        "memory_mb": limits.get("memory_mb"),
        "guest_memory_mb": limits.get("guest_memory_mb"),
        "cpus": limits.get("cpus"),
        "buffer_kb": limits.get("buffer_kb"),
        "max_trace_mb": limits.get("max_trace_mb"),
        "max_run_mb": limits.get("max_run_mb"),
        "min_free_mb": limits.get("min_free_mb"),
        "strict_kcov_feedback": limits.get("strict_kcov_feedback"),
        "effect_aware_admission": limits.get("effect_aware_admission"),
        "require_target_success": limits.get("require_target_success"),
    }


def latest_upstream_coverage(run: pathlib.Path) -> tuple[dict[str, Any], str | None]:
    candidates = [run / "coverage-checkpoints/final.json"]
    candidates.extend(sorted((run / "coverage-checkpoints").glob("*.json"), reverse=True)
                      if (run / "coverage-checkpoints").is_dir() else [])
    candidates.append(run / "coverage-initial.json")
    seen: set[pathlib.Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        data = load(path)
        if isinstance(data, dict) and "target_ext4_jbd2_pc" in data:
            return data, str(path)
    return {}, None


def count_executor_failures(path: pathlib.Path) -> int:
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8", errors="replace") as stream:
        return sum("SYZFAIL:" in line for line in stream)


def upstream_result(run: pathlib.Path) -> dict[str, Any]:
    metadata = load(run / "run-metadata.json", {}) or {}
    coverage, coverage_path = latest_upstream_coverage(run)
    return {
        "run_dir": str(run),
        "status": metadata.get("status"),
        "manager_exit_code": metadata.get("manager_exit_code"),
        "executor_failure_count": count_executor_failures(run / "manager.log"),
        "stopped_after_duration": bool(metadata.get("stopped_after_duration")),
        "coverage_endpoint_ready": bool(metadata.get("coverage_endpoint_ready")),
        "coverage_path": coverage_path,
        "final_coverage_checkpoint": coverage_path == str(run / "coverage-checkpoints/final.json"),
        "coverage_scope": coverage.get("coverage_scope", "manager_corpus"),
        "coverage_source": coverage.get("source", metadata.get("coverage_source")),
        "all_kernel_pc": int(coverage.get("all_kernel_pc", 0) or 0),
        "ext4_pc": int(coverage.get("ext4_pc", 0) or 0),
        "jbd2_pc": int(coverage.get("jbd2_pc", 0) or 0),
        "target_pc_coverage": int(coverage.get("target_ext4_jbd2_pc", 0) or 0),
        "target_fraction": float(coverage.get("target_fraction", 0.0) or 0.0),
        "requested_syscall_count": int(metadata.get("requested_syscalls", 0) or 0),
        "actual_enabled_syscall_count": metadata.get("actual_enabled_syscall_count"),
        "available_syscall_count": metadata.get("available_syscall_count"),
        # Keep the historical field for schema-1 consumers, but expose the
        # two independent notions explicitly.  syz-manager has no numeric
        # RNG seed option; an optional .syz program seed can still be loaded.
        "seed_applied": bool(metadata.get("seed_applied")),
        "random_seed_applied": bool(metadata.get("seed_applied")),
        "seed_program": metadata.get("seed_program"),
        "seed_program_applied": bool(metadata.get("seed_program_applied")),
        "seed_program_loaded_count": int(metadata.get("seed_program_loaded_count", 0) or 0),
        "seed_program_as_corpus": bool(metadata.get("seed_program_as_corpus")),
        "initial_corpus_program_count": int(metadata.get("initial_corpus_program_count", 0) or 0),
        "manager_execution_activity_observed_after_seed_load": bool(
            metadata.get("manager_execution_activity_observed_after_seed_load",
                         metadata.get("seed_execution_activity_observed"))),
        # None means a legacy run predating the independent executor evidence
        # artifact. New runs always write a boolean and are held to that gate.
        "target_filesystem_execution_verified": metadata.get(
            "target_filesystem_execution_verified"),
        "seed_execution_evidence": metadata.get("seed_execution_evidence", ""),
        "seed_execution_evidence_status": metadata.get("seed_execution_evidence_status", "missing"),
        "target_ext4_image_sha256": metadata.get("target_ext4_image_sha256"),
        "rootfs_backing": metadata.get("rootfs_backing"),
        "rootfs_backing_sha256": sha256(pathlib.Path(metadata["rootfs_backing"]))
        if metadata.get("rootfs_backing") else None,
        "kernel_sha256": sha256(pathlib.Path(metadata["kernel"]))
        if metadata.get("kernel") else None,
    }


def build_report(custom: pathlib.Path, upstream: pathlib.Path, target_scope: str) -> dict[str, Any]:
    custom_data = custom_result(custom, target_scope)
    upstream_data = upstream_result(upstream)
    warnings = []
    if custom_data["target_scope"] != target_scope:
        warnings.append("custom_target_scope_mismatch")
    if custom_data["corpus_coverage_scope"] != "admitted_corpus":
        warnings.append("custom_corpus_metric_missing_use_all_sample_as_diagnostic")
    if not custom_data["corpus_target_pc_coverage_present"]:
        warnings.append("custom_target_corpus_coverage_missing")
    custom_stop_is_expected = (
        custom_data["stop_reason"] == "duration_limit" or
        (custom_data["stop_reason"] == "syscall_budget" and custom_data["requested_target_syscalls"] > 0)
    )
    if (custom_data["status"] != "complete" or not custom_stop_is_expected or
            not custom_data["coverage_complete"] or
            not custom_data["kcov_feedback_complete"]):
        warnings.append("custom_coverage_collection_incomplete")
    if upstream_data["coverage_source"] != "rawcover":
        warnings.append("upstream_not_using_rawcover")
    if (upstream_data["status"] != "passed" or upstream_data["manager_exit_code"] != 0 or
            not upstream_data["stopped_after_duration"] or not upstream_data["coverage_endpoint_ready"] or
            not upstream_data["final_coverage_checkpoint"]):
        warnings.append("upstream_coverage_collection_incomplete")
    if upstream_data["executor_failure_count"]:
        warnings.append("upstream_executor_failure_observed")
    if (upstream_data["final_coverage_checkpoint"] and
            (upstream_data["all_kernel_pc"] <= 0 or upstream_data["target_pc_coverage"] <= 0)):
        warnings.append("upstream_coverage_data_empty")
    # A successful manager process with only generic PCs does not establish
    # that the seeded ext4 workload executed.  Keep the numeric zero for
    # diagnostics, but make the comparison incomplete so it cannot be read as
    # a native-vs-custom coverage result.
    if upstream_data["target_filesystem_execution_verified"] is None:
        # Schema-1 compatibility for reports produced before the evidence
        # artifact was added; current runs never take this path.
        upstream_seed_execution_verified = (
            upstream_data["seed_program_applied"] and
            upstream_data["manager_execution_activity_observed_after_seed_load"] and
            upstream_data["final_coverage_checkpoint"] and
            upstream_data["target_pc_coverage"] > 0)
    else:
        upstream_seed_execution_verified = (
            upstream_data["seed_program_applied"] and
            upstream_data["manager_execution_activity_observed_after_seed_load"] and
            bool(upstream_data["target_filesystem_execution_verified"])
        )
    if ((upstream_data["final_coverage_checkpoint"] and
         upstream_data["all_kernel_pc"] > 0 and
         upstream_data["target_pc_coverage"] == 0) or
            (upstream_data["seed_program"] and not upstream_seed_execution_verified)):
        warnings.append("inconclusive_upstream_target_execution")
    if not upstream_data["random_seed_applied"]:
        warnings.append("upstream_random_seed_not_applied")
    if upstream_data["seed_program"] and not upstream_data["seed_program_applied"]:
        warnings.append("upstream_seed_program_not_applied")
    if (upstream_data["actual_enabled_syscall_count"] is not None and
            upstream_data["actual_enabled_syscall_count"] != upstream_data["requested_syscall_count"]):
        warnings.append("upstream_transitive_syscall_closure_differs")
    # Hashes describe shared backing artifacts, while target workload state differs.
    if not custom_data.get("kernel_sha256") or not upstream_data.get("kernel_sha256"):
        warnings.append("kernel_hash_missing")
    elif custom_data["kernel_sha256"] != upstream_data["kernel_sha256"]:
        warnings.append("kernel_mismatch")
    if not custom_data.get("rootfs_sha256") or not upstream_data.get("rootfs_backing_sha256"):
        warnings.append("rootfs_hash_missing")
    elif custom_data["rootfs_sha256"] != upstream_data["rootfs_backing_sha256"]:
        warnings.append("rootfs_mismatch")
    warnings.extend(("target_device_scope_differs_custom_vdb_vdc_vdd_upstream_seed_mount_image",
                     "requested_syscall_sets_not_aligned",
                     "upstream_manager_corpus_vs_custom_admitted_corpus_workload_differs"))
    custom_metric = custom_data["corpus_target_pc_coverage"]
    upstream_metric = upstream_data["target_pc_coverage"]
    return {
        "schema": 1,
        "comparison": "custom_feedback_vs_upstream_syz_manager",
        "metric": "target_ext4_jbd2_pc",
        "target_scope": target_scope,
        "custom": custom_data,
        "upstream": upstream_data,
        "aligned_corpus_target_pc": {
            "custom_admitted_corpus": custom_metric,
            "upstream_manager_corpus": upstream_metric,
            "delta_custom_minus_upstream": custom_metric - upstream_metric,
        },
        "measurement_complete": not any(
            warning.endswith("coverage_collection_incomplete") or
            warning in ("custom_target_corpus_coverage_missing",
                        "inconclusive_upstream_target_execution",
                        "upstream_coverage_data_empty",
                        "upstream_executor_failure_observed")
            for warning in warnings),
        "fairness": {
            "same_kernel": bool(custom_data.get("kernel_sha256") and upstream_data.get("kernel_sha256") and
                                 custom_data["kernel_sha256"] == upstream_data["kernel_sha256"]),
            "same_rootfs_backing": bool(custom_data.get("rootfs_sha256") and
                                         upstream_data.get("rootfs_backing_sha256") and
                                         custom_data["rootfs_sha256"] == upstream_data["rootfs_backing_sha256"]),
            "same_seed": bool(upstream_data["seed_applied"]),
            "same_program_seed": False,
            "upstream_seed_program_applied": bool(upstream_data["seed_program_applied"]),
            "same_syscall_set": False,
            "same_target_device": False,
            "same_execution_scope": False,
            "strictly_causal_comparison": False,
        },
        "warnings": warnings,
    }


def load_operation_validation(path: pathlib.Path | None) -> dict[str, Any]:
    if path is None:
        return {"status": "missing", "evidence_complete": False,
                "operations": {"target_success_total": 0}}
    report = load(path, {}) or {}
    operations = report.get("operations") or {}
    return {
        "path": str(path),
        "status": report.get("status", "invalid"),
        "evidence_readable": bool(report.get("evidence_readable")),
        "evidence_complete": bool(report.get("evidence_complete")),
        "target_device": report.get("target_device"),
        "target_success_total": int(operations.get("target_success_total", 0) or 0),
        "data_success": int(((operations.get("data") or {}).get(
            "verified_successful_on_regular_unrepaired", 0)) or 0),
        "namespace_success": int(((operations.get("namespace") or {}).get(
            "verified_successful_on_regular_unrepaired", 0)) or 0),
        "required_data_and_namespace_categories_verified": bool(
            operations.get("required_data_and_namespace_categories_verified")),
        "errors": report.get("errors", []),
        "warnings": report.get("warnings", []),
    }


def build_three_arm_report(native: pathlib.Path, pc_only: pathlib.Path,
                           full_feedback: pathlib.Path, target_scope: str,
                           pc_only_operations: pathlib.Path | None,
                           full_operations: pathlib.Path | None,
                           expected_full_feedback_mode: str = "pc_edge_semantic") -> dict[str, Any]:
    native_data = upstream_result(native)
    pc_data = custom_result(pc_only, target_scope)
    full_data = custom_result(full_feedback, target_scope)
    pc_validation = load_operation_validation(pc_only_operations)
    full_validation = load_operation_validation(full_operations)
    warnings: list[str] = []

    for label, result in (("pc_only", pc_data), ("full_feedback", full_data)):
        if result["target_scope"] != target_scope:
            warnings.append(label + "_target_scope_mismatch")
        if result["target_kind"] != "regular":
            warnings.append(label + "_not_regular_only")
        if result["corpus_coverage_scope"] != "admitted_corpus" or not result["corpus_target_pc_coverage_present"]:
            warnings.append(label + "_admitted_corpus_target_coverage_missing")
        expected_stop = (result["stop_reason"] == "duration_limit" or
                         (result["stop_reason"] == "syscall_budget" and result["requested_target_syscalls"] > 0))
        if (result["status"] != "complete" or not expected_stop or
                not result["coverage_complete"] or not result["kcov_feedback_complete"]):
            warnings.append(label + "_coverage_collection_incomplete")

    if native_data["coverage_source"] != "rawcover":
        warnings.append("native_not_using_rawcover")
    if (native_data["status"] != "passed" or native_data["manager_exit_code"] != 0 or
            not native_data["stopped_after_duration"] or not native_data["coverage_endpoint_ready"] or
            not native_data["final_coverage_checkpoint"]):
        warnings.append("native_coverage_collection_incomplete")
    if native_data["executor_failure_count"]:
        warnings.append("native_executor_failure_observed")
    if (native_data["final_coverage_checkpoint"] and
            (native_data["all_kernel_pc"] <= 0 or native_data["target_pc_coverage"] <= 0)):
        warnings.append("native_coverage_data_empty")
    if not native_data["seed_program_applied"]:
        warnings.append("native_seed_program_not_loaded")
    if not native_data["manager_execution_activity_observed_after_seed_load"]:
        warnings.append("native_manager_execution_activity_after_seed_load_not_observed")
    evidence_ready = (native_data["target_filesystem_execution_verified"]
                      if native_data["target_filesystem_execution_verified"] is not None
                      else (native_data["target_pc_coverage"] > 0))
    native_workload_ready = (native_data["seed_program_applied"] and
                             native_data["manager_execution_activity_observed_after_seed_load"] and
                             evidence_ready and native_data["final_coverage_checkpoint"])
    native_target_coverage_ready = (native_data["all_kernel_pc"] > 0 and
                                    native_data["target_pc_coverage"] > 0)
    # Executor evidence establishes workload execution; KCOV counts establish
    # that the requested coverage metric was actually collected. Keep these
    # gates independent so a successful seed cannot turn empty coverage into
    # a complete measurement.
    if ((native_data["seed_program"] or native_data["seed_program_applied"]) and
            not native_workload_ready):
        warnings.append("inconclusive_native_target_execution")

    images = [native_data.get("target_ext4_image_sha256"),
              pc_data.get("regular_image_sha256"), full_data.get("regular_image_sha256")]
    same_image = bool(images[0] and images[1] and images[2] and len(set(images)) == 1)
    if not same_image:
        warnings.append("target_ext4_image_mismatch")
    kernel_hashes = [native_data.get("kernel_sha256"), pc_data.get("kernel_sha256"),
                     full_data.get("kernel_sha256")]
    same_kernel = bool(all(kernel_hashes) and len(set(kernel_hashes)) == 1)
    if not same_kernel:
        warnings.append("kernel_mismatch_or_hash_missing")
    rootfs_hashes = [native_data.get("rootfs_backing_sha256"), pc_data.get("rootfs_sha256"),
                     full_data.get("rootfs_sha256")]
    same_rootfs = bool(all(rootfs_hashes) and len(set(rootfs_hashes)) == 1)
    if not same_rootfs:
        warnings.append("rootfs_mismatch_or_hash_missing")

    custom_controls = ("seed", "target_kind", "syzkaller_calls", "resource_valid_every",
                       "fresh_generate_every", "resource_mutation_mode", "resource_seed_refresh_every",
                       "feedback_edge_scope", "scheduler_policy", "mutation_budget_policy",
                       "target_scope", "rootfs_sha256", "kernel_sha256", "regular_image_sha256",
                       "requested_target_syscalls", "duration_seconds", "segments",
                       "segment_seconds", "max_batches", "batch_timeout", "workers",
                       "memory_mb", "guest_memory_mb", "cpus", "buffer_kb",
                       "max_trace_mb", "max_run_mb", "min_free_mb", "strict_kcov_feedback",
                       "effect_aware_admission", "require_target_success",
                       "random_fault_injection", "random_fault_probability_ppm",
                       "random_fault_operations", "random_fault_errnos", "fault_isolation")
    custom_settings_aligned = all(pc_data.get(key) == full_data.get(key)
                                  for key in custom_controls)
    if not custom_settings_aligned:
        warnings.append("custom_feedback_arms_configuration_mismatch")
    if pc_data.get("feedback_mode") != "pc" or full_data.get("feedback_mode") != expected_full_feedback_mode:
        warnings.append("custom_feedback_mode_mismatch")
    if not pc_validation["evidence_complete"] or not pc_validation["evidence_readable"] or not pc_validation["target_success_total"]:
        warnings.append("pc_only_target_ext4_operations_unverified")
    if not full_validation["evidence_complete"] or not full_validation["evidence_readable"] or not full_validation["target_success_total"]:
        warnings.append("full_feedback_target_ext4_operations_unverified")
    if native_data["actual_enabled_syscall_count"] != native_data["requested_syscall_count"]:
        warnings.append("native_transitive_syscall_closure_differs")

    coverage_complete = not any(item.endswith("coverage_collection_incomplete") or
                                 item.endswith("admitted_corpus_target_coverage_missing") or
                                 item in ("native_coverage_collection_incomplete",
                                          "native_coverage_data_empty",
                                          "native_executor_failure_observed")
                                 for item in warnings)
    operations_verified = (pc_validation["evidence_complete"] and pc_validation["evidence_readable"] and
                           pc_validation["target_success_total"] > 0 and
                           pc_validation.get("data_success", 0) > 0 and
                           pc_validation.get("namespace_success", 0) > 0 and
                           full_validation["evidence_complete"] and full_validation["evidence_readable"] and
                           full_validation["target_success_total"] > 0 and
                           full_validation.get("data_success", 0) > 0 and
                           full_validation.get("namespace_success", 0) > 0)
    feedback_modes_aligned = (pc_data.get("feedback_mode") == "pc" and
                              full_data.get("feedback_mode") == expected_full_feedback_mode)
    return {
        "schema": 1,
        "comparison": "native_vs_pc_only_vs_full_feedback",
        "metric": "target_ext4_jbd2_pc",
        "target_scope": target_scope,
        "arms": {
            "native": native_data,
            "pc_only": {**pc_data, "target_ext4_operations": pc_validation},
            "full_feedback": {**full_data, "target_ext4_operations": full_validation},
        },
        "coverage": {
            "native_manager_corpus": native_data["target_pc_coverage"],
            "pc_only_admitted_corpus": pc_data["corpus_target_pc_coverage"],
            "full_feedback_admitted_corpus": full_data["corpus_target_pc_coverage"],
            "feedback_delta_full_minus_pc_only": (
                full_data["corpus_target_pc_coverage"] - pc_data["corpus_target_pc_coverage"]),
        },
        "measurement_complete": (coverage_complete and operations_verified and same_image and
                                  same_kernel and same_rootfs and custom_settings_aligned and
                                  feedback_modes_aligned and native_workload_ready and
                                  native_target_coverage_ready),
        "fairness": {
            "same_target_ext4_image_bytes": same_image,
            "same_kernel": same_kernel,
            "same_rootfs_backing": same_rootfs,
            "custom_feedback_arm_settings_aligned_except_feedback_mode": custom_settings_aligned,
            "same_custom_program_seed": pc_data.get("seed") == full_data.get("seed"),
            "successful_unrepaired_target_ext4_operations_verified_for_both_custom_arms": operations_verified,
            "native_seed_loaded": bool(native_data["seed_program_applied"]),
            "native_manager_execution_activity_observed_after_seed_load": bool(
                native_data["manager_execution_activity_observed_after_seed_load"]),
            "native_seed_and_execution_gate_satisfied": native_workload_ready,
            "native_target_coverage_gate_satisfied": native_target_coverage_ready,
            "native_per_syscall_return_and_device_evidence_available": False,
            "native_syscall_set_matches_custom": False,
            "target_filesystem_state_lifetime_matches": False,
            "strictly_causal_native_comparison": False,
            "feedback_ablation_settings_aligned": custom_settings_aligned and feedback_modes_aligned,
        },
        "limitations": [
            "syz-manager reports seed corpus loading and execution activity, but not per-syscall return/device attribution.",
            "syz-manager enables a transitive syscall closure that differs from the custom generator's syscall set.",
            "Native manager-corpus coverage and custom admitted-corpus coverage have distinct corpus admission semantics.",
            "Native syz_mount_image recreates the embedded image per seed execution, while the custom regular block image persists across segments.",
        ],
        "warnings": sorted(set(warnings)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--custom-run", type=pathlib.Path, required=True)
    parser.add_argument("--upstream-run", type=pathlib.Path, required=True)
    parser.add_argument("--pc-only-run", type=pathlib.Path)
    parser.add_argument("--full-feedback-run", type=pathlib.Path)
    parser.add_argument("--pc-only-operations", type=pathlib.Path)
    parser.add_argument("--full-feedback-operations", type=pathlib.Path)
    parser.add_argument("--full-feedback-mode",
                        choices=("pc_edge", "pc_semantic", "pc_edge_semantic"),
                        default="pc_edge_semantic")
    parser.add_argument("--target-scope", choices=("all", "ext4_jbd2"), default="ext4_jbd2")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    if args.pc_only_run or args.full_feedback_run:
        if not args.pc_only_run or not args.full_feedback_run:
            parser.error("--pc-only-run and --full-feedback-run must be supplied together")
        report = build_three_arm_report(
            args.upstream_run, args.pc_only_run, args.full_feedback_run, args.target_scope,
            args.pc_only_operations, args.full_feedback_operations,
            args.full_feedback_mode)
    else:
        report = build_report(args.custom_run, args.upstream_run, args.target_scope)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("measurement_complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
