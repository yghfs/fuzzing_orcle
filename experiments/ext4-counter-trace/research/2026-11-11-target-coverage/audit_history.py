#!/usr/bin/env python3
"""Summarize existing campaigns without changing their historical artifacts.

Example (run from the repository root):
  python3 experiments/ext4-counter-trace/research/2026-11-11-target-coverage/audit_history.py \
    --pc-campaign experiments/ext4-counter-trace/results/campaign-9chz7m3j \
    --full-campaign experiments/ext4-counter-trace/results/campaign-fyubc_lv \
    --output experiments/ext4-counter-trace/research/2026-11-11-target-coverage/history-audit.json
"""

import argparse
from collections import Counter, defaultdict
import gzip
import json
import math
from pathlib import Path
import re


def read_json(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def source_path(path):
    """Keep repository-local references portable when invoked from its root."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path.resolve())


def take(mapping, keys):
    return {key: mapping.get(key) for key in keys}


def audit_campaign(path):
    campaign = read_json(path / "campaign.json")
    report = read_json(path / "campaign-report.json")
    coverage = read_json(path / "feedback/coverage.json")
    scheduler = read_json(path / "feedback/scheduler.json")
    performance = read_json(path / "performance-report.json")
    with gzip.open(path / "segment-summary.jsonl.gz", "rt", encoding="utf-8") as stream:
        segments = {row["segment_id"]: row for row in map(json.loads, stream)}

    tasks = defaultdict(Counter)
    resource_templates = defaultdict(Counter)
    all_templates = Counter()
    points = []
    failures = []
    missing = []
    last_novelty = {}
    start_ns = campaign["collection_start_wall_ns"]
    failed_ids = {
        int(match.group(1))
        for issue in report.get("issues", [])
        if (match := re.fullmatch(r"segment-(\d+):analysis_failed", issue))
    }
    fault_ids = {
        row["segment_id"] for row in report.get("fault_injection", {}).get("hit_segments", [])
    }

    for segment_id, segment in sorted(segments.items()):
        directory = path / "segments" / f"segment-{segment_id:06d}"
        elapsed = (segment["end_wall_ns"] - start_ns) / 1e9
        current_coverage_path = directory / "coverage.json"
        if not current_coverage_path.exists():
            missing.append(source_path(current_coverage_path))
            continue
        current = read_json(current_coverage_path)
        if current.get("new_target_pc_coverage", 0):
            last_novelty["raw_target_pc"] = {
                "segment_id": segment_id, "elapsed_seconds": round(elapsed, 3)
            }

        for program in current.get("programs", []):
            metadata_path = directory / "syz-programs" / program["program"] / "program.json"
            if not metadata_path.exists():
                missing.append(source_path(metadata_path))
                continue
            metadata = read_json(metadata_path)
            task = metadata.get("task", metadata.get("method", "unknown"))
            template = metadata.get("resource_template", "unknown")
            all_templates[template] += 1
            buckets = [tasks[task]]
            if task.startswith("resource_"):
                buckets.append(resource_templates[template])
            for bucket in buckets:
                bucket["programs"] += 1
                bucket["admitted_programs"] += bool(program.get("admitted"))
                bucket["raw_new_target_pcs"] += program.get("raw_new_target_pcs", 0)
                bucket["effect_new_target_pcs"] += program.get("effect_new_target_pcs", 0)
                bucket["new_semantic_features"] += program.get("new_semantic_features", 0)
                for call in program.get("calls", []):
                    bucket["recorded_calls"] += 1
                    bucket[call.get("effect", "unclassified")] += 1
            for field, label in [("effect_new_target_pcs", "effect_target_pc"),
                                 ("new_semantic_features", "semantic")]:
                if program.get(field, 0):
                    last_novelty[label] = {
                        "segment_id": segment_id, "elapsed_seconds": round(elapsed, 3)
                    }

        feedback_path = directory / "feedback.json"
        if feedback_path.exists():
            cumulative = read_json(feedback_path)["campaign_summary"]
            points.append({
                "segment_id": segment_id,
                "elapsed_seconds": round(elapsed, 3),
                **take(cumulative, ["complete_samples", "target_pc_coverage",
                                    "effect_target_pc_coverage", "corpus_target_pc_coverage",
                                    "total_semantic_features"]),
            })
        else:
            missing.append(source_path(feedback_path))

        if segment_id in failed_ids:
            lifecycle_path = directory / "analysis/lifecycle/report.json.gz"
            lifecycle = read_json(lifecycle_path)
            failure = {
                "segment_id": segment_id,
                "immediately_after_fault": segment_id - 1 in fault_ids,
                "filesystem_reset_before": segment.get("filesystem_reset_before"),
                "filesystem_reset_completed": segment.get("filesystem_reset_completed"),
                "blockers": lifecycle.get("blockers", []),
                "loss_stats": lifecycle.get("loss_stats", {}),
                "source": source_path(lifecycle_path),
                "invalid_runs": [],
            }
            for run in lifecycle.get("runs", []):
                if run.get("trajectory_integrity") is False:
                    failure["invalid_runs"].append(take(run, [
                        "name", "epoch", "dev", "start_seq", "end_seq", "integrity_issues"
                    ]))
            kind = segment["kind"]
            failure["new_raw_start"] = read_json(directory / f"{kind}-start.json")
            chain = read_json(directory / "analysis/chain-manifest.json")
            failure["carry"] = take(chain, ["carry_available", "carry_source"])
            failures.append(failure)

    hours = []
    duration = report["collection_elapsed_seconds"]
    for hour in range(1, math.floor(duration / 3600) + 1):
        finished = [point for point in points if point["elapsed_seconds"] <= hour * 3600]
        if finished:
            hours.append({"checkpoint_hour": hour, **finished[-1]})

    parent_yields = defaultdict(Counter)
    for parent in scheduler["parents"]:
        bucket = parent_yields[parent.get("admission_kind", "unknown")]
        bucket["parents"] += 1
        bucket["resource_parents"] += bool(parent.get("resource_parent"))
        for key in ["selections", "child_executions", "child_new_pcs", "child_new_ext4_pcs",
                    "child_new_edges", "child_new_semantic_features"]:
            bucket[key] += parent.get(key, 0)

    effects = coverage["call_effect_counts"]
    total_effect_calls = sum(effects.values())
    transitions = report["exploration"]["independent_transition_calls"]
    return {
        "source": source_path(path),
        "sources": {
            "aggregate": "campaign-report.json",
            "coverage": "feedback/coverage.json",
            "scheduler": "feedback/scheduler.json",
            "performance": "performance-report.json",
            "segments": "segment-summary.jsonl.gz",
            "per_program": "segments/segment-*/syz-programs/*/program.json",
            "per_segment_coverage": "segments/segment-*/coverage.json",
            "per_segment_feedback": "segments/segment-*/feedback.json",
        },
        "settings": take(campaign["limits"], [
            "duration_seconds", "seed", "feedback_mode", "scheduler_policy", "target_scope",
            "target_kind", "scenario", "syz_calls", "resource_valid_every", "fresh_generate_every",
            "random_fault_injection", "random_fault_probability_ppm", "fault_isolation",
            "qemu_cpu", "cpus", "guest_memory_mb",
        ]),
        "validity": take(report, ["status", "passed", "raw_coverage_complete",
                                   "coverage_complete", "kcov_feedback_complete",
                                   "counter_validation_passed", "source_attribution_complete"]),
        "coverage": take(coverage, [
            "expected_samples", "complete_samples", "saturated_samples", "invalid_samples",
            "missing_samples", "ext4_pc_coverage", "jbd2_pc_coverage", "target_pc_coverage",
            "effect_target_pc_coverage", "corpus_target_pc_coverage", "effect_target_edge_coverage",
            "target_edge_coverage", "corpus_size", "total_semantic_features", "joint_feedback",
        ]),
        "effect_counts": effects,
        "effect_percent_of_recorded_calls": {
            key: round(100 * value / total_effect_calls, 3) for key, value in effects.items()
        },
        "program_tasks": dict(tasks),
        "all_template_counts": dict(all_templates),
        "resource_templates": dict(resource_templates),
        "parent_yields_by_admission_kind": dict(parent_yields),
        "hourly_curve": hours,
        "final_curve_point": points[-1] if points else None,
        "last_novelty": last_novelty,
        "execution": {
            **take(report, ["collection_elapsed_seconds", "generated_syscalls", "native_verified",
                            "native_success", "fd_repairs", "event_sources", "classification_counts"]),
            "independent_transition_calls": transitions,
            "verified_syscalls_per_collection_hour": round(report["native_verified"] / duration * 3600, 3),
            "transitions_per_collection_hour": round(transitions / duration * 3600, 3),
            "syscall_stats": {
                name: take(stats, ["executions", "successful", "failed", "repaired",
                                   "independent_transition_calls", "errors"])
                for name, stats in report["exploration"]["syscall_stats"].items()
            },
        },
        "performance_stages": {
            name: take(stats, ["count", "total", "p50", "p95"])
            for name, stats in performance["stages"].items()
        },
        "fault_and_analysis": {
            "fault_segments": sorted(fault_ids),
            "analysis_failed_segments": sorted(failed_ids),
            "all_failures_immediately_follow_fault": bool(failed_ids) and
                all(segment_id - 1 in fault_ids for segment_id in failed_ids),
            "failures": failures,
        },
        "missing_audit_inputs": missing,
        "consistency_checks": {
            "segment_count_matches_expected_samples": len(segments) == coverage["expected_samples"],
            "scanned_program_count_matches_samples": sum(x["programs"] for x in tasks.values()) == coverage["samples"],
            "scanned_calls_match_aggregate_effects": sum(x["recorded_calls"] for x in tasks.values()) == total_effect_calls,
            "effect_novelty_sum_matches_final_target": sum(x["effect_new_target_pcs"] for x in tasks.values()) == coverage["effect_target_pc_coverage"],
            "raw_novelty_sum_matches_final_target": sum(x["raw_new_target_pcs"] for x in tasks.values()) == coverage["target_pc_coverage"],
            "semantic_novelty_sum_matches_final": sum(x["new_semantic_features"] for x in tasks.values()) == coverage["total_semantic_features"],
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pc-campaign", type=Path, required=True)
    parser.add_argument("--full-campaign", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Write summary here; default: standard output")
    args = parser.parse_args()
    if args.output:
        output = args.output.resolve()
        for historical in [args.pc_campaign, args.full_campaign]:
            if output == historical.resolve() or historical.resolve() in output.parents:
                parser.error("--output must be outside both historical campaign directories")
    arms = {"pc_only": audit_campaign(args.pc_campaign),
            "full_feedback": audit_campaign(args.full_campaign)}
    result = {
        "schema": 1,
        "audit_date": "2026-11-11",
        "arms": arms,
        "full_minus_pc_target_coverage": {
            key: arms["full_feedback"]["coverage"][key] - arms["pc_only"]["coverage"][key]
            for key in ["target_pc_coverage", "effect_target_pc_coverage", "corpus_target_pc_coverage"]
        },
        "limitations": [
            "This is a descriptive audit of one historical seed, not a causal efficacy comparison.",
            "PC counts use the historical symbol-based ext4/JBD2 scope, without a total target-PC denominator.",
            "target_fraction is not filesystem coverage percentage and is intentionally omitted.",
            "Novelty credit depends on execution order; template associations do not establish causality.",
            "Hourly checkpoints use segment end wall timestamps; the final point is reported separately.",
            "Performance quantiles are copied from the existing performance report; nested stages must not be summed.",
            "Semantic features are the historical implementation's features, not unique bugs or complete difference trajectories.",
            "KCOV completeness and formula lifecycle completeness are independent audit fields.",
            "The native syz-manager arm is not audited by this script; see the original comparison.json for its validity gates.",
        ],
    }
    rendered = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
