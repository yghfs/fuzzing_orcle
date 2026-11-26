#!/usr/bin/env python3
"""Map every realistic CSV record to Linux 6.12.1 and export replayable probes.

Coordinates describe the pristine release, not the counter-trace development
tree. Token matching preserves that correspondence across the checker patch.
Unrecognised expansions or changed expressions are errors, never skipped sites.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parents[1]
BASE = PROJECT.parent / "ext4-counter-trace"
PRISTINE = BASE / "kernel/linux-6.12.1"
CSV = ROOT / "realistic_review.csv"
PATCH = PROJECT / "patches/0001-reviewed-error-site-trace.patch"
MANIFEST = PROJECT / "sites.json"
SITE_CSV = PROJECT / "sites.csv"
BASE_PATCH = BASE / "patches/0001-counter-trace-core-and-probes.patch"
TOKEN = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|'
                   r"'(?:\\.|[^'\\])*'|[A-Za-z_]\w*|\d+|[^\s]", re.S)
ALIASES = {
    "kmalloc_array_noprof": {"kmalloc_array", "kcalloc"},
    "kmem_cache_alloc_noprof": {"kmem_cache_alloc", "kmem_cache_zalloc", "jbd2_alloc_inode"},
    "__percpu_counter_init_many": {"percpu_counter_init"},
    "__kvmalloc_node_noprof": {"kvzalloc", "kvmalloc", "kvmalloc_node_noprof"},
    "pcpu_alloc_noprof": {"alloc_percpu"},
    "__kmem_cache_create": {"kmem_cache_create"},
    "mempool_create_node_noprof": {"mempool_create_slab_pool"},
    "kvmalloc_array_node_noprof": {"kvmalloc_array"},
    "__percpu_init_rwsem": {"percpu_init_rwsem"},
    "kmem_cache_alloc_lru_noprof": {"alloc_inode_sb"},
}
ASSETS = {
    "ext4_error_site.h": "include/linux/ext4_error_site.h",
    "ext4_error_site_event.h": "include/trace/events/ext4_error_site.h",
    "error_site_trace.c": "fs/ext4/error_site_trace.c",
}
# The checker evaluates the directory count once, then uses it for both the
# counter initialization and the diagnostic. Keep this reviewed relocation
# explicit rather than accepting arbitrary argument changes.
RELOCATIONS = {
    504: ("percpu_counter_init(&sbi->s_dirs_counter, ext4_count_dirs(sbi->s_sb), GFP_KERNEL)",
          "percpu_counter_init(&sbi->s_dirs_counter, dirs, GFP_KERNEL)"),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tokens(text):
    return [(m[0], m.start(), m.end()) for m in TOKEN.finditer(text)
            if not m[0].startswith(("/*", "//"))]


def call_end(ts, start):
    if ts[start + 1][0] != "(":
        raise ValueError("CSV coordinate is not a call")
    depth = 0
    for end in range(start + 1, len(ts)):
        if ts[end][0] == "(":
            depth += 1
        elif ts[end][0] == ")":
            depth -= 1
            if not depth:
                return end + 1
    raise ValueError("unclosed call expression")


def locate(row, text, ts):
    lines = text.splitlines(keepends=True)
    offset = sum(map(len, lines[:int(row["line"]) - 1])) + int(row["column"]) - 1
    positions = {t[1]: i for i, t in enumerate(ts)}
    if offset not in positions:
        raise ValueError(f"site {row['site_id']}: coordinate is not a token")
    begin = positions[offset]
    end = call_end(ts, begin)
    # The LLVM location for these calls points at a containing source macro.
    if ts[begin][0] == "unlikely" and row["callee"] == "buffer_uptodate":
        choices = [i for i in range(begin + 1, end) if ts[i][0] == "buffer_uptodate"]
    elif ts[begin][0] == "rcu_assign_pointer" and row["callee"] == "kvmalloc_array_node_noprof":
        choices = [i for i in range(begin + 1, end) if ts[i][0] == "kvmalloc_array"]
    else:
        choices = [begin]
    if len(choices) != 1:
        raise ValueError(f"site {row['site_id']}: ambiguous macro expansion")
    begin = choices[0]
    end = call_end(ts, begin)
    allowed = {row["callee"], row["callee"].removesuffix("_noprof")}
    allowed |= ALIASES.get(row["callee"], set())
    if ts[begin][0] not in allowed:
        raise ValueError(f"site {row['site_id']}: unknown spelling {ts[begin][0]}")
    return begin, end


def map_call(original, baseline, ots, bts, blocks, row):
    begin, end = locate(row, original, ots)
    matches = [(b + begin - a, b + end - a) for a, b, size in blocks
               if a <= begin and end <= a + size]
    # A checker refactor can move an error handler around an unchanged call.
    # Constrain all mappings (including diff matches) to the named function.
    ranges = []
    for index, token in enumerate(bts[:-1]):
        if token[0] != row["caller"] or bts[index + 1][0] != "(":
            continue
        body = call_end(bts, index)
        if bts[body][0] != "{":
            continue
        depth = 0
        for stop in range(body, len(bts)):
            if bts[stop][0] == "{":
                depth += 1
            elif bts[stop][0] == "}":
                depth -= 1
                if not depth:
                    ranges.append((body, stop + 1))
                    break
    matches = [(a, b) for a, b in matches if any(lo <= a < b <= hi for lo, hi in ranges)]
    original_signature = [t[0] for t in ots[begin:end]]
    signature = original_signature
    relocation = RELOCATIONS.get(int(row['site_id']))
    if relocation:
        if signature != [t[0] for t in tokens(relocation[0])]:
            raise ValueError(f"site {row['site_id']}: reviewed relocation changed")
        signature = [t[0] for t in tokens(relocation[1])]
        matches = []
    if not matches:
        for lo, hi in ranges:
            for index in range(lo, hi - len(signature) + 1):
                if bts[index][0] == signature[0] and [t[0] for t in bts[index:index + len(signature)]] == signature:
                    matches.append((index, index + len(signature)))
    if len(matches) != 1:
        raise ValueError(f"site {row['site_id']}: call changed across counter-trace patch")
    mapped_begin, mapped_end = matches[0]
    assert signature == [t[0] for t in bts[mapped_begin:mapped_end]]
    return bts[mapped_begin][1], bts[mapped_end - 1][2], ots[begin][0]


def patch_run(target, patch):
    result = subprocess.run(["patch", "--batch", "--fuzz=0", "--no-backup-if-mismatch",
                             "-s", "-p1", "-i", str(patch)], cwd=target,
                            capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stdout + result.stderr)


def load_baseline(target, rows):
    base_manifest = json.loads((BASE / "patches/manifest.json").read_text())
    if sha(BASE_PATCH.read_bytes()) != base_manifest["sha256"] or not base_manifest["replay_verified"]:
        raise ValueError("counter-trace patch manifest mismatch")
    files = {r["source"] for r in rows} | {"fs/ext4/Makefile", "fs/ext4/Kconfig"}
    files |= {r["path"] for r in base_manifest["files"]}
    official = {}
    for line in (BASE / "source/pristine-files.sha256").read_text().splitlines():
        digest, path = line.split("  ", 1)
        path = path.removeprefix("linux-6.12.1/")
        if path in files:
            official[path] = digest
    for name in sorted(files):
        source = PRISTINE / name
        if not source.exists():
            if name in official:
                raise ValueError(f"missing official source: {name}")
            continue
        if sha(source.read_bytes()) != official.get(name):
            raise ValueError(f"official source hash mismatch: {name}")
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    patch_run(target, BASE_PATCH)
    for entry in base_manifest["files"]:
        if sha((target / entry["path"]).read_bytes()) != entry["after_sha256"]:
            raise ValueError(f"counter-trace patch replay mismatch: {entry['path']}")
    return files, base_manifest


def add_include(text):
    match = re.search(r"^#include\b", text, flags=re.M)
    if not match:
        raise ValueError("no include insertion point")
    return text[:match.start()] + "#include <linux/ext4_error_site.h>\n" + text[match.start():]


def generate():
    with CSV.open(newline="") as stream:
        all_rows = list(csv.DictReader(stream))
    rows = [r for r in all_rows if r["realistic"] == "1"]
    ids = [int(r["site_id"]) for r in all_rows]
    if not rows or len(set(ids)) != len(ids) or min(ids) < 1:
        raise ValueError("missing sites or invalid/duplicate site_id")
    tus = {tu: i + 1 for i, tu in enumerate(sorted({r["translation_unit"] for r in rows}))}
    for row in rows:
        for name in (row["source"], row["translation_unit"]):
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError(f"invalid source path: {name}")
    by_file = defaultdict(list)
    for row in rows:
        by_file[row["source"]].append(row)
    generated = {}
    sites = []
    with tempfile.TemporaryDirectory() as directory:
        baseline = Path(directory)
        _, base_manifest = load_baseline(baseline, rows)
        for name, records in sorted(by_file.items()):
            original = (PRISTINE / name).read_text()
            text = (baseline / name).read_text()
            ots, bts = tokens(original), tokens(text)
            blocks = difflib.SequenceMatcher(None, [t[0] for t in ots],
                                             [t[0] for t in bts], autojunk=False).get_matching_blocks()
            groups = defaultdict(list)
            for row in records:
                start, end, spelling = map_call(original, text, ots, bts, blocks, row)
                site = {key: row[key] for key in ("site_id", "translation_unit", "call_instance",
                                                 "source", "line", "column", "caller", "callee", "confidence")}
                for key in ("site_id", "call_instance", "line", "column"):
                    site[key] = int(site[key])
                site.update(source_spelling=spelling, source_expression=text[start:end],
                            mapping_note="reviewed_hoisted_directory_count" if int(row['site_id']) in RELOCATIONS else "unchanged_call_expression",
                            mapped_line=text.count("\n", 0, start) + 1,
                            translation_unit_id=tus[row["translation_unit"]])
                groups[start, end].append(site)
                sites.append(site)
            last_end = -1
            for (start, end), group in sorted(groups.items()):
                if start < last_end:
                    raise ValueError(f"overlapping call expressions in {name}")
                last_end = end
                direct = len(group) == 1 and name == group[0]["translation_unit"]
                selector = str(group[0]["site_id"]) if direct else "(" + " : ".join(
                    [f"EXT4_ERROR_SITE_TU == {s['translation_unit_id']} ? {s['site_id']}" for s in group] + ["0"]) + ")"
                for site in group:
                    site["runtime_selector"] = selector
            for (start, end), group in sorted(groups.items(), reverse=True):
                text = (text[:start] + f"EXT4_ES_CALL({group[0]['runtime_selector']}, " +
                        text[start:end] + ")" + text[end:])
            generated[name] = add_include(text)
            for group in groups.values():
                for site in group:
                    marker = f"EXT4_ES_CALL({site['runtime_selector']}, {site['source_spelling']}("
                    if generated[name].count(marker) != 1:
                        raise ValueError(f"site {site['site_id']}: probe uniqueness failed")
                    site["instrumented_line"] = generated[name].count("\n", 0, generated[name].index(marker)) + 1
        generated["fs/ext4/Makefile"] = (baseline / "fs/ext4/Makefile").read_text() + (
            "\n# Reviewed error-site identity for calls in shared inline headers.\n"
            "ext4-$(CONFIG_EXT4_ERROR_SITE_TRACE) += error_site_trace.o\n"
            "ifeq ($(CONFIG_EXT4_ERROR_SITE_TRACE),y)\n" + "".join(
                f"CFLAGS_{Path(tu).stem}.o += -DEXT4_ERROR_SITE_TU={number}\n" for tu, number in tus.items()) + "endif\n")
        generated["fs/ext4/Kconfig"] = (baseline / "fs/ext4/Kconfig").read_text() + (
            '\nconfig EXT4_ERROR_SITE_TRACE\n\tbool "Trace reviewed ext4 error sites"\n'
            '\tdepends on EXT4_COUNTER_TRACE && EXT4_FS=y\n\thelp\n'
            '\t  Observe reviewed call sites, return values and per-site hit counts.\n'
            '\t  Tracing is disabled at runtime by default. This does not inject faults.\n')
        for asset, dest in ASSETS.items():
            generated[dest] = (PROJECT / "instrumentation" / asset).read_text()
        generated["fs/ext4/error_site_table.h"] = (
            "/* SPDX-License-Identifier: GPL-2.0 */\n/* Generated by instrument-sites.py; do not edit. */\n"
            f"static const struct ext4_error_site_desc ext4_error_sites[{max(ids) + 1}] = {{\n" +
            "".join(f"\t[{s['site_id']}] = {{ " + ", ".join(
                json.dumps(s[k]) for k in ("translation_unit", "source", "line", "column", "caller", "callee")) + " },\n"
                for s in sorted(sites, key=lambda s: s["site_id"])) + "};\n")
        chunks, files = [], []
        for name, after in sorted(generated.items()):
            before_path = baseline / name
            before = before_path.read_text() if before_path.exists() else ""
            diff = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                             fromfile=f"a/{name}" if before else "/dev/null", tofile=f"b/{name}"))
            chunks.append(diff)
            files.append(dict(path=name, baseline_sha256=sha(before.encode()) if before else None,
                              instrumented_sha256=sha(after.encode())))
        patch_bytes = "".join(chunks).encode()
        with tempfile.NamedTemporaryFile() as stream:
            stream.write(patch_bytes)
            stream.flush()
            patch_run(baseline, Path(stream.name))
        for entry in files:
            if sha((baseline / entry["path"]).read_bytes()) != entry["instrumented_sha256"]:
                raise ValueError(f"error-site patch replay mismatch: {entry['path']}")
    inputs = [Path(__file__).resolve(), PROJECT / "config/error-site-trace.config"]
    inputs += [PROJECT / "instrumentation" / asset for asset in ASSETS]
    manifest = dict(schema=1, kernel_version="6.12.1", mode="observe_only",
                    review_csv_sha256=sha(CSV.read_bytes()), base_patch_sha256=sha(BASE_PATCH.read_bytes()),
                    patch_sha256=sha(patch_bytes), replay_verified=True,
                    inputs={p.relative_to(PROJECT).as_posix(): sha(p.read_bytes()) for p in inputs},
                    summary=dict(candidate_records=len(all_rows), realistic_records=len(rows),
                                 instrumented_records=len(sites),
                                 source_locations=len({(s['source'], s['line'], s['column']) for s in sites}),
                                 translation_units=len(tus), source_files=len(by_file)),
                    translation_units=tus, files=files,
                    sites=sorted(sites, key=lambda s: s["site_id"]))
    output = io.StringIO(newline="")
    columns = ["site_id", "translation_unit", "source", "line", "column", "caller", "callee",
               "source_spelling", "mapped_line", "instrumented_line", "runtime_selector", "confidence"]
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(manifest["sites"])
    return {PATCH: patch_bytes, MANIFEST: (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
            SITE_CSV: output.getvalue().encode()}, manifest


def prepare_source(manifest):
    development = PROJECT / "dev/linux-6.12.1"
    expected = {r["path"]: r["after_sha256"] for r in
                json.loads((BASE / "patches/manifest.json").read_text())["files"]}
    expected.update({r["path"]: r["instrumented_sha256"] for r in manifest["files"]})
    if not development.exists():
        development.parent.mkdir(parents=True, exist_ok=True)
        # cp/patch replace touched hardlinks; never write an existing inode in place.
        subprocess.run(["cp", "-al", str(PRISTINE), str(development)], check=True)
        patch_run(development, BASE_PATCH)
        patch_run(development, PATCH)
    issues = [name for name, digest in expected.items()
              if not (development / name).is_file() or sha((development / name).read_bytes()) != digest]
    if issues:
        raise ValueError("refusing mismatched existing source: " + ", ".join(issues))
    print(f"source_verified={development}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify deterministic outputs without changing files")
    parser.add_argument("--prepare-source", action="store_true", help="prepare/verify this experiment's isolated source")
    args = parser.parse_args()
    outputs, manifest = generate()
    for path, data in outputs.items():
        if args.check or args.prepare_source:
            if not path.is_file() or path.read_bytes() != data:
                raise ValueError(f"stale generated output: {path}; run instrument-sites.py first")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    print(json.dumps(manifest["summary"], ensure_ascii=False))
    if args.prepare_source:
        prepare_source(manifest)


if __name__ == "__main__":
    main()
