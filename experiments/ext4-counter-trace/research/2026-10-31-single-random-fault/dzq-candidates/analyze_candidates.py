#!/usr/bin/env python3
"""Read-only candidate audit for campaign-dzq91z1h.

The source campaign is immutable.  This script derives compact CSV/JSON
ledgers from the retained manifests, fault attribution, lifecycle reports,
snapshots, and final disk evidence.
"""
from __future__ import annotations

import collections
import csv
import gzip
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
BASE = ROOT / "experiments/ext4-counter-trace/results/campaign-dzq91z1h"
OUT = Path(__file__).resolve().parent
KINDS = ("regular", "bigalloc", "inline")


def read(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", errors="replace") as stream:
        return json.load(stream)


def write_json(name: str, value) -> None:
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_csv(name: str, rows: list[dict], fields: list[str] | None = None) -> None:
    if fields is None:
        fields = list(rows[0]) if rows else []
    with (OUT / name).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def trace_fault_event(folder: Path, expected: dict) -> tuple[bool, bool, int | None]:
    """Return (found, agrees, line) for the injected event in raw trace."""
    path = folder / "trace.txt.gz"
    if not path.is_file():
        path = folder / "trace.txt"
    if not path.is_file():
        return False, False, None
    found = []
    pattern = re.compile(r"\b([a-z_]+)=([^\s]+)")
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", errors="replace") as stream:
        for line_no, line in enumerate(stream, 1):
            if "ext4_fault_inject:" not in line or "injection=1" not in line:
                continue
            fields = dict(pattern.findall(line))
            if fields.get("pid") == str(expected.get("pid")) and fields.get("target_pid") == str(expected.get("target_pid")):
                found.append((line_no, fields))
    if len(found) != 1:
        return bool(found), False, found[0][0] if found else None
    line_no, actual = found[0]
    expected_fields = expected.get("fields") or {}
    expected_values = {
        "operation": expected.get("operation"),
        "errno": expected.get("errno"),
        "pid": expected.get("pid"),
        "target_pid": expected.get("target_pid"),
        "random_seed": expected_fields.get("random_seed"),
        "random_draw": expected.get("random_draw", expected_fields.get("random_draw")),
        "errno_choice_index": expected.get("errno_choice_index", expected_fields.get("errno_choice_index")),
        "errno_choice_draw": expected.get("errno_choice_draw", expected_fields.get("errno_choice_draw")),
    }
    agrees = all(str(actual.get(key)) == str(value) for key, value in expected_values.items())
    return True, agrees, line_no


def lifecycle_report(folder: Path):
    path = folder / "analysis/lifecycle/report.json.gz"
    if not path.is_file():
        path = folder / "analysis/lifecycle/report.json"
    return read(path) if path.is_file() else {}


def compact_detail(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value else ""


def main() -> None:
    report = read(BASE / "campaign-report.json")
    fault = report["fault_injection"]
    candidates = fault["candidates"]
    hit_by_segment = {item["segment_id"]: item for item in fault["hit_segments"]}
    manifests = {}
    folders = {}
    for path in sorted((BASE / "segments").glob("segment-*/manifest.json")):
        manifest = read(path)
        segment = manifest["segment_id"]
        manifests[segment] = manifest
        folders[segment] = path.parent

    candidate_by_segment = collections.defaultdict(list)
    for item in candidates:
        candidate_by_segment[item["segment_id"]].append(item)

    candidate_rows = []
    for ordinal, item in enumerate(candidates, 1):
        segment = item["segment_id"]
        manifest = manifests[segment]
        hit = hit_by_segment[segment]
        policy = manifest.get("fault_injection") or {}
        event = policy.get("event") or {}
        attribution = policy.get("attribution") or {}
        before = (policy.get("stable_observation") or {}).get("before") or {}
        after = (policy.get("stable_observation") or {}).get("after") or {}
        lifecycle = lifecycle_report(folders[segment])
        counts = lifecycle.get("classification_counts", {})
        if item["source"] == "lifecycle":
            classification = "experimental_operation_exit_only" if hit["admission"] == "experimental" else "admitted_operation_exit"
        elif item["source"] == "workload_exit":
            classification = "fault_cleanup_exit_90" if item.get("workload_exit") == 90 else "fault_cleanup_exit_other"
        else:
            classification = item["source"]
        candidate_rows.append({
            "ordinal": ordinal,
            "source": item.get("source"),
            "segment_id": segment,
            "kind": item.get("kind"),
            "operation": hit.get("operation"),
            "errno": hit.get("errno"),
            "admission": hit.get("admission"),
            "classification": classification,
            "guest_exit": manifest.get("guest_exit"),
            "segment_status": manifest.get("status"),
            "fault_expected_workload_exit": manifest.get("fault_expected_workload_exit", False),
            "attribution_status": attribution.get("status"),
            "native_syscall": (attribution.get("native_envelope") or {}).get("name"),
            "native_result": (attribution.get("native_envelope") or {}).get("raw_result"),
            "trace_event_verified": policy.get("errno_choice_verified"),
            "before_snapshot": before.get("status"),
            "after_snapshot": after.get("status"),
            "after_snapshot_reason": after.get("reason"),
            "after_observed_mismatches": after.get("observed_mismatches"),
            "after_namespace_mismatched": after.get("namespace_mismatched"),
            "after_relations_mismatches": after.get("relations_mismatches"),
            "persistent_mismatch": counts.get("persistent_mismatch", 0),
            "operation_exit_mismatch": counts.get("operation_exit_mismatch", 0),
            "candidate_details": compact_detail(item.get("details")),
            "evidence": item.get("evidence") or item.get("disk_evidence"),
        })

    hit_rows = []
    snapshot_rows = []
    lifecycle_episode_rows = []
    lifecycle_totals = collections.Counter()
    trace_found = trace_agree = 0
    for segment, hit in sorted(hit_by_segment.items()):
        manifest = manifests[segment]
        folder = folders[segment]
        policy = manifest.get("fault_injection") or {}
        event = policy.get("event") or {}
        attribution = policy.get("attribution") or {}
        native = attribution.get("native_envelope") or {}
        before = (policy.get("stable_observation") or {}).get("before") or {}
        after = (policy.get("stable_observation") or {}).get("after") or {}
        found, agrees, trace_line = trace_fault_event(folder, event)
        trace_found += found
        trace_agree += agrees
        lifecycle = lifecycle_report(folder)
        lifecycle_counts = lifecycle.get("classification_counts", {})
        lifecycle_totals.update(lifecycle_counts)
        if lifecycle_counts.get("operation_exit_mismatch"):
            for run in lifecycle.get("runs", []):
                for episode in run.get("episodes", []):
                    if episode.get("classification") != "operation_exit_mismatch":
                        continue
                    native_results = [item.get("raw_result") for item in episode.get("exit_evidence", [])
                                      if item.get("kind") == "native_syscall_return"]
                    lifecycle_episode_rows.append({
                        "segment_id": segment,
                        "kind": manifest.get("kind"),
                        "fault_operation": hit.get("operation"),
                        "fault_errno": hit.get("errno"),
                        "admission": hit.get("admission"),
                        "stream": episode.get("stream"),
                        "onset_point": episode.get("onset_point"),
                        "peak_gap": episode.get("peak_gap"),
                        "recovered_in_trace": episode.get("recovered_seq") is not None,
                        "recovered_seq": episode.get("recovered_seq"),
                        "native_evidence_count": len(native_results),
                        "native_results": compact_detail(native_results),
                    })
        sources = sorted({item.get("source") for item in candidate_by_segment.get(segment, [])})
        hit_rows.append({
            "segment_id": segment,
            "kind": manifest.get("kind"),
            "operation": hit.get("operation"),
            "errno": hit.get("errno"),
            "admission": hit.get("admission"),
            "attribution_status": attribution.get("status"),
            "native_envelope_verified": attribution.get("native_envelope_verified"),
            "native_syscall": native.get("name"),
            "native_call_index": native.get("call_index"),
            "native_result": native.get("raw_result"),
            "native_errno": native.get("errno"),
            "trace_event_found": found,
            "trace_event_agrees": agrees,
            "trace_line": trace_line,
            "errno_choice_verified": policy.get("errno_choice_verified"),
            "guest_exit": manifest.get("guest_exit"),
            "segment_status": manifest.get("status"),
            "filesystem_reset_before": manifest.get("filesystem_reset_before", False),
            "stable_before": before.get("status"),
            "stable_after": after.get("status"),
            "stable_after_reason": after.get("reason"),
            "lifecycle_persistent_mismatch": lifecycle_counts.get("persistent_mismatch", 0),
            "lifecycle_operation_exit_mismatch": lifecycle_counts.get("operation_exit_mismatch", 0),
            "candidate_sources": "+".join(sources),
        })
        for phase, observation in (("before", before), ("after", after)):
            snapshot_rows.append({
                "segment_id": segment,
                "kind": manifest.get("kind"),
                "operation": hit.get("operation"),
                "admission": hit.get("admission"),
                "phase": phase,
                "status": observation.get("status"),
                "reason": observation.get("reason"),
                "valid": observation.get("valid"),
                "namespace_valid": observation.get("namespace_valid"),
                "relations_valid": observation.get("relations_valid"),
                "observed_mismatches": observation.get("observed_mismatches"),
                "namespace_mismatched": observation.get("namespace_mismatched"),
                "relations_mismatches": observation.get("relations_mismatches"),
            })

    admitted_rows = [row for row in hit_rows if row["admission"] == "reviewed"]
    source_counts = collections.Counter(item["source"] for item in candidates)
    admission_candidate_counts = collections.Counter(
        (item["source"], item["admission"]) for item in candidate_rows)
    summary = {
        "campaign": report["run_id"],
        "inputs": {"report": "campaign-report.json", "campaign": "campaign.json"},
        "campaign_status": report.get("status"),
        "segments": report.get("segments"),
        "complete_segments": report.get("complete_segments"),
        "fault_hits": len(hit_rows),
        "fault_hits_by_kind": report["fault_injection"].get("hit_counts_by_kind"),
        "fault_hits_by_admission": dict(collections.Counter(row["admission"] for row in hit_rows)),
        "fault_hits_by_attribution": dict(collections.Counter(row["attribution_status"] for row in hit_rows)),
        "trace_event_found": trace_found,
        "trace_event_agrees": trace_agree,
        "errno_choice_verified": sum(bool(row["errno_choice_verified"]) for row in hit_rows),
        "candidate_count": len(candidate_rows),
        "candidate_unique_segments": len({row["segment_id"] for row in candidate_rows}),
        "candidate_source_counts": dict(source_counts),
        "candidate_admission_counts": {f"{source}:{admission}": count
                                        for (source, admission), count in sorted(admission_candidate_counts.items())},
        "candidate_overlap_segments": sum(len(candidate_by_segment[s]) > 1 for s in candidate_by_segment),
        "unverified_count": report["fault_injection"].get("unverified_count"),
        "stable_snapshot_status": {
            ":".join(str(value) for value in key): count
            for key, count in collections.Counter(
                (row["phase"], row["status"], row["reason"]) for row in snapshot_rows).items()
        },
        "lifecycle_classification_totals": dict(lifecycle_totals),
        "candidate_lifecycle_persistent_mismatch": sum(
            row["persistent_mismatch"] for row in candidate_rows),
        "candidate_lifecycle_operation_exit_mismatch": sum(
            row["operation_exit_mismatch"] for row in candidate_rows if row["source"] == "lifecycle"),
        "lifecycle_candidate_episode_shape": {
            "episodes": len(lifecycle_episode_rows),
            "streams": dict(collections.Counter(row["stream"].split(":", 1)[0]
                                                for row in lifecycle_episode_rows)),
            "onset_points": dict(collections.Counter(row["onset_point"]
                                                     for row in lifecycle_episode_rows)),
            "recovered_in_trace": sum(row["recovered_in_trace"] for row in lifecycle_episode_rows),
            "unrecovered_at_segment_end": sum(not row["recovered_in_trace"]
                                               for row in lifecycle_episode_rows),
            "without_native_exit_evidence": sum(row["native_evidence_count"] == 0
                                                 for row in lifecycle_episode_rows),
        },
        "admitted_hits": {
            "total": len(admitted_rows),
            "by_operation": dict(collections.Counter(row["operation"] for row in admitted_rows)),
            "candidate_rows": sum(row["admission"] == "reviewed" for row in candidate_rows),
            "candidate_sources": dict(collections.Counter(
                row["source"] for row in candidate_rows if row["admission"] == "reviewed")),
        },
        "offline_disk": report.get("disk"),
        "tainted_filesystems": report.get("tainted_filesystems"),
        "classification_counts": report.get("classification_counts"),
        "source_attribution_complete": report.get("source_attribution_complete"),
        "counter_validation_passed": report.get("counter_validation_passed"),
    }
    write_csv("candidate-ledger.csv", candidate_rows)
    write_csv("real-hits.csv", hit_rows)
    write_csv("admitted-hits.csv", admitted_rows)
    write_csv("snapshot-timeline.csv", snapshot_rows)
    write_csv("lifecycle-candidate-episodes.csv", lifecycle_episode_rows)
    write_json("summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
