#!/usr/bin/env python3
"""Validate storage-layer I/O error propagation independently of ext4.

The test uses QEMU's blkdebug driver over a raw image and compares one direct
read with the same read through an injected error.  It intentionally emits a
different evidence kind from the ext4 helper-fault schema: a blkdebug error
proves that QEMU/storage I/O propagated an errno, but says nothing about an
ext4 counter or journal path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import tempfile
from typing import Any


def digest(path: pathlib.Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, text=True, capture_output=True,
                               check=False)
    return {"argv": command, "returncode": completed.returncode,
            "stdout": completed.stdout, "stderr": completed.stderr}


def image_opts(config: pathlib.Path, image: pathlib.Path) -> str:
    # qemu-io's --image-opts parser accepts the blkdebug config filename and a
    # nested file child without relying on the deprecated -o spelling.
    return (f"driver=blkdebug,config={config},"
            f"image.driver=file,image.filename={image}")


def validate(image: pathlib.Path | None, event: str, errno: int,
             workdir: pathlib.Path | None = None) -> dict[str, Any]:
    qemu_io = shutil.which("qemu-io")
    qemu_img = shutil.which("qemu-img")
    issues: list[str] = []
    if qemu_io is None or qemu_img is None:
        return {"schema": 1, "kind": "blkdebug_io_control",
                "classification": "tool_unavailable", "passed": False,
                "issues": ["qemu_tools_missing"]}
    with tempfile.TemporaryDirectory(dir=str(workdir) if workdir else None,
                                      prefix="blkdebug-io-") as temporary:
        root = pathlib.Path(temporary)
        owned_image = image is None
        target = root / "control.raw" if owned_image else image
        if target is None:
            raise AssertionError("target image was not selected")
        if owned_image:
            create = run([qemu_img, "create", "-q", "-f", "raw",
                          str(target), "1M"])
            if create["returncode"] != 0:
                return {"schema": 1, "kind": "blkdebug_io_control",
                        "classification": "tool_failure", "passed": False,
                        "issues": ["qemu_img_create_failed"],
                        "create": create}
        before = digest(target)
        control = run([qemu_io, "--image-opts", "-c", "read 0 4096",
                       f"driver=file,filename={target}"])
        config = root / "blkdebug.conf"
        # event=none is a supported blkdebug trigger at node creation.  It
        # gives a deterministic read failure across qemu-io versions, while
        # callers can select read_aio when validating a QEMU build's async
        # event path explicitly.
        config_text = ("[inject-error]\n" +
                       f'event = "{event}"\n' +
                       f'errno = "{errno}"\n' +
                       'once = "on"\nimmediately = "on"\n')
        config.write_text(config_text)
        injected = run([qemu_io, "--image-opts", "-c", "read 0 4096",
                        image_opts(config, target)])
        after = digest(target)
        control_ok = control["returncode"] == 0
        injected_error = injected["returncode"] != 0 and any(
            token in (injected["stdout"] + injected["stderr"]).lower()
            for token in ("input/output error", "i/o error", "error"))
        if not control_ok:
            issues.append("control_read_failed")
        if not injected_error:
            issues.append("injected_error_not_propagated")
        if before != after:
            issues.append("read_changed_image")
        classification = "blkdebug_io_error_propagated" if not issues else \
            "blkdebug_control_invalid"
        return {
            "schema": 1,
            "kind": "blkdebug_io_control",
            "classification": classification,
            "passed": not issues,
            "event": event,
            "errno": errno,
            "blkdebug_config": config_text,
            "image": str(target),
            "image_sha256_before": before,
            "image_sha256_after": after,
            "control": control,
            "injected": injected,
            "issues": issues,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=pathlib.Path,
                        help="read an existing raw image; otherwise create a temporary one")
    parser.add_argument("--event", default="none",
                        help="blkdebug event (default: deterministic node-creation trigger)")
    parser.add_argument("--errno", type=int, default=5)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    report = validate(args.image, args.event, args.errno)
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
