#!/usr/bin/env python3
"""Validate and summarize a four-arm custom feedback ablation."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
from typing import Any


MODES = ('pc', 'pc_edge', 'pc_semantic', 'pc_edge_semantic')
COMMON_LIMITS = (
    'seed', 'duration_seconds', 'target_syscalls', 'target_kind', 'target_scope',
    'syz_calls', 'resource_valid_every', 'fresh_generate_every',
    'resource_seed_refresh_every', 'resource_mutation_mode', 'scheduler_policy',
    'mutation_budget_policy', 'scenario', 'segment_seconds', 'max_batches',
    'batch_timeout', 'workers', 'memory_mb', 'guest_memory_mb', 'cpus',
    'qemu_cpu', 'buffer_kb', 'max_trace_mb', 'max_run_mb', 'min_free_mb',
    'strict_kcov_feedback', 'effect_aware_admission', 'require_target_success',
)


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'{path} must contain a JSON object')
    return value


def sha256(path: pathlib.Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def arm_summary(mode: str, run: pathlib.Path, expected_seed: int,
                expected_duration: int, expected_scope: str) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    try:
        campaign = read_json(run / 'campaign.json')
        report = read_json(run / 'campaign-report.json')
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {'mode': mode, 'run_dir': str(run), 'readable': False,
                'error': str(error)}, [f'{mode}_campaign_artifact_unreadable']

    limits = campaign.get('limits') or {}
    dependencies = campaign.get('dependencies') or {}
    build = campaign.get('build_sha256') or {}
    start_images = campaign.get('start_images') or {}
    regular_image = start_images.get('regular') or {}
    feedback = report.get('kcov_feedback') or {}
    exploration = report.get('exploration') or {}
    call_effects = feedback.get('call_effect_counts') or {}
    result = {
        'mode': mode,
        'run_dir': str(run.resolve()),
        'readable': True,
        'status': campaign.get('status'),
        'stop_reason': campaign.get('stop_reason'),
        'passed': report.get('passed') is True,
        'coverage_complete': report.get('coverage_complete') is True,
        'raw_coverage_complete': report.get('raw_coverage_complete') is True,
        'kcov_feedback_complete': report.get('kcov_feedback_complete') is True,
        'counter_validation_passed': report.get('counter_validation_passed') is True,
        'issues': report.get('issues') or [],
        'seed': limits.get('seed'),
        'duration_seconds': limits.get('duration_seconds'),
        'target_scope': limits.get('target_scope'),
        'feedback_target_scope': feedback.get('target_scope'),
        'feedback_edge_scope': feedback.get('feedback_edge_scope'),
        'feedback_mode': limits.get('feedback_mode'),
        'generated_syscalls': int(report.get('generated_syscalls', 0) or 0),
        'native_verified': int(report.get('native_verified', 0) or 0),
        'native_success': int(report.get('native_success', 0) or 0),
        'data_success': int(report.get('data_success', 0) or 0),
        'verified_syscalls_per_second': (report.get('performance') or {}).get(
            'all', {}).get('syscalls_per_second'),
        'verified_formula_transitions': int(
            exploration.get('independent_transition_calls', 0) or 0),
        'successful_namespace_operations': int(
            exploration.get('successful_namespace_operations', 0) or 0),
        'target_success_unmodified': int(
            call_effects.get('target_success_unmodified', 0) or 0),
        'effect_target_pc_coverage': int(
            feedback.get('effect_target_pc_coverage', 0) or 0),
        'corpus_target_pc_coverage': int(
            feedback.get('corpus_target_pc_coverage', 0) or 0),
        'target_pc_coverage': int(feedback.get('target_pc_coverage', 0) or 0),
        'effect_target_edge_coverage': int(
            feedback.get('effect_target_edge_coverage', 0) or 0),
        'semantic_features': int(feedback.get('total_semantic_features', 0) or 0),
        'kcov_samples': {
            'expected': int(feedback.get('expected_samples', 0) or 0),
            'complete': int(feedback.get('complete_samples', 0) or 0),
            'missing': int(feedback.get('missing_samples', 0) or 0),
            'invalid': int(feedback.get('invalid_samples', 0) or 0),
            'saturated': int(feedback.get('saturated_samples', 0) or 0),
        },
        'call_effect_counts': call_effects,
        'rootfs_sha256': dependencies.get('rootfs_sha256'),
        'kernel_sha256': build.get('kernel.bzImage'),
        'regular_image_sha256': regular_image.get('sha256'),
        'limits': limits,
        'campaign_elapsed_seconds': campaign.get('elapsed_seconds'),
        'collection_elapsed_seconds': campaign.get('collection_elapsed_seconds'),
    }

    if campaign.get('status') != 'complete' or result['stop_reason'] != 'duration_limit':
        warnings.append(f'{mode}_campaign_did_not_finish_duration')
    if not result['passed'] or not result['coverage_complete'] or \
            not result['raw_coverage_complete'] or not result['kcov_feedback_complete'] or \
            not result['counter_validation_passed']:
        warnings.append(f'{mode}_campaign_validation_incomplete')
    if result['issues']:
        warnings.append(f'{mode}_campaign_has_issues')
    if result['feedback_mode'] != mode:
        warnings.append(f'{mode}_feedback_mode_mismatch')
    if result['seed'] != expected_seed:
        warnings.append(f'{mode}_seed_mismatch')
    if result['duration_seconds'] != expected_duration:
        warnings.append(f'{mode}_duration_mismatch')
    if result['target_scope'] != expected_scope:
        warnings.append(f'{mode}_target_scope_mismatch')
    if result['feedback_target_scope'] != expected_scope or \
            result['feedback_edge_scope'] != expected_scope:
        warnings.append(f'{mode}_feedback_scope_mismatch')
    samples = result['kcov_samples']
    if (samples['expected'] <= 0 or samples['complete'] != samples['expected'] or
            samples['missing'] or samples['invalid'] or samples['saturated']):
        warnings.append(f'{mode}_kcov_sample_integrity_failed')
    if result['native_verified'] != result['generated_syscalls']:
        warnings.append(f'{mode}_native_syscalls_not_fully_verified')
    if (result['target_success_unmodified'] <= 0 or result['data_success'] <= 0 or
            result['successful_namespace_operations'] <= 0):
        warnings.append(f'{mode}_target_operation_activity_missing')

    for key in ('rootfs_sha256', 'kernel_sha256', 'regular_image_sha256'):
        if not result[key]:
            warnings.append(f'{mode}_{key}_missing')
    if not isinstance(limits, dict):
        warnings.append(f'{mode}_limits_missing')
    return result, warnings


def build_report(arms: dict[str, pathlib.Path], seed: int,
                 duration_seconds: int, target_scope: str) -> dict[str, Any]:
    warnings: list[str] = []
    results: dict[str, Any] = {}
    for mode in MODES:
        if mode not in arms:
            warnings.append(f'{mode}_arm_missing')
            results[mode] = {'mode': mode, 'readable': False, 'run_dir': None}
            continue
        summary, arm_warnings = arm_summary(
            mode, arms[mode], seed, duration_seconds, target_scope)
        results[mode] = summary
        warnings.extend(arm_warnings)

    present = [results[mode] for mode in MODES if results[mode].get('readable')]
    if len(present) == len(MODES):
        for field in ('rootfs_sha256', 'kernel_sha256', 'regular_image_sha256'):
            values = {arm.get(field) for arm in present}
            if len(values) != 1:
                warnings.append(f'arms_{field}_mismatch')
        reference = present[0].get('limits') or {}
        for arm in present[1:]:
            limits = arm.get('limits') or {}
            differences = [key for key in COMMON_LIMITS
                           if reference.get(key) != limits.get(key)]
            if differences:
                warnings.append(arm['mode'] + '_common_limits_mismatch:' + ','.join(differences))

    baseline = results.get('pc', {})
    deltas = {}
    for mode in MODES[1:]:
        arm = results.get(mode, {})
        deltas[mode] = {
            'effect_target_pc_delta_vs_pc': (
                arm.get('effect_target_pc_coverage', 0) -
                baseline.get('effect_target_pc_coverage', 0)),
            'corpus_target_pc_delta_vs_pc': (
                arm.get('corpus_target_pc_coverage', 0) -
                baseline.get('corpus_target_pc_coverage', 0)),
            'verified_formula_transition_delta_vs_pc': (
                arm.get('verified_formula_transitions', 0) -
                baseline.get('verified_formula_transitions', 0)),
        }
    warnings = sorted(set(warnings))
    return {
        'schema': 1,
        'experiment': 'four_arm_custom_feedback_ablation',
        'seed': seed,
        'duration_seconds': duration_seconds,
        'target_scope': target_scope,
        'modes': list(MODES),
        'measurement_complete': not warnings,
        'arms': results,
        'deltas_vs_pc': deltas,
        'warnings': warnings,
        'interpretation': 'One seed is a short-run validation, not statistical evidence of feedback benefit.',
    }


def render(report: dict[str, Any]) -> str:
    lines = [f"# Four-arm feedback ablation: seed {report['seed']}", '',
             f"Duration: {report['duration_seconds']} s per arm; target: `{report['target_scope']}`.",
             f"Measurement complete: **{str(report['measurement_complete']).lower()}**.", '',
             '| Arm | Passed | Verified calls | Effect target PC | Corpus target PC | Target edges | Semantic | Formula transitions | Verified calls/s |',
             '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for mode in MODES:
        arm = report['arms'][mode]
        lines.append(
            f"| {mode} | {arm.get('passed', False)} | {arm.get('native_verified', 0)} | "
            f"{arm.get('effect_target_pc_coverage', 0)} | {arm.get('corpus_target_pc_coverage', 0)} | "
            f"{arm.get('effect_target_edge_coverage', 0)} | {arm.get('semantic_features', 0)} | "
            f"{arm.get('verified_formula_transitions', 0)} | "
            f"{arm.get('verified_syscalls_per_second') or 0} |")
    if report['warnings']:
        lines += ['', '## Validation warnings', '']
        lines.extend(f'- `{warning}`' for warning in report['warnings'])
    lines += ['', report['interpretation'], '']
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arm', action='append', default=[], metavar='MODE=RUN_DIR')
    parser.add_argument('--output', type=pathlib.Path, required=True)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--duration-seconds', type=int, required=True)
    parser.add_argument('--target-scope', choices=('all', 'ext4_jbd2'), required=True)
    args = parser.parse_args()
    arms: dict[str, pathlib.Path] = {}
    for item in args.arm:
        if '=' not in item:
            parser.error('--arm must be MODE=RUN_DIR')
        mode, path = item.split('=', 1)
        if mode not in MODES:
            parser.error(f'unknown feedback mode: {mode}')
        if mode in arms:
            parser.error(f'duplicate feedback mode: {mode}')
        arms[mode] = pathlib.Path(path)
    report = build_report(arms, args.seed, args.duration_seconds, args.target_scope)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    args.output.with_suffix('.md').write_text(render(report), encoding='utf-8')
    print(json.dumps({key: report[key] for key in
                      ('measurement_complete', 'deltas_vs_pc', 'warnings')}, indent=2))
    return 0 if report['measurement_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
