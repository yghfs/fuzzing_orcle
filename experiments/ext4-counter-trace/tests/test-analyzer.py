#!/usr/bin/env python3
"""Negative checks against a real smoke trace, kept intact in results/."""
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile

project = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("analyzer", project / "scripts/analyze-trace.py")
analyzer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyzer)
source = pathlib.Path(sys.argv[1])
assert analyzer.analyze(source)["passed"]

with tempfile.TemporaryDirectory() as temp:
    fixture = pathlib.Path(temp) / "trace"
    shutil.copytree(source, fixture)
    trace = fixture / "trace.txt"
    original = trace.read_text()
    epoch = json.loads((fixture / "regular-start.json").read_text())["epoch"]
    lines = original.splitlines(keepends=True)
    victim = next(i for i, line in enumerate(lines)
                  if f"epoch={epoch} " in line and "point=BLOCK.BITMAP.TRANSITION " in line)
    trace.write_text("".join(lines[:victim] + lines[victim + 1:]))
    report = analyzer.analyze(fixture)
    assert not report["passed"] and any("incomplete interval" in x for x in report["failures"])
    trace.write_text(original)
    stats = next(fixture.glob("cpu*-stats.txt"))
    original_stats = stats.read_text()
    stats.write_text(original_stats.replace("overrun: 0", "overrun: 1", 1))
    assert not analyzer.analyze(fixture)["passed"]
    stats.write_text(original_stats)
    snapshot = fixture / "regular-retained.json"
    wrong = json.loads(snapshot.read_text())
    wrong["free_clusters"] += 1
    snapshot.write_text(json.dumps(wrong))
    report = analyzer.analyze(fixture)
    assert not report["passed"] and any("total delta mismatch" in x for x in report["failures"])
    disk = fixture / "regular-disk.json"
    wrong_disk = json.loads(disk.read_text())
    wrong_disk["superblock"]["free_clusters"] += 1
    disk.write_text(json.dumps(wrong_disk))
    report = analyzer.analyze(fixture)
    assert not report["passed"] and any("P-DISK" in x for x in report["failures"])

print("analyzer_negative_checks=passed lost_event overrun incorrect_total_delta incorrect_disk_total")
