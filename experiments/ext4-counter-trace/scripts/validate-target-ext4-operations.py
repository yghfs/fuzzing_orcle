#!/usr/bin/env python3
"""Validate successful, unrepaired file operations on a campaign's regular ext4.

The syscall lifecycle report is the evidence source. A call contributes to the
target count only when its native return was correlated, the return succeeded,
the executor did not repair its fd, and the observed object device is the
campaign's regular ext4 device.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import pathlib
from typing import Any


DATA_CALLS = {
    'write', 'pwrite64', 'fallocate', 'ftruncate', 'fsync', 'fdatasync',
}
NAMESPACE_CALLS = {
    'mkdirat', 'rmdir', 'unlinkat', 'renameat2', 'linkat', 'symlinkat',
}
FILE_CALLS = DATA_CALLS | NAMESPACE_CALLS


def load_json(path: pathlib.Path) -> Any:
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as stream:
        return json.load(stream)


def canonical_device(value: Any) -> str | None:
    """Normalize campaign major,minor and trace st_dev values to major:minor."""
    try:
        if isinstance(value, str) and ',' in value:
            major, minor = (int(part) for part in value.split(',', 1))
        elif isinstance(value, str) and ':' in value:
            major, minor = (int(part) for part in value.split(':', 1))
        else:
            device = int(value)
            major, minor = os.major(device), os.minor(device)
    except (TypeError, ValueError, OverflowError):
        return None
    if major < 0 or minor < 0:
        return None
    return f'{major}:{minor}'


def lifecycle_path(segment: pathlib.Path) -> pathlib.Path | None:
    # The compressed report is the finalized artifact if both forms exist.
    for name in ('report.json.gz', 'report.json'):
        path = segment / 'analysis' / 'lifecycle' / name
        if path.is_file():
            return path
    return None


def new_category() -> dict[str, Any]:
    return {
        'calls_seen': 0,
        'verified_successful_on_regular_unrepaired': 0,
        'rejections': dict(unverified=0, failed_return=0, repaired=0,
                           device_mismatch=0, missing_device=0),
    }


def validate(campaign_dir: pathlib.Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    campaign_path = campaign_dir / 'campaign.json'
    try:
        campaign = load_json(campaign_path)
        if not isinstance(campaign, dict):
            raise ValueError('campaign.json must contain an object')
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return {
            'schema': 1,
            'campaign_dir': str(campaign_dir),
            'status': 'unreadable',
            'evidence_readable': False,
            'evidence_complete': False,
            'errors': [f'campaign_metadata_unreadable:{error}'],
            'warnings': [],
            'target_device': None,
            'segments': {'discovered': 0, 'reports_read': 0,
                         'missing_reports': 0, 'unreadable_reports': 0,
                         'lifecycle_failed': 0},
            'operations': {'data': new_category(), 'namespace': new_category(),
                           'target_success_total': 0},
        }

    devices = campaign.get('devices')
    target_device = canonical_device(devices.get('regular') if isinstance(devices, dict) else None)
    if target_device is None:
        errors.append('regular_device_identity_missing_or_invalid')

    operation_counts = {'data': new_category(), 'namespace': new_category()}
    segments = sorted(path for path in (campaign_dir / 'segments').glob('segment-*') if path.is_dir())
    reports_read = missing_reports = unreadable_reports = lifecycle_failed = 0
    evidence_complete = True

    if not segments:
        warnings.append('no_segment_directories_found')
        evidence_complete = False

    for segment in segments:
        manifest_path = segment / 'manifest.json'
        try:
            manifest = load_json(manifest_path)
            if not isinstance(manifest, dict):
                raise ValueError('manifest must contain an object')
        except (OSError, json.JSONDecodeError, ValueError) as error:
            errors.append(f'{segment.name}:manifest_unreadable:{error}')
            evidence_complete = False

        report_path = lifecycle_path(segment)
        if report_path is None:
            missing_reports += 1
            errors.append(f'{segment.name}:lifecycle_report_missing')
            evidence_complete = False
            continue
        try:
            report = load_json(report_path)
            if not isinstance(report, dict):
                raise ValueError('lifecycle report must contain an object')
            calls = report.get('native_syscalls')
            if not isinstance(calls, list):
                raise ValueError('native_syscalls list missing')
        except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError, ValueError) as error:
            unreadable_reports += 1
            errors.append(f'{segment.name}:lifecycle_report_unreadable:{error}')
            evidence_complete = False
            continue

        reports_read += 1
        if report.get('passed') is not True:
            lifecycle_failed += 1
            warnings.append(f'{segment.name}:lifecycle_report_failed')
        syscall_issues = report.get('syscall_issues')
        if syscall_issues:
            evidence_complete = False
            warnings.append(f'{segment.name}:syscall_correlation_issues')

        for call in calls:
            if not isinstance(call, dict):
                evidence_complete = False
                warnings.append(f'{segment.name}:malformed_syscall_record')
                continue
            name = call.get('name')
            if name in DATA_CALLS:
                category = operation_counts['data']
            elif name in NAMESPACE_CALLS:
                category = operation_counts['namespace']
            else:
                continue
            category['calls_seen'] += 1

            if call.get('verified') is not True:
                category['rejections']['unverified'] += 1
                evidence_complete = False
                continue
            raw_result = call.get('raw_result')
            if not isinstance(raw_result, int):
                category['rejections']['unverified'] += 1
                evidence_complete = False
                continue
            if raw_result < 0:
                category['rejections']['failed_return'] += 1
                continue
            if call.get('repaired') != 0:
                category['rejections']['repaired'] += 1
                continue
            object_device = canonical_device(call.get('object_dev'))
            if object_device is None:
                category['rejections']['missing_device'] += 1
                continue
            if target_device is None or object_device != target_device:
                category['rejections']['device_mismatch'] += 1
                continue
            category['verified_successful_on_regular_unrepaired'] += 1

    target_success_total = sum(
        category['verified_successful_on_regular_unrepaired']
        for category in operation_counts.values())
    campaign_status = campaign.get('status')
    if campaign_status != 'complete':
        warnings.append(f'campaign_status_{campaign_status or "missing"}')
    evidence_readable = unreadable_reports == 0 and missing_reports == 0 and not any(
        error.startswith('campaign_metadata_unreadable:') for error in errors)
    if target_device is None:
        evidence_readable = False
    if target_success_total == 0:
        errors.append('no_verified_successful_unrepaired_file_operation_on_regular_device')
    for category_name, category in operation_counts.items():
        if category['verified_successful_on_regular_unrepaired'] == 0:
            errors.append(f'no_verified_successful_unrepaired_{category_name}_operation_on_regular_device')

    required_operation_categories_verified = all(
        category['verified_successful_on_regular_unrepaired'] > 0
        for category in operation_counts.values())

    return {
        'schema': 1,
        'campaign_dir': str(campaign_dir),
        'status': 'passed' if evidence_readable and required_operation_categories_verified else 'failed',
        'campaign_status': campaign_status,
        'evidence_readable': evidence_readable,
        'evidence_complete': evidence_complete and reports_read == len(segments),
        'target_device': target_device,
        'segments': {
            'discovered': len(segments),
            'reports_read': reports_read,
            'missing_reports': missing_reports,
            'unreadable_reports': unreadable_reports,
            'lifecycle_failed': lifecycle_failed,
        },
        'operations': {
            **operation_counts,
            'target_success_total': target_success_total,
            'required_data_and_namespace_categories_verified': required_operation_categories_verified,
        },
        'errors': errors,
        'warnings': sorted(set(warnings)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', type=pathlib.Path, required=True,
                        help='campaign output directory containing campaign.json and segments/')
    parser.add_argument('--output', type=pathlib.Path,
                        help='optional JSON report path (defaults to stdout only)')
    args = parser.parse_args(argv)
    report = validate(args.campaign)
    encoded = json.dumps(report, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding='utf-8')
    print(encoded, end='')
    return 0 if (report['evidence_readable'] and
                 report['operations']['required_data_and_namespace_categories_verified']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
