#!/usr/bin/env python3
"""Validate that an upstream ``.syz`` seed actually ran on its ext4 image.

syz-manager's corpus coverage is not an execution receipt: one-shot programs
can be consumed as candidates and then disappear from ``/rawcover``.  This
checker uses the executor debug stream as an independent receipt.  It requires
one successful ``syz_mount_image$ext4`` followed by the seed's operations and
records the return value of every operation in that mount lifetime.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import pathlib
import re
import sys
import zlib
from typing import Any


CALL_RE = re.compile(r"->\s+([A-Za-z0-9_$]+)\s*\(")
RETURN_RE = re.compile(r"<-\s+([A-Za-z0-9_$]+)=([^\s]+)")
SEED_CALL_RE = re.compile(r"^\s*(?:r\d+\s*=\s*)?([A-Za-z0-9_$]+)\s*\(", re.M)
MOUNT_NAME = "syz_mount_image$ext4"
UNMOUNT_RE = re.compile(r"unmounting filesystem\s+([^\s.]+)")


def load_seed_calls(path: pathlib.Path) -> list[str]:
    calls: list[str] = []
    for match in SEED_CALL_RE.finditer(path.read_text(encoding="utf-8", errors="replace")):
        name = match.group(1)
        # Comments and helper assignments are excluded by the call grammar;
        # keep the syscall variants exactly as syz-manager prints them.
        calls.append(name)
    if not calls or calls[0] != MOUNT_NAME:
        raise ValueError("seed does not start with syz_mount_image$ext4")
    return calls


def _success(value: str) -> bool:
    value = value.lower()
    return value not in {"0xffffffffffffffff", "0xffffffffffffffffu"} and not value.startswith("-1")


def embedded_image_sha256(seed: pathlib.Path) -> str:
    text = seed.read_text(encoding="utf-8", errors="replace")
    # Syzkaller's ``$`` marker is immediately followed by the base64 data;
    # zlib payloads commonly begin with ``e`` so do not consume that byte as
    # part of the marker.
    matches = re.findall(r'&AUTO="\$([A-Za-z0-9+/=]+)"', text)
    if len(matches) != 1:
        raise ValueError("seed must contain exactly one compressed syz_mount_image payload")
    try:
        image = zlib.decompress(base64.b64decode(matches[0], validate=True))
    except (ValueError, zlib.error) as error:
        raise ValueError(f"seed embedded image payload is invalid: {error}") from error
    return hashlib.sha256(image).hexdigest()


def parse_events(text: str) -> list[dict[str, Any]]:
    """Pair executor call/return lines without assuming a fixed line wrap."""
    events: list[dict[str, Any]] = []
    pending: list[tuple[str, int]] = []
    for match in re.finditer(r"->\s+[A-Za-z0-9_$]+\s*\(|<-\s+[A-Za-z0-9_$]+=", text):
        line = text.count("\n", 0, match.start()) + 1
        if match.group(0).startswith("->"):
            call = CALL_RE.search(text, match.start())
            if call:
                pending.append((call.group(1), line))
            continue
        result = RETURN_RE.search(text, match.start())
        if not result:
            continue
        name, value = result.groups()
        # Concurrent executor workers can interleave output.  Pair with the
        # nearest pending call of the same variant, otherwise retain a
        # standalone return for diagnostics.
        index = next((i for i, item in enumerate(pending) if item[0] == name), None)
        call_line = pending[index][1] if index is not None else None
        if index is not None:
            del pending[index]
        events.append({"syscall": name, "return": value, "success": _success(value),
                       "call_line": call_line, "return_line": line})
    return events


def validate(log: pathlib.Path, seed: pathlib.Path,
             image: pathlib.Path | None = None,
             metadata: pathlib.Path | None = None) -> dict[str, Any]:
    text = log.read_text(encoding="utf-8", errors="replace")
    expected = load_seed_calls(seed)
    events = parse_events(text)
    mounts: list[dict[str, Any]] = []
    mount_positions = [m.start() for m in re.finditer(re.escape(MOUNT_NAME), text)]
    # Use executor call positions rather than arbitrary log occurrences.
    mount_positions = [m.start() for m in re.finditer(r"->\s+" + re.escape(MOUNT_NAME), text)]
    for start in mount_positions:
        end_match = UNMOUNT_RE.search(text, start)
        end = end_match.start() if end_match else len(text)
        region = text[start:end]
        region_events = parse_events(region)
        mount_return = next((e for e in region_events if e["syscall"] == MOUNT_NAME), None)
        # With syz-manager -debug the executor stream is usually captured in
        # manager.log, while the VM serial stream is kept separately.  The
        # executor prints the filesystem type and loop device when
        # syz_mount_image succeeds, so accept that as the mount receipt too;
        # when serial output is available the kernel EXT4-fs line remains a
        # second, independent confirmation.
        mounted = bool(
            re.search(r"EXT4-fs \(loop[^)]*\): mounted filesystem", region) or
            re.search(r"syz_mount_image:\s+.*?fs=['\"]ext4['\"]", region,
                      re.IGNORECASE))
        if not mount_return or not mount_return["success"] or not mounted:
            continue
        # The mount event itself is first.  Ignore unrelated calls before the
        # seed sequence and then consume the exact expected call order.
        cursor = 0
        matched: list[dict[str, Any]] = []
        for name in expected:
            found = None
            for index in range(cursor, len(region_events)):
                if region_events[index]["syscall"] == name:
                    found = region_events[index]
                    cursor = index + 1
                    break
            if found is None:
                break
            matched.append(found)
        mounts.append({
            "mount_return": mount_return["return"],
            "mounted": mounted,
            "expected_calls": len(expected),
            "matched_calls": len(matched),
            "calls": matched,
            "complete": len(matched) == len(expected) and all(e["success"] for e in matched),
            "log_start_line": text.count("\n", 0, start) + 1,
            "log_end_line": text.count("\n", 0, end) + 1,
        })
    selected = next((item for item in mounts if item["complete"]), None)
    result: dict[str, Any] = {
        "schema": 1,
        "status": "passed" if selected else "failed",
        "evidence_source": "syz-manager executor debug log",
        "log": str(log.resolve()),
        "seed_program": str(seed.resolve()),
        "seed_program_sha256": hashlib.sha256(seed.read_bytes()).hexdigest(),
        "expected_calls": expected,
        "mount_attempts": mounts,
        "successful_mounts": len(mounts),
        "selected_mount": selected,
        "target_filesystem_execution_verified": selected is not None,
    }
    if image is not None:
        target_digest = hashlib.sha256(image.read_bytes()).hexdigest()
        embedded_digest = embedded_image_sha256(seed)
        result["target_ext4_image"] = str(image.resolve())
        result["target_ext4_image_sha256"] = target_digest
        result["seed_embedded_image_sha256"] = embedded_digest
        result["seed_embedded_image_matches_target"] = embedded_digest == target_digest
        if embedded_digest != target_digest:
            result["status"] = "failed"
            result["target_filesystem_execution_verified"] = False
            result["failure"] = "seed embedded image hash does not match target ext4 image"
    if metadata is not None and metadata.is_file():
        try:
            result["seed_metadata"] = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            result["metadata_error"] = str(error)
    if not selected:
        result["failure"] = (
            "no complete successful syz_mount_image$ext4 lifetime was found; "
            "run syz-manager with -debug to collect executor evidence"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=pathlib.Path, required=True)
    parser.add_argument("--seed-program", type=pathlib.Path, required=True)
    parser.add_argument("--target-ext4-image", type=pathlib.Path)
    parser.add_argument("--metadata", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = validate(args.log, args.seed_program, args.target_ext4_image, args.metadata)
    except (OSError, ValueError) as error:
        result = {"schema": 1, "status": "unreadable", "target_filesystem_execution_verified": False,
                  "failure": str(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
