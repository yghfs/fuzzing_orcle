#!/usr/bin/env python3
"""Validate a finite trace interval using per-superblock sequence endpoints."""
import collections
import json
import pathlib
import re
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from trace_lifecycle import analyze as analyze_lifecycle


def analyze(directory):
    events = []
    for line in (directory / "trace.txt").read_text().splitlines():
        if "ext4_counter_event:" not in line:
            continue
        fields = dict(re.findall(r"(\w+)=([^\s]+)", line))

        fields["trace_prefix"] = line.split("ext4_counter_event:", 1)[0].strip()
        for key in ("epoch", "seq", "op", "parent", "status", "update_delta"):
            fields[key] = int(fields[key])
        events.append(fields)
    with (directory / "events.jsonl").open("w") as output:
        for event in events:
            output.write(json.dumps(event) + "\n")
    failures = []
    runs = {}
    for kind in ("regular", "bigalloc", "nojournal", "lazy"):
        start = json.loads((directory / f"{kind}-start.json").read_text())
        end = json.loads((directory / f"{kind}-end.json").read_text())
        selected = sorted((e for e in events if e["epoch"] == start["epoch"]
                           and start["seq"] < e["seq"] <= end["seq"]),
                          key=lambda e: e["seq"])
        seqs = [e["seq"] for e in selected]
        complete = seqs == list(range(start["seq"] + 1, end["seq"] + 1))
        if not complete or start["active"] or end["active"]:
            failures.append(f"{kind}: incomplete interval or active endpoint")
        opened = {}
        for event in selected:
            if event["formula"] == "BEGIN":
                if event["op"] in opened:
                    failures.append(f"{kind}: duplicate operation begin")
                opened[event["op"]] = event["point"]
            elif event["formula"] == "END":
                if opened.pop(event["op"], None) != event["point"]:
                    failures.append(f"{kind}: unmatched operation end")
        if opened:
            failures.append(f"{kind}: incomplete operations")
        snapshots = {}
        for stage in ("before", "after", "retained", "released", "namespace"):
            sample = json.loads((directory / f"{kind}-{stage}.json").read_text())
            snapshots[stage] = sample
            if (not sample["valid"] or sample["observed_mismatches"] or
                    not sample.get("namespace_valid", False)):
                failures.append(f"{kind}: {stage} snapshot failed")
        status = collections.Counter(e["status"] for e in selected)
        if status[2]:
            failures.append(f"{kind}: valid mismatch events")
        points = collections.Counter(e["point"] for e in selected)
        for point in ("BLOCK.ALLOC.GLOBAL", "BLOCK.FREE.GLOBAL",
                      "INODE.ALLOC.BITMAP", "INODE.FREE.BITMAP",
                      "NAMESPACE.rename2", "NAMESPACE.link", "NAMESPACE.rmdir"):
            if not points[point]:
                failures.append(f"{kind}: missing point {point}")
        for point in ("NAMESPACE.DIRENT.ADD", "NAMESPACE.DIRENT.DELETE",
                      "NAMESPACE.NLINK.INC", "NAMESPACE.NLINK.DEC"):
            if not points[point]:
                failures.append(f"{kind}: missing fine namespace point {point}")
        delta = {}
        for formula, independent, publications in (
                ("R-BDELTA", {"BLOCK.BITMAP.TRANSITION"},
                 {"BLOCK.ALLOC.GLOBAL", "BLOCK.FREE.GLOBAL",
                  "BLOCK.BIGALLOC.RERESERVE"}),
                ("R-IDELTA", {"INODE.ALLOC.BITMAP", "INODE.FREE.BITMAP"},
                 {"INODE.ALLOC.GLOBAL", "INODE.FREE.GLOBAL"}),
                ("R-UDELTA", {"INODE.ALLOC.LIFETIME", "INODE.FREE.LIFETIME"},
                 {"INODE.ALLOC.GLOBAL", "INODE.FREE.GLOBAL"})):
            expected = sum(e["update_delta"] for e in selected
                           if e["formula"] == formula and e["point"] in independent)
            published = sum(e["update_delta"] for e in selected
                            if e["formula"] == formula and e["point"] in publications)
            delta[formula] = {"independent": expected, "published": published}
            if expected != published:
                failures.append(f"{kind}: {formula} publication delta mismatch")
        for left, right in (("before", "after"), ("after", "retained"),
                            ("retained", "released"), ("released", "namespace")):
            left_id = snapshots[left]["snapshot_id"]
            right_id = snapshots[right]["snapshot_id"]
            lo = next(e["seq"] for e in selected if e["op"] == left_id
                      and e["formula"] == "END")
            hi = next(e["seq"] for e in selected if e["op"] == right_id
                      and e["formula"] == "BEGIN")
            window = [e for e in selected if lo < e["seq"] < hi]
            for formula, field, points_for_expected in (
                    ("R-BDELTA", "free_clusters", {"BLOCK.BITMAP.TRANSITION"}),
                    ("R-IDELTA", "free_inodes", {"INODE.ALLOC.BITMAP", "INODE.FREE.BITMAP"}),
                    ("R-UDELTA", "dirs", {"INODE.ALLOC.LIFETIME", "INODE.FREE.LIFETIME"})):
                expected = sum(e["update_delta"] for e in window
                               if e["formula"] == formula and
                               e["point"] in points_for_expected)
                actual = snapshots[right][field] - snapshots[left][field]
                delta[f"{left}:{right}:{formula}"] = {
                    "independent": expected, "actual": actual}
                if expected != actual:
                    failures.append(f"{kind}: {left}:{right}:{formula} total delta mismatch")
        if not (snapshots["retained"]["free_clusters"] < snapshots["after"]["free_clusters"]
                and snapshots["retained"]["free_inodes"] < snapshots["after"]["free_inodes"]
                and snapshots["retained"]["dirs"] > snapshots["after"]["dirs"]):
            failures.append(f"{kind}: retained resources did not change all totals")
        special = collections.Counter(e["point"] for e in selected
                                      if e["point"].startswith((
                                          "FAST_COMMIT.", "RESIZE.",
                                          "BITMAP.CORRUPT.",
                                          "BLOCK.BIGALLOC.")))
        attempts = [json.loads(line) for line in
                    (directory / f"{kind}-concurrent.jsonl").read_text().splitlines()]
        runs[kind] = {"events": len(selected), "sequence_complete": complete,
                      "status": dict(status), "points": dict(points), "delta": delta,
                      "special_points": dict(special),
                      "snapshots": snapshots,
                      "concurrent_unverified": sum(not s["valid"] for s in attempts)}
    stats_files = list(directory.glob("cpu*-stats.txt"))
    if not stats_files:
        failures.append("Missing per-CPU loss statistics")
    for path in stats_files:
        stats = dict(re.findall(r"^([\w ]+):\s*(\d+)$", path.read_text(), re.M))
        for key in ("overrun", "commit overrun", "dropped events"):
            if key not in stats:
                failures.append(f"{path.name}: missing {key} counter")
            if int(stats.get(key, 0)):
                failures.append(f"{path.name}: {key}")
    if not any(e["formula"] == "P-SB" for e in events):
        failures.append("Missing superblock publication events")
    disk = {}
    for kind in ("regular", "bigalloc", "nojournal", "lazy"):
        path = directory / f"{kind}-disk.json"
        if not path.exists():
            failures.append(f"{kind}: missing P-DISK report")
            continue
        report = json.loads(path.read_text())
        disk[kind] = report
        if not report.get("valid"):
            failures.append(f"{kind}: P-DISK invalid")
            continue
        final = runs[kind]["snapshots"]["namespace"]
        disk_clusters = report["superblock"]["free_clusters"]
        disk_inodes = report["superblock"]["free_inodes"]
        disk_dirs = report["descriptor_totals"]["dirs"]
        persisted = {"free_clusters": disk_clusters,
                     "free_inodes": disk_inodes, "dirs": disk_dirs}
        disk[kind]["matches_final_stable_snapshot"] = persisted == {
            field: final[field] for field in persisted}
        if not disk[kind]["matches_final_stable_snapshot"]:
            failures.append(f"{kind}: P-DISK does not match final stable snapshot")
    lifecycle = None
    if any(e.get('schema') == '2' for e in events):
        lifecycle = analyze_lifecycle(directory)
        failures.extend('lifecycle: ' + issue for issue in lifecycle['blockers'])
    return {"lifecycle": ({"passed": lifecycle['passed'],
                          "classification_counts": lifecycle['classification_counts']}
                         if lifecycle else {"available": False, "reason": "legacy_schema"}),
            "passed": not failures, "failures": failures, "runs": runs,
            "disk": disk}


if __name__ == "__main__":
    report = analyze(pathlib.Path(sys.argv[1]))
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["passed"] else 1)
