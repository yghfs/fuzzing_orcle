#!/usr/bin/env python3
"""Export the unified ext4/JBD2 checker and verify isolated patch replay."""
import hashlib
import json
import pathlib
import shutil
import subprocess
import tempfile

project = pathlib.Path(__file__).resolve().parents[1]
pristine = project / "kernel/linux-6.12.1"
development = project / "dev/linux-6.12.1"
paths = sorted(
    {p.relative_to(tree) for tree in (development, pristine)
     for p in (tree / "fs/ext4").iterdir()
     if p.is_file() and (p.suffix in {".c", ".h"} or p.name in {"Kconfig", "Makefile"})} |
    {p.relative_to(tree) for tree in (development, pristine)
     for p in (tree / "include/trace/events").glob("ext4*.h") if p.is_file()} |
    {pathlib.Path("fs/jbd2/journal.c"), pathlib.Path("include/linux/jbd2.h")})
changed = []
chunks = []
for path in paths:
    before = pristine / path
    after = development / path
    if before.exists() and after.exists() and before.read_bytes() == after.read_bytes():
        continue
    diff = subprocess.run(["diff", "-u", "--label", f"a/{path}", "--label", f"b/{path}",
                           str(before) if before.exists() else "/dev/null",
                           str(after) if after.exists() else "/dev/null"],
                          capture_output=True, check=False)
    if diff.returncode != 1:
        raise RuntimeError(f"Unexpected diff status for {path}: {diff.stderr!r}")
    chunks.append(diff.stdout)
    changed.append({"path": str(path), "before_sha256": hashlib.sha256(before.read_bytes()).hexdigest()
                    if before.exists() else None,
                    "after_sha256": hashlib.sha256(after.read_bytes()).hexdigest()
                    if after.exists() else None})
patch = project / "patches/0001-counter-trace-core-and-probes.patch"
patch.write_bytes(b"".join(chunks))
with tempfile.TemporaryDirectory() as directory:
    target = pathlib.Path(directory)
    for item in changed:
        source = pristine / item["path"]
        if source.exists():
            destination = target / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch)], cwd=target, check=True)
    for item in changed:
        replayed = target / item["path"]
        if item["after_sha256"] is None:
            assert not replayed.exists()
        else:
            assert hashlib.sha256(replayed.read_bytes()).hexdigest() == item["after_sha256"]
report = {"patch": patch.name, "sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
          "replay_verified": True, "files": changed}
(project / "patches/manifest.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({"patch": str(patch), "changed_files": len(changed), "replay_verified": True}))
