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
    return {
        "run_dir": str(run),
        "status": campaign.get("status"),
        "passed": bool(report.get("passed")),
        "coverage_complete": bool(report.get("coverage_complete")),
        "kcov_feedback_complete": bool(report.get("kcov_feedback_complete")),
        "feedback_mode": limits.get("feedback_mode", feedback.get("feedback_mode")),
        "target_scope": feedback.get("target_scope", limits.get("target_scope")),
        "coverage_scope": feedback.get("coverage_scope", "all_complete_samples"),
        "corpus_coverage_scope": feedback.get("corpus_coverage_scope"),
        "all_sample_pc_coverage": int(feedback.get("total_coverage", 0) or 0),
        "all_sample_target_pc_coverage": int(feedback.get("target_pc_coverage", 0) or 0),
        "corpus_pc_coverage": int(feedback.get("corpus_pc_coverage", 0) or 0),
        "corpus_target_pc_coverage": int(
            feedback.get("corpus_target_pc_coverage", 0) or 0),
        "ext4_pc_coverage": int(feedback.get("ext4_pc_coverage", 0) or 0),
        "jbd2_pc_coverage": int(feedback.get("jbd2_pc_coverage", 0) or 0),
        "generated_syscalls": int(report.get("generated_syscalls", 0) or 0),
        "native_verified": int(report.get("native_verified", 0) or 0),
        "rootfs_sha256": (campaign.get("dependencies", {}) or {}).get("rootfs_sha256"),
        "kernel_sha256": (campaign.get("build_sha256", {}) or {}).get("kernel.bzImage"),
        "requested_target_scope": target_scope,
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


def upstream_result(run: pathlib.Path) -> dict[str, Any]:
    metadata = load(run / "run-metadata.json", {}) or {}
    coverage, coverage_path = latest_upstream_coverage(run)
    return {
        "run_dir": str(run),
        "status": metadata.get("status"),
        "manager_exit_code": metadata.get("manager_exit_code"),
        "stopped_after_duration": bool(metadata.get("stopped_after_duration")),
        "coverage_endpoint_ready": bool(metadata.get("coverage_endpoint_ready")),
        "coverage_path": coverage_path,
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
    if upstream_data["coverage_source"] != "rawcover":
        warnings.append("upstream_not_using_rawcover")
    if not upstream_data["random_seed_applied"]:
        warnings.append("upstream_random_seed_not_applied")
    if upstream_data["seed_program"] and not upstream_data["seed_program_applied"]:
        warnings.append("upstream_seed_program_not_applied")
    if (upstream_data["actual_enabled_syscall_count"] is not None and
            upstream_data["actual_enabled_syscall_count"] != upstream_data["requested_syscall_count"]):
        warnings.append("upstream_transitive_syscall_closure_differs")
    # Both runners use the same kernel file and backing rootfs hash, but the
    # guest workload currently reaches different devices and path state.
    if (custom_data.get("kernel_sha256") and upstream_data.get("kernel_sha256") and
            custom_data["kernel_sha256"] != upstream_data["kernel_sha256"]):
        warnings.append("kernel_mismatch")
    if (custom_data.get("rootfs_sha256") and upstream_data.get("rootfs_backing_sha256") and
            custom_data["rootfs_sha256"] != upstream_data["rootfs_backing_sha256"]):
        warnings.append("rootfs_mismatch")
    warnings.extend(("target_device_scope_differs_custom_vdb_vdc_vdd_upstream_vda",
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
        "fairness": {
            "same_kernel": "kernel_mismatch" not in warnings,
            "same_rootfs_backing": "rootfs_mismatch" not in warnings,
            "same_seed": bool(upstream_data["seed_applied"]),
            "same_program_seed": bool(upstream_data["seed_program_applied"]),
            "same_syscall_set": "upstream_transitive_syscall_closure_differs" not in warnings,
            "same_target_device": False,
            "same_execution_scope": False,
            "strictly_causal_comparison": False,
        },
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--custom-run", type=pathlib.Path, required=True)
    parser.add_argument("--upstream-run", type=pathlib.Path, required=True)
    parser.add_argument("--target-scope", choices=("all", "ext4_jbd2"), default="ext4_jbd2")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    report = build_report(args.custom_run, args.upstream_run, args.target_scope)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
