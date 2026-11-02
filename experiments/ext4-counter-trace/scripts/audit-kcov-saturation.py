#!/usr/bin/env python3
"""Compare a legacy saturated KCOV sample with the new runtime diagnostics."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import pathlib
import re
import struct

HEADER = struct.Struct('<QQQQ')
WORD = struct.Struct('<Q')
PAIR = struct.Struct('<QQ')
MAGIC = 0x43544B434F563031


def infer_legacy_saturation_cause(count: int, ranges: list[tuple[int, int]],
                                  saturated: bool, runtime: str,
                                  default_entries: int) -> dict:
    """Describe legacy evidence without deriving a cause from retained PC counts.

    After a failed realloc truncates one call to the old capacity, a later call
    can allocate successfully and append more PCs. The final count therefore
    need not equal a doubling capacity, even when allocation failure occurred.
    """
    result = dict(saturation_cause='legacy_or_unclassified_saturation',
                  inferred_CT_KCOV_ENTRIES=None,
                  inference_basis='legacy flag does not identify its trigger',
                  allocation_capacities=None)
    if not saturated:
        return dict(saturation_cause='none', inferred_CT_KCOV_ENTRIES=None,
                    inference_basis='sample has no legacy saturated flag',
                    allocation_capacities=None)

    initial_match = re.search(
        r'capacity\s*=\s*ct_kcov_capacity\s*\?\s*ct_kcov_capacity\s*:\s*([0-9]+)',
        runtime)
    ceiling_match = re.search(
        r'while\s*\(capacity\s*<\s*needed\s*&&\s*capacity\s*<\s*([0-9]+)\)\s*capacity\s*\*=\s*2\s*;',
        runtime)
    if not initial_match or not ceiling_match:
        result['inference_basis'] = 'saved runtime allocation limits are not recognized'
        return result

    initial_capacity = int(initial_match[1])
    growth_ceiling = int(ceiling_match[1])
    capacities = []
    capacity = initial_capacity
    while capacity < growth_ceiling:
        capacities.append(capacity)
        capacity *= 2
    capacities.append(capacity)
    result['allocation_capacities'] = capacities

    result.update(
        capacity_boundary_calls=[dict(index=index, start=start, count=length,
                                      end=start + length)
                                 for index, (start, length) in enumerate(ranges)
                                 if start + length in capacities],
        possible_causes=['per_call_buffer_full', 'aggregate_pc_limit',
                         'aggregate_allocation_failure'],
        inference_basis=(
            'legacy flags omit the trigger and actual buffer size; retained call counts '
            'may already be truncated, and a later successful allocation can append '
            'PCs after an earlier failure, so neither the final count nor the largest '
            'retained call identifies the cause or CT_KCOV_ENTRIES'))
    return result


def open_binary(path: pathlib.Path):
    return gzip.open(path, 'rb') if path.suffix == '.gz' else path.open('rb')


def legacy_sample(campaign: pathlib.Path, segment_name: str) -> dict:
    segment = campaign / 'segments' / segment_name
    sample = next(iter(sorted((segment / 'coverage').glob('b1-w0.bin*'))), None)
    if sample is None:
        raise ValueError('legacy_kcov_sample_missing')
    with open_binary(sample) as stream:
        header = stream.read(HEADER.size)
        if len(header) != HEADER.size:
            raise ValueError('legacy_kcov_header_short')
        magic, version, count, flags = HEADER.unpack(header)
        if magic != MAGIC or version != 3:
            raise ValueError('expected_legacy_v3_sample')
        stream.seek(HEADER.size + count * WORD.size)
        call_count_raw = stream.read(WORD.size)
        if len(call_count_raw) != WORD.size:
            raise ValueError('legacy_kcov_call_table_missing')
        call_count, = WORD.unpack(call_count_raw)
        if call_count > 256:
            raise ValueError('legacy_kcov_call_count_invalid')
        ranges_raw = stream.read(call_count * PAIR.size)
        if len(ranges_raw) != call_count * PAIR.size:
            raise ValueError('legacy_kcov_call_table_short')
        ranges = [PAIR.unpack_from(ranges_raw, index * PAIR.size)
                  for index in range(call_count)]
    expected_start = 0
    for start, length in ranges:
        if start != expected_start or start + length > count:
            raise ValueError('legacy_kcov_call_ranges_invalid')
        expected_start = start + length
    if expected_start != count:
        raise ValueError('legacy_kcov_call_ranges_incomplete')
    runtime = (campaign / 'evidence/tests/syz-runtime.h').read_text()
    guest = (campaign / 'evidence/tests/guest-campaign.sh').read_text()
    aggregate_match = re.search(r'capacity < ([0-9]+)', runtime)
    default_match = re.search(r'CT_KCOV_ENTRIES:-([0-9]+)', guest)
    if not aggregate_match or not default_match:
        raise ValueError('legacy_kcov_limits_not_found')
    max_index, max_range = max(enumerate(ranges), key=lambda item: item[1][1])
    count_limit = int(aggregate_match[1])
    default_entries = int(default_match[1])
    manifest = json.loads((segment / 'manifest.json').read_text())
    workload = segment / 'syz-programs' / 'b1-w0' / 'workload.c'
    workload_sha256 = hashlib.sha256(workload.read_bytes()).hexdigest() if workload.is_file() else None
    manifest_workload_sha256 = manifest.get('evidence_sha256', {}).get(
        'syz-programs/b1-w0/workload.c')
    runtime_matches_workload = bool(
        workload_sha256 and manifest_workload_sha256 == workload_sha256 and
        runtime in workload.read_text())
    inference = infer_legacy_saturation_cause(
        count, ranges, bool(flags & 1), runtime, default_entries)
    return dict(campaign=str(campaign.resolve()), segment=segment_name,
        sample=str(sample.relative_to(campaign)), version=version, flags=flags,
        saturated=bool(flags & 1), count=count, calls=call_count,
        maximum_call=dict(index=max_index, count=max_range[1]),
        saved_runtime_aggregate_growth_limit=count_limit,
        saved_guest_default_entries=default_entries,
        count_below_saved_aggregate_limit=count < count_limit,
        max_call_below_saved_guest_default_pc_limit=max_range[1] < default_entries - 1,
        actual_CT_KCOV_ENTRIES_recorded=False,
        generated_workload_sha256=workload_sha256,
        manifest_workload_sha256=manifest_workload_sha256,
        saved_runtime_matches_manifest_workload=runtime_matches_workload,
        **inference)


def current_summaries(campaign: pathlib.Path) -> dict:
    meta = json.loads((campaign / 'campaign.json').read_text())
    summaries = [row for segment in meta.get('segments', [])
                 for row in segment.get('kcov_workload_summaries', [])]
    if not summaries:
        # Per-segment summary JSON also carries this field and supports runs
        # created before the campaign aggregate was refreshed.
        summaries = []
        for path in sorted((campaign / 'segments').glob('segment-*/manifest.json')):
            manifest = json.loads(path.read_text())
            summaries.extend(manifest.get('kcov_workload_summaries', []))
    return dict(campaign=str(campaign.resolve()), workload_summaries=summaries,
                saturation_flags=[row.get('kcov_saturation_flags') for row in summaries],
                all_unsaturated=bool(summaries) and all(
                    row.get('kcov_saturation_flags') == 0 for row in summaries))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('legacy_campaign', type=pathlib.Path)
    parser.add_argument('--legacy-segment', default='segment-001093')
    parser.add_argument('--validation-campaign', type=pathlib.Path)
    parser.add_argument('--output', type=pathlib.Path)
    args = parser.parse_args()
    result = dict(legacy=legacy_sample(args.legacy_campaign.resolve(), args.legacy_segment))
    if args.validation_campaign:
        result['validation'] = current_summaries(args.validation_campaign.resolve())
    rendered = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end='')


if __name__ == '__main__':
    main()
