#!/usr/bin/env python3
"""Validate the targeted special-path trace.

Every special path has its own independent expected value.  This checker
requires the path to be present, requires its valid comparisons to hold and
reports the paths that were not exercised by the workload.
"""
from __future__ import annotations

import json
import hashlib
import pathlib
import re
import sys
from trace_lifecycle import parse_trace, loss_evidence, scope_tree, write_coverage


REQUIRED = {
    "BLOCK.BIGALLOC.RERESERVE": "R-BDELTA",
    "BLOCK.BIGALLOC.RERESERVE.DIRTY": "R-BDELTA",
    "RESIZE.COUNTERS.PUBLISH.CLUSTERS": "R-BSUM",
    "RESIZE.COUNTERS.PUBLISH.INODES": "R-ISUM",
    "RESIZE.COUNTERS.PUBLISH.GROUPS": "P-SB",
    "FAST_COMMIT.BLOCK.REPLAY": "R-BDELTA",
    "FAST_COMMIT.INODE.REPLAY.GLOBAL": "R-IDELTA",
    "FAST_COMMIT.INODE.REPLAY.DESCRIPTOR": "R-IDELTA",
    "BITMAP.CORRUPT.EXCLUDE.BLOCK": "M-B",
    "BITMAP.CORRUPT.EXCLUDE.INODE": "M-I",
}



def validate_stage(stage):
    events, _ = parse_trace(stage / "trace.txt")
    dev = (stage / "dev.txt").read_text().strip().replace(":", ",")
    end = json.loads((stage / "end.json").read_text())
    selected = sorted((e for e in events if e['dev'] == dev and
                       e['epoch'] == end['epoch']), key=lambda e:e['seq'])
    issues = []
    if [e['seq'] for e in selected] != list(range(1, end['seq'] + 1)):
        issues.append('incomplete_mount_epoch_sequence')
    if not selected or end.get('active') != 0:
        issues.append('missing_or_active_stage_endpoint')
    if any(e.get('schema', 0) < 3 for e in selected):
        issues.append('requires_write_audit_schema3_or_newer')
    stats, loss = loss_evidence(stage)
    issues.extend(loss)
    cpus = (stage / 'cpus.txt').read_text().split()
    if not cpus or set(stats) != {cpu+'-stats.txt' for cpu in cpus}:
        issues.append('incomplete_cpu_loss_evidence')
    scopes, scope_issues = scope_tree(selected)
    issues.extend(scope_issues)
    coverage = write_coverage(selected, scopes)
    issues.extend(coverage['issues'])
    for e in selected:
        if e['comparison_valid'] and e['status'] == 2:
            issues.append(f'valid_comparison_mismatch:{e["point"]}:{e["seq"]}')
        if e['point'] == 'WRITE.DESCRIPTOR' and e['actual']-e['expected'] != e['update_delta']:
            issues.append(f'write_payload_disagrees:{e["seq"]}')
    # Replay uses the same block bitmap/descriptor authority helper. Its
    # audit stream must still match every publication despite special-domain
    # exclusion from the ordinary timeline.
    for op, scope in scopes.items():
        own = [e for e in selected if e['op'] == op]
        if scope['point'] == 'BLOCK.MARK_CONTEXT':
            writes = [e['update_delta'] for e in own if e['formula'] == 'WRITE-BG']
            local = [e['update_delta'] for e in own if e['point'] in
                     ('BLOCK.DESCRIPTOR.LOCAL','GROUP.LAZY.BLOCK.DESCRIPTOR')]
            if writes != local:
                issues.append(f'block_descriptor_write_disagrees:{op}')
        if scope['point'] == 'FAST_COMMIT.INODE.REPLAY':
            bitmap = [e for e in own if e['point'] == 'FAST_COMMIT.INODE.REPLAY.BITMAP']
            desc = [e for e in own if e['point'] == 'FAST_COMMIT.INODE.REPLAY.DESCRIPTOR']
            writes = [e for e in own if e['formula'] == 'WRITE-IG']
            globals_ = [e for e in own if e['point'] == 'WRITE.GLOBAL']
            if len(bitmap) != 1 or len(desc) != 1:
                issues.append(f'incomplete_replay_inode_steps:{op}')
                continue
            delta = bitmap[0]['update_delta']
            if delta not in (0,-1) or desc[0]['update_delta'] != delta or globals_:
                issues.append(f'replay_inode_delta_disagrees:{op}')
            if [e['update_delta'] for e in writes] != ([-1] if delta == -1 else []):
                issues.append(f'replay_inode_authority_disagrees:{op}')
    return selected, {'passed':not issues, 'issues':issues, 'events':len(selected),
                      'end':end, 'dev':dev, 'loss':stats, 'write_coverage':coverage}


def parse(directory: pathlib.Path):
    events, evidence = [], {}
    roots = (directory / 'first-boot/guest-evidence/stages',
             directory / 'ext4-counter-trace/stages')
    expected = ({'bigalloc','resize'}, {'replay','corrupt'})
    for root, names in zip(roots, expected):
        if {p.name for p in root.iterdir() if p.is_dir()} != names:
            raise ValueError(f'missing or unexpected stage evidence: {root}')
        for stage in sorted(root.iterdir()):
            selected, report = validate_stage(stage)
            evidence[stage.name] = report
            for event in selected:
                event['trace_file'] = stage.relative_to(directory).as_posix()+'/trace.txt'
                events.append(event)
    with (directory / "events.jsonl").open("w") as output:
        for event in events:
            output.write(json.dumps(event) + "\n")
    return events, evidence


def main(directory: pathlib.Path):
    try:
        events, stages = parse(directory)
    except (OSError, ValueError) as exc:
        return {"passed": False, "failures": [str(exc)], "covered": [],
                "missing": sorted(REQUIRED), "optional_missing": [],
                "counts": {}, "snapshots": {}}
    failures = []
    for name, stage in stages.items():
        failures.extend(name+':'+issue for issue in stage['issues'])
    points = {}
    for event in events:
        points.setdefault(event["point"], []).append(event)
    for point in REQUIRED:
        if point not in points:
            failures.append(f"{point}: targeted event missing")
        for event in points.get(point, []):
            if event.get("status") != 1:
                failures.append(
                    f"{point}: comparison not proven status={event.get('status')} "
                    f"actual={event.get('actual')} expected={event.get('expected')}")
    # rereserve must publish exactly one cluster and one dirty reservation
    for event in points.get("BLOCK.BIGALLOC.RERESERVE", []):
        if event["update_delta"] != 1:
            failures.append("rereserve delta is not one cluster")
        if event["actual"] != event["expected"]:
            failures.append("rereserve global publication mismatch")
    # resize must add capacity in both dimensions
    for point, field in (("RESIZE.COUNTERS.PUBLISH.CLUSTERS", "actual"),
                         ("RESIZE.COUNTERS.PUBLISH.INODES", "actual"),
                         ("RESIZE.COUNTERS.PUBLISH.GROUPS", "actual")):
        for event in points.get(point, []):
            if event[field] != event["expected"]:
                failures.append(f"{point}: publication mismatch")
            if event["update_delta"] <= 0:
                failures.append(f"{point}: non positive added capacity")
    # fast-commit replay must not publish to the global counters
    for point in ("FAST_COMMIT.BLOCK.REPLAY", "FAST_COMMIT.INODE.REPLAY.GLOBAL"):
        for event in points.get(point, []):
            if event["actual"] != event["expected"]:
                failures.append(f"{point}: replay published to global counter")
    # corruption exclusion must subtract the group's free counts
    for point in ("BITMAP.CORRUPT.EXCLUDE.BLOCK", "BITMAP.CORRUPT.EXCLUDE.INODE"):
        for event in points.get(point, []):
            if event["update_delta"] >= 0:
                failures.append(f"{point}: exclusion did not subtract")
            if event["actual"] != event["expected"]:
                failures.append(f"{point}: exclusion mismatch")
    bitmap = points.get('FAST_COMMIT.INODE.REPLAY.BITMAP', [])
    branches = {e['update_delta'] for e in bitmap}
    if not {-1, 0} <= branches:
        failures.append('fast_commit_inode_changed_and_idempotent_branches_required')
    snapshots = {}
    for name in sorted(directory.rglob("*.json")):
        try:
            data = json.loads(name.read_text())
        except json.JSONDecodeError:
            continue
        if "snapshot_id" in data:
            snapshot_name = name.relative_to(directory).as_posix()
            snapshots[snapshot_name] = data
            if (not data.get("valid") or data.get("observed_mismatches") or
                    not data.get("namespace_valid") or
                    data.get("namespace_mismatched")):
                failures.append(f"{snapshot_name}: snapshot failed")
    if not snapshots:
        failures.append("no stable snapshots found")
    # A successful replay trace must be tied to an offline check of the exact
    # fast-commit image produced by this run.
    try:
        disk_evidence = json.loads((directory / 'fc-disk-evidence.json').read_text())
        disk = json.loads((directory / 'fc-disk.json').read_text())
        replay_snapshot = json.loads(
            (directory / 'ext4-counter-trace/special-replay-after.json').read_text())
        if disk_evidence.get('fsck_exit') != 0:
            failures.append('fast_commit_offline_fsck_failed')
        if not disk.get('valid'):
            failures.append('fast_commit_offline_disk_check_failed')
        for name, expected in disk_evidence.get('sha256', {}).items():
            actual = hashlib.sha256((directory / name).read_bytes()).hexdigest()
            if actual != expected:
                failures.append(f'fast_commit_disk_evidence_hash_mismatch:{name}')
        for field in ('free_clusters', 'free_inodes'):
            if disk['superblock'][field] != replay_snapshot[field]:
                failures.append(f'fast_commit_disk_snapshot_mismatch:{field}')
        if disk['descriptor_totals']['dirs'] != replay_snapshot['dirs']:
            failures.append('fast_commit_disk_snapshot_mismatch:dirs')
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        failures.append(f'missing_or_invalid_fast_commit_disk_evidence:{exc}')
    report = {
        "passed": not failures,
        "failures": failures,
        "covered": sorted(point for point in REQUIRED if point in points),
        "missing": sorted(point for point in REQUIRED if point not in points),
        "optional_missing": [],
        "counts": {point: len(items) for point, items in sorted(points.items())
                   if point in REQUIRED},
        "snapshots": snapshots,
        "fast_commit_disk_evidence": disk_evidence if 'disk_evidence' in locals() else None,
        "stages": stages,
        "replay_inode_branch_counts": {str(delta):sum(e["update_delta"] == delta for e in bitmap) for delta in (-1,0)},
    }
    return report


if __name__ == "__main__":
    report = main(pathlib.Path(sys.argv[1]))
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["passed"] else 1)
