#!/usr/bin/env python3
"""Export changed ext4 files and verify replay in an isolated small tree."""
import hashlib
import json
import pathlib
import shutil
import subprocess
import tempfile

project = pathlib.Path(__file__).resolve().parents[1]
pristine = project / "kernel/linux-6.12.1"
development = project / "dev/linux-6.12.1"
paths = sorted({p.relative_to(development) for p in (development / "fs/ext4").iterdir()
                if p.is_file()} |
               {p.relative_to(development) for p in
                (development / "include/trace/events").glob("ext4*.h") if p.is_file()})
changed = []
chunks = []
for path in paths:
    before = pristine / path
    after = development / path
    if before.exists() and before.read_bytes() == after.read_bytes():
        continue
    diff = subprocess.run(["diff", "-u", "--label", f"a/{path}", "--label", f"b/{path}",
                           str(before) if before.exists() else "/dev/null", str(after)],
                          capture_output=True, check=False)
    if diff.returncode != 1:
        raise RuntimeError(f"Unexpected diff status for {path}: {diff.stderr!r}")
    chunks.append(diff.stdout)
    changed.append({"path": str(path), "before_sha256": hashlib.sha256(before.read_bytes()).hexdigest()
                    if before.exists() else None,
                    "after_sha256": hashlib.sha256(after.read_bytes()).hexdigest()})
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
        assert hashlib.sha256((target / item["path"]).read_bytes()).hexdigest() == item["after_sha256"]
report = {"patch": patch.name, "sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
          "replay_verified": True, "files": changed}
(project / "patches/manifest.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({"patch": str(patch), "changed_files": len(changed), "replay_verified": True}))
