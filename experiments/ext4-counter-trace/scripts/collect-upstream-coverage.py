#!/usr/bin/env python3
"""Collect and classify coverage from an upstream syz-manager instance.

The manager's ``/cover?jsonl=1`` endpoint is preferred because every record
already carries the source file path.  ``/rawcover`` is also supported for
older managers or for checking corpus raw PCs; in that mode a kallsyms file
or vmlinux is required to classify ext4 and JBD2 symbols.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from typing import Iterable


EXT4_PREFIXES = ("ext4", "__ext4", "_ext4", "ext4fs")
JBD2_PREFIXES = ("jbd2", "__jbd2", "_jbd2")
SYMBOL_EXCLUDES = ("__pfx_", "__traceiter_", "perf_trace_", "trace_",
                   "__event_", "__SCT__")
SYMBOL_TYPES = frozenset("TtWw")


def _address(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip(), 0)
        except ValueError:
            try:
                return int(value.strip(), 16)
            except ValueError:
                return None
    return None


def symbol_class(name: str) -> str | None:
    """Return ext4/jbd2 for a kernel symbol, or None for generic code."""
    clean = name.split(".", 1)[0]
    if clean.startswith(EXT4_PREFIXES):
        return "ext4"
    if clean.startswith(JBD2_PREFIXES):
        return "jbd2"
    return None


def parse_symbol_lines(lines: Iterable[str]) -> list[tuple[int, int, str]]:
    """Parse kallsyms/nm-like text into ``(start, end, class)`` ranges.

    Data and absolute symbols are intentionally ignored.  The next text
    symbol bounds each range; a final range is open-ended and represented by
    ``2**64``.
    """
    symbols: list[tuple[int, str, str | None]] = []
    for raw in lines:
        fields = raw.strip().split()
        if len(fields) < 3:
            continue
        # kallsyms and nm print bare addresses in hexadecimal (without the
        # 0x prefix), while JSON/rawcover values may use either spelling.
        try:
            address = int(fields[0], 16)
        except ValueError:
            address = _address(fields[0])
        if address is None:
            continue
        if len(fields[1]) != 1 or fields[1] not in SYMBOL_TYPES:
            continue
        name = fields[2]
        symbols.append((address, name, symbol_class(name)))
    symbols.sort(key=lambda item: (item[0], item[1]))
    ranges: list[tuple[int, int, str]] = []
    for index, (start, name, cls) in enumerate(symbols):
        end = symbols[index + 1][0] if index + 1 < len(symbols) else (1 << 64)
        if end > start and cls is not None and not name.startswith(SYMBOL_EXCLUDES):
            ranges.append((start, end, cls))
    return ranges


def load_symbol_ranges(kallsyms: pathlib.Path | None,
                       vmlinux: pathlib.Path | None) -> list[tuple[int, int, str]]:
    if kallsyms:
        with kallsyms.open("r", encoding="utf-8", errors="replace") as stream:
            return parse_symbol_lines(stream)
    if not vmlinux:
        return []
    try:
        result = subprocess.run(
            ["nm", "-n", "--defined-only", str(vmlinux)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"failed to read symbols from {vmlinux}: {error}") from error
    # nm emits ``address type name`` and may use a leading underscore in
    # toolchain-specific formats; parse_symbol_lines handles the normal case.
    return parse_symbol_lines(result.stdout.splitlines())


def classify_pc(pc: int, ranges: list[tuple[int, int, str]]) -> str | None:
    # Ranges are sorted, and binary search avoids a scan for large corpora.
    lo, hi = 0, len(ranges)
    while lo < hi:
        mid = (lo + hi) // 2
        if ranges[mid][0] <= pc:
            lo = mid + 1
        else:
            hi = mid
    if not lo:
        return None
    start, end, cls = ranges[lo - 1]
    return cls if start <= pc < end else None


def _path_class(path: str) -> str | None:
    normalized = "/" + path.lstrip("./")
    if "/fs/ext4/" in normalized or normalized.startswith("/fs/ext4/"):
        return "ext4"
    if "/fs/jbd2/" in normalized or normalized.startswith("/fs/jbd2/"):
        return "jbd2"
    return None


def fetch(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json, text/plain"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        body = error.read(512).decode("utf-8", "replace")
        raise RuntimeError(f"GET {url} returned HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"GET {url} failed: {error.reason}") from error


def read_records(data: bytes, source: str,
                 ranges: list[tuple[int, int, str]]) -> tuple[set[int], set[int], set[int], Counter[str]]:
    all_pcs: set[int] = set()
    ext4_pcs: set[int] = set()
    jbd2_pcs: set[int] = set()
    files: Counter[str] = Counter()
    if source == "rawcover":
        for raw in data.decode("utf-8", "replace").splitlines():
            value = raw.strip()
            if not value:
                continue
            pc = _address(value)
            if pc is None:
                continue
            all_pcs.add(pc)
            cls = classify_pc(pc, ranges)
            if cls == "ext4":
                ext4_pcs.add(pc)
            elif cls == "jbd2":
                jbd2_pcs.add(pc)
        return all_pcs, ext4_pcs, jbd2_pcs, files

    for number, raw in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"invalid coverage JSON on line {number}: {error}") from error
        if not isinstance(record, dict):
            continue
        # /cover?jsonl=1 emits every symbolized frame, including frames that
        # have never appeared in the manager corpus.  Only a positive hit
        # count represents corpus coverage; zero-hit frames are the
        # instrumented universe and must not inflate the result.
        try:
            hit_count = int(record.get("hit_count", 0) or 0)
        except (TypeError, ValueError):
            hit_count = 0
        if hit_count <= 0:
            continue
        pc = _address(record.get("pc"))
        if pc is None:
            continue
        all_pcs.add(pc)
        path = str(record.get("file_path") or "")
        if path:
            files[path] += 1
        cls = _path_class(path)
        if cls == "ext4":
            ext4_pcs.add(pc)
        elif cls == "jbd2":
            jbd2_pcs.add(pc)
        # A file-less JSONL record can still be classified if symbols were
        # supplied, which is useful with manager versions that omit paths.
        elif not path:
            cls = classify_pc(pc, ranges)
            if cls == "ext4":
                ext4_pcs.add(pc)
            elif cls == "jbd2":
                jbd2_pcs.add(pc)
    return all_pcs, ext4_pcs, jbd2_pcs, files


def collect(*, endpoint: str | None, input_path: pathlib.Path | None,
            source: str, timeout: float,
            ranges: list[tuple[int, int, str]]) -> tuple[bytes, dict]:
    if input_path:
        data = input_path.read_bytes()
        source_url = str(input_path)
    else:
        if not endpoint:
            raise RuntimeError("either --endpoint or --input is required")
        suffix = "/cover?jsonl=1" if source == "cover-jsonl" else "/rawcover"
        data = fetch(endpoint.rstrip("/") + suffix, timeout)
        source_url = endpoint.rstrip("/") + suffix
    all_pcs, ext4_pcs, jbd2_pcs, files = read_records(data, source, ranges)
    target = ext4_pcs | jbd2_pcs
    total = len(all_pcs)
    summary = {
        "schema": 1,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "coverage_scope": "manager_corpus",
        "covered_only": source == "cover-jsonl",
        "source_url": source_url,
        "records_bytes": len(data),
        "all_kernel_pc": total,
        "ext4_pc": len(ext4_pcs),
        "jbd2_pc": len(jbd2_pcs),
        "target_ext4_jbd2_pc": len(target),
        "target_fraction": (len(target) / total) if total else 0.0,
        "target_definition": ["fs/ext4/", "fs/jbd2/"],
        "symbol_ranges": len(ranges),
        "files": dict(files),
    }
    return data, summary


def write_outputs(data: bytes, summary: dict, output: pathlib.Path,
                  raw_records: pathlib.Path | None, curve: pathlib.Path | None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if raw_records:
        raw_records.parent.mkdir(parents=True, exist_ok=True)
        raw_records.write_bytes(data)
    if curve:
        curve.parent.mkdir(parents=True, exist_ok=True)
        header = "timestamp\tall_kernel_pc\text4_pc\tjbd2_pc\ttarget_ext4_jbd2_pc\ttarget_fraction\n"
        row = "{collected_at}\t{all_kernel_pc}\t{ext4_pc}\t{jbd2_pc}\t{target_ext4_jbd2_pc}\t{target_fraction:.9f}\n".format(**summary)
        if not curve.exists():
            curve.write_text(header + row, encoding="utf-8")
        else:
            with curve.open("a", encoding="utf-8") as stream:
                stream.write(row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:11900",
                        help="syz-manager HTTP base URL")
    parser.add_argument("--input", type=pathlib.Path,
                        help="read a saved endpoint response instead of HTTP")
    parser.add_argument("--source", choices=("cover-jsonl", "rawcover"), default="cover-jsonl")
    parser.add_argument("--output", type=pathlib.Path, required=True,
                        help="JSON summary output path")
    parser.add_argument("--raw-records", type=pathlib.Path,
                        help="optional copy of the endpoint response")
    parser.add_argument("--curve", type=pathlib.Path,
                        help="optional append-only TSV curve output")
    parser.add_argument("--kallsyms", type=pathlib.Path,
                        help="kallsyms text for raw-PC symbol classification")
    parser.add_argument("--vmlinux", type=pathlib.Path,
                        help="vmlinux used with nm for raw-PC classification")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    if args.input and args.endpoint != parser.get_default("endpoint"):
        parser.error("--input cannot be combined with a non-default --endpoint")
    if args.kallsyms and not args.kallsyms.is_file():
        parser.error(f"kallsyms file does not exist: {args.kallsyms}")
    if args.vmlinux and not args.vmlinux.is_file():
        parser.error(f"vmlinux does not exist: {args.vmlinux}")
    if args.source == "rawcover" and not (args.kallsyms or args.vmlinux):
        parser.error("--source rawcover requires --kallsyms or --vmlinux")
    try:
        ranges = load_symbol_ranges(args.kallsyms, args.vmlinux)
        data, summary = collect(endpoint=None if args.input else args.endpoint,
                                input_path=args.input, source=args.source,
                                timeout=args.timeout, ranges=ranges)
        write_outputs(data, summary, args.output, args.raw_records, args.curve)
    except (OSError, RuntimeError) as error:
        print(f"coverage collection failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
