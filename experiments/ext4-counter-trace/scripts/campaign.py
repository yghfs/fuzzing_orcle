#!/usr/bin/env python3
"""Bounded campaign evidence helpers and aggregation; raw evidence is immutable."""
from __future__ import annotations
import collections
import gzip
import hashlib
import json
import pathlib
import re
from trace_lifecycle import CLASSES, PREFIX, fields

KINDS = ('regular', 'bigalloc', 'inline')
LOSS_KEYS = ('overrun', 'commit overrun', 'dropped events')
DATA_CALLS = {'write', 'pwrite64', 'fallocate', 'ftruncate', 'fsync', 'fdatasync'}


def atomic_json(path, value):
    path = pathlib.Path(path)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        import os
        os.fsync(stream.fileno())
    tmp.replace(path)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def digest(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def read_json(path):
    path = pathlib.Path(path)
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt') as stream:
        return json.load(stream)


def text_stream(path):
    path = pathlib.Path(path)
    return gzip.open(path, 'rt', errors='replace') if path.suffix == '.gz' else path.open(errors='replace')


def raw_identity(path):
    """Sequence order may differ between CPUs; completeness uses the set."""
    domains = {}
    with text_stream(path) as stream:
        for number, line in enumerate(stream, 1):
            if 'ext4_counter_event:' not in line:
                continue
            match = PREFIX.search(line.strip())
            if not match:
                raise ValueError(f'undecodable_counter_line:{number}')
            e = fields(match['body'])
            key = (e['dev'], e['epoch'])
            d = domains.setdefault(key, {'seqs': set(), 'duplicates': 0})
            if e['seq'] in d['seqs']:
                d['duplicates'] += 1
            d['seqs'].add(e['seq'])
    result = []
    for (dev, epoch), d in sorted(domains.items()):
        seqs = d['seqs']
        result.append(dict(dev=dev, epoch=epoch, first=min(seqs), last=max(seqs),
                           count=len(seqs), duplicates=d['duplicates'],
                           holes=max(seqs)-min(seqs)+1-len(seqs)))
    return result


def stats(path):
    return {k: int(v) for k, v in re.findall(r'^([\w ]+):\s*(\d+)$', pathlib.Path(path).read_text(), re.M)}


def compress(path, level=6):
    """Never replace the sole copy until compression has finished."""
    path = pathlib.Path(path)
    target = path.with_name(path.name + '.gz')
    tmp = target.with_name(target.name + '.tmp')
    import shutil
    with path.open('rb') as source, gzip.open(tmp, 'wb', compresslevel=level) as dest:
        shutil.copyfileobj(source, dest, 1024 * 1024)
    tmp.replace(target)
    path.unlink()
    return target


def aggregate(run, output=None, reanalyzed=None):
    run = pathlib.Path(run)
    output = pathlib.Path(output) if output else run
    output.mkdir(parents=True, exist_ok=True)
    meta = read_json(run / 'campaign.json')
    issues, summaries, totals = [], [], collections.Counter()
    event_sources = collections.Counter()
    source_attribution_issues = []
    dimensions = collections.defaultdict(collections.Counter)
    concurrency = collections.Counter()
    identities, last = {}, {}
    expected = list(range(meta.get('segments_started', 0)))
    manifests = sorted((run / 'segments').glob('segment-*/manifest.json'))
    ids = []
    episodes_path = output / 'episodes.jsonl'
    summaries_path = output / 'segment-summary.jsonl'
    budget = None
    if output.resolve().is_relative_to(run.resolve()) and meta.get('limits', {}).get('max_run_mb'):
        used = sum(p.stat().st_size for p in run.rglob('*') if p.is_file())
        # Leave space for manifests/report; archived episodes are always retained.
        budget = max(0, meta['limits']['max_run_mb']*1024**2 - used - max(512*1024, len(manifests)*1024))
    def emit(stream, row):
        nonlocal budget
        data = json.dumps(row, separators=(',', ':')) + '\n'
        size = len(data.encode())
        if budget is not None:
            if size > budget:
                if 'aggregate_output_quota' not in issues:
                    issues.append('aggregate_output_quota')
                return
            budget -= size
        stream.write(data)
    # Regeneration reads only one bounded segment at a time.
    with episodes_path.open('w') as episodes_out, summaries_path.open('w') as summaries_out:
        for path in manifests:
            m = read_json(path); ids.append(m['segment_id'])
            folder = path.parent; local = []
            ident = f'segment-{m["segment_id"]:06d}'
            if m.get('run_id') != meta['run_id']:
                local.append('run_id_changed')
            if m.get('boot_id') != meta.get('boot_id'):
                local.append('boot_id_changed_without_new_campaign')
            if m.get('status') != 'complete':
                local.append('segment_' + m.get('status', 'unknown'))
            for rel, expected_hash in m.get('evidence_sha256', {}).items():
                evidence = folder / rel
                if not evidence.is_file() or digest(evidence) != expected_hash:
                    local.append('missing_or_changed_evidence:' + rel)
            raw = folder / m.get('trace_file', 'trace.txt')
            if not raw.is_file() or not raw.stat().st_size:
                local.append('missing_or_empty_trace')
                ranges = []
            else:
                try:
                    ranges = raw_identity(raw)
                except (ValueError, KeyError, OSError, EOFError) as error:
                    local.append('invalid_raw_trace:' + str(error)); ranges = []
            if ranges != m.get('raw_domains', []):
                local.append('raw_identity_manifest_disagrees')
            for d in ranges:
                if d['dev'] not in meta.get('devices', {}).values():
                    continue  # rootfs activity is retained, outside experiment scope
                domain = (m.get('boot_id'), d['dev'])
                previous = last.get(domain)
                if d['duplicates'] or d['holes']:
                    local.append(f'raw_sequence_loss_or_duplicate:{d["dev"]}')
                if previous and (d['epoch'] != previous['epoch'] or
                                 d['first'] != previous['last']+1):
                    local.append(f'cross_segment_epoch_or_sequence_discontinuity:{d["dev"]}')
                if domain not in identities:
                    identities[domain] = d.copy()
                last[domain] = d
            for cpu, s in m.get('loss_delta', {}).items():
                if any(s.get(k, -1) != 0 for k in LOSS_KEYS):
                    local.append('trace_loss:' + cpu)
            expected_cpus = meta.get('trace_cpus', meta.get('limits', {}).get('cpus', 2))
            if not m.get('loss_delta') or len(m['loss_delta']) != expected_cpus:
                local.append('missing_cpu_loss_evidence')
            report_path = folder / m.get('lifecycle_report', 'analysis/lifecycle/report.json.gz')
            if reanalyzed is not None:
                report_path = pathlib.Path(reanalyzed) / folder.name / 'lifecycle/report.json'
                if not report_path.exists():
                    report_path = report_path.with_name('report.json.gz')
            report = {}
            if report_path.is_file():
                try:
                    report = read_json(report_path)
                except (ValueError, OSError, EOFError) as error:
                    local.append('invalid_lifecycle_report:' + str(error))
            else:
                local.append('missing_lifecycle_report')
            if not report.get('passed'):
                local.append('lifecycle_validation_failed')
            if m.get('source') == 'syzkaller':
                from syz_workload import execution_issues
                try:
                    local.extend(execution_issues(folder / 'syz-programs', report.get('native_syscalls', []),
                                                  folder.name, m.get('batches', 0), m['workers']))
                except (ValueError, KeyError, OSError) as error:
                    local.append('invalid_syzkaller_execution_evidence:' + str(error))
            local.extend(m.get('issues', []))
            counts = report.get('classification_counts', {})
            totals.update(counts)
            interval_reports = report.get('runs', [])
            interleavings = [pair for interval in interval_reports
                             for pair in interval.get('interleavings', [])]
            scope_interleavings = [pair for interval in interval_reports
                                   for pair in interval.get('scope_interleavings', [])]
            native_interleavings = [pair for interval in interval_reports
                                    for pair in interval.get('native_syscall_interleavings', [])]
            # Older lifecycle reports only carried the de-duplicated union.
            # Recover its attribution from `basis` when the dedicated arrays
            # are absent, while preserving union counts for compatibility.
            if not scope_interleavings:
                scope_interleavings = [pair for pair in interleavings
                                       if 'ext4_scope_interval' in pair.get('basis', [])]
            if not native_interleavings:
                native_interleavings = [pair for pair in interleavings
                                        if 'native_syscall_interval' in pair.get('basis', [])]
            stable_samples = [sample for interval in interval_reports
                              for sample in interval.get('snapshots', [])]
            concurrency['shared_segments'] += m.get('object_sharing') == 'shared'
            concurrency['operation_pairs'] += len(interleavings)
            concurrency['shared_object_pairs'] += sum(bool(pair.get('shared_objects'))
                                                        for pair in interleavings)
            concurrency['scope_operation_pairs'] += len(scope_interleavings)
            concurrency['scope_shared_object_pairs'] += sum(bool(pair.get('shared_objects'))
                                                              for pair in scope_interleavings)
            concurrency['native_syscall_pairs'] += len(native_interleavings)
            concurrency['native_shared_object_pairs'] += sum(bool(pair.get('shared_objects'))
                                                               for pair in native_interleavings)
            concurrency['quiescent_valid_snapshots'] += sum(
                sample.get('quiescent', False) and sample.get('valid', False)
                for sample in stable_samples)
            concurrency['rejected_or_interrupted_snapshots'] += sum(
                not sample.get('quiescent', False) or not sample.get('valid', False)
                for sample in stable_samples)
            source_report = report.get('event_sources', {})
            if isinstance(source_report, dict) and 'counts' in source_report:
                source_report = source_report.get('counts', {})
            event_sources.update(source_report)
            source_attribution_issues.extend(report.get('event_sources', {}).get('issues', [])
                                             if isinstance(report.get('event_sources'), dict) else [])
            dimensions[m.get('kind', 'unknown') + '/' + m.get('mode', 'unknown')].update(counts)
            for interval in report.get('runs', []):
                for episode in interval.get('episodes', []):
                    row = dict(episode, run_id=meta['run_id'], boot_id=m.get('boot_id'),
                               segment_id=m['segment_id'], kind=m.get('kind'),
                               mode=m.get('mode'), interval=interval['name'],
                               segment_complete=not local)
                    emit(episodes_out, row)
            summary = {k: m.get(k) for k in ('segment_id', 'status', 'kind', 'mode', 'seed',
                       'workers', 'memory_mb', 'batches', 'trace_bytes', 'start_wall_ns', 'end_wall_ns',
                       'kcov_feedback_errors', 'generated_syscalls', 'scenario', 'scenario_evidence',
                       'kcov_workload_summaries', 'fallocate_adaptations')}
            summary['object_sharing'] = m.get('object_sharing', 'private')
            summary['interleavings'] = dict(operation_pairs=len(interleavings),
                                            shared_object_pairs=sum(bool(pair.get('shared_objects'))
                                                                    for pair in interleavings),
                                            scope_operation_pairs=len(scope_interleavings),
                                            scope_shared_object_pairs=sum(bool(pair.get('shared_objects'))
                                                                          for pair in scope_interleavings),
                                            native_syscall_pairs=len(native_interleavings),
                                            native_shared_object_pairs=sum(bool(pair.get('shared_objects'))
                                                                           for pair in native_interleavings),
                                            quiescent_valid_snapshots=sum(
                                                sample.get('quiescent', False) and sample.get('valid', False)
                                                for sample in stable_samples),
                                            rejected_or_interrupted_snapshots=sum(
                                                not sample.get('quiescent', False) or not sample.get('valid', False)
                                                for sample in stable_samples))
            summary.update(coverage_complete=not local, issues=sorted(set(local)),
                           stage_timings=m.get('stage_timings', {}),
                           segment_elapsed_seconds=m.get('segment_elapsed_seconds'),
                           classification_counts=counts,
                           native_syscalls=len(report.get('native_syscalls', [])),
                           native_verified=sum(c.get('verified', False) for c in report.get('native_syscalls', [])),
                           native_success=sum(isinstance(c.get('raw_result'), int) and c['raw_result'] >= 0
                                              for c in report.get('native_syscalls', [])),
                           data_calls=m.get('data_calls', 0), fd_repairs=m.get('fd_repairs', 0),
                           fallocate_adaptations=m.get('fallocate_adaptations', 0),
                           data_syscalls=sum(c.get('name') in DATA_CALLS for c in report.get('native_syscalls', [])),
                           data_success=sum(c.get('name') in DATA_CALLS and
                                            isinstance(c.get('raw_result'), int) and c['raw_result'] >= 0
                                            for c in report.get('native_syscalls', [])))
            summary['exploration'] = report.get('exploration', {})
            summaries.append(summary)
            emit(summaries_out, summary)
            issues.extend(ident + ':' + x for x in local)
    if ids != expected:
        issues.append('missing_duplicate_or_noncontinuous_segment_ids')
    if set(meta.get('devices', {})) != set(KINDS):
        issues.append('missing_campaign_device_identity')
    scheduled_kinds = {summary.get('kind') for summary in summaries}
    for kind, dev in meta.get('devices', {}).items():
        domain = (meta.get('boot_id'), dev)
        initial = meta.get('initial_states', {}).get(kind)
        final = meta.get('final_states', {}).get(kind)
        first, end = identities.get(domain), last.get(domain)
        if (kind not in scheduled_kinds and initial and final and not first and not end and
                initial.get('epoch') == final.get('epoch') and
                initial.get('seq') == final.get('seq')):
            # A target-kind campaign still mounts, snapshots, fscks, and
            # validates every disk, but it intentionally schedules workload
            # only on the selected filesystem. An untouched device has no
            # trace domain; identical stable endpoints prove no activity was
            # lost from its counter stream.
            pass
        elif not initial or not final or not first or not end:
            issues.append(kind + ':missing_campaign_endpoints')
        elif (initial['epoch'] != first['epoch'] or first['first'] != initial['seq']+1 or
              final['epoch'] != end['epoch'] or end['last'] != final['seq']):
            issues.append(kind + ':campaign_endpoint_discontinuity')
        if initial and initial.get('active') != 0 or final and final.get('active') != 0:
            issues.append(kind + ':active_campaign_endpoint')
    native_verified_total = sum(s['native_verified'] for s in summaries)
    target_syscalls = meta.get('limits', {}).get('target_syscalls', 0)
    if target_syscalls and native_verified_total < target_syscalls:
        issues.append('verified_syscall_budget_not_reached')
    raw_complete = bool(summaries) and set(meta.get('devices', {})) == set(KINDS) and not any('sequence' in x or 'loss' in x or
                    'endpoint' in x or 'raw_' in x or 'segment_ids' in x or 'boot_id' in x or
                    'evidence' in x or 'trace' in x or 'run_id' in x for x in issues)
    coverage = bool(summaries) and not issues and meta.get('status') == 'complete'
    disk_issues = []
    for kind in KINDS:
        evidence = meta.get('disk', {}).get(kind, {})
        if (not evidence.get('valid') or not evidence.get('matches_final_stable_snapshot') or
                evidence.get('fsck_exit') != 0):
            disk_issues.append(kind + ':missing_or_invalid_disk_confirmation')
        path = run / (kind + '-disk.json')
        if not path.is_file() or (evidence.get('sha256') and digest(path) != evidence['sha256']):
            disk_issues.append(kind + ':missing_or_changed_disk_evidence')
        elif path.is_file() and not read_json(path).get('valid'):
            disk_issues.append(kind + ':invalid_disk_payload')
        snap = run / 'checkpoint' / (kind + '-final.json')
        fsck = run / (kind + '-fsck.txt')
        for p, key in ((snap, 'snapshot_sha256'), (fsck, 'fsck_sha256')):
            if not p.is_file() or not evidence.get(key) or digest(p) != evidence[key]:
                disk_issues.append(kind + ':missing_or_changed_' + key)
        if path.is_file() and snap.is_file():
            sample, disk = read_json(snap), read_json(path)
            try:
                observed = dict(free_clusters=disk['superblock']['free_clusters'],
                                free_inodes=disk['superblock']['free_inodes'],
                                dirs=disk['descriptor_totals']['dirs'])
                if not sample.get('valid') or not sample.get('namespace_valid') or any(
                        sample[k] != v for k, v in observed.items()):
                    disk_issues.append(kind + ':disk_final_snapshot_disagrees')
            except KeyError:
                disk_issues.append(kind + ':incomplete_disk_snapshot_payload')
    if not meta.get('clean_shutdown'):
        disk_issues.append('unclean_or_unconfirmed_shutdown')
    kcov_gaps = [dict(segment_id=s['segment_id'], errors=s['kcov_feedback_errors'])
                 for s in summaries if s.get('kcov_feedback_errors')]
    feedback_path = run / 'feedback/coverage.json'
    kcov_summary = read_json(feedback_path) if feedback_path.is_file() else meta.get('kcov_feedback', {})
    kcov_complete = (kcov_summary.get('schema', 0) >= 3 and
                     bool(kcov_summary.get('feedback_complete', kcov_summary.get('samples_complete'))) and
                     not kcov_gaps)
    if kcov_summary and kcov_summary.get('schema', 0) < 3:
        kcov_gaps.append(dict(segment_id='campaign', errors=['legacy_feedback_status_unknown']))
    source_complete = bool(event_sources) and not source_attribution_issues and not any(
        event_sources.get(name) for name in ('background_unattributed', 'outside_generated_call'))
    if meta.get('status') != 'complete':
        issues.append('campaign_' + meta.get('status', 'unknown'))
    kernel_fault = meta.get('kernel_fault')
    if kernel_fault:
        issues.append(kernel_fault.get('kind', 'kernel_fault'))
    counter_validation_passed = (coverage and not disk_issues and not kernel_fault)
    strict_kcov = bool(meta.get('limits', {}).get('strict_kcov_feedback'))
    if strict_kcov and not kcov_complete:
        issues.append('kcov_feedback_incomplete')
    passed = counter_validation_passed and (not strict_kcov or kcov_complete)
    bounds = [s for s in summaries if s.get('start_wall_ns') and s.get('end_wall_ns')]
    collection_elapsed = meta.get('collection_elapsed_seconds')
    if collection_elapsed is None and bounds:
        collection_elapsed = round((max(s['end_wall_ns'] for s in bounds)-min(s['start_wall_ns'] for s in bounds))/10**9,3)
    report = dict(schema=1, run_id=meta['run_id'], boot_id=meta.get('boot_id'), passed=passed,
                  status='passed' if passed else 'incomplete_or_failed',
                  raw_coverage_complete=raw_complete, coverage_complete=coverage,
                  segments=len(summaries), complete_segments=sum(s['coverage_complete'] for s in summaries),
                  elapsed_seconds=meta.get('elapsed_seconds'), collection_elapsed_seconds=collection_elapsed,
                  stop_reason=meta.get('stop_reason'),
                  classification_counts={c: totals[c] for c in CLASSES},
                  by_kind_mode={k: dict(v) for k, v in sorted(dimensions.items())},
                  native_syscalls=sum(s['native_syscalls'] for s in summaries),
                  native_verified=native_verified_total,
                  native_success=sum(s.get('native_success', 0) for s in summaries),
                  generated_syscalls=sum(s.get('generated_syscalls', 0) or 0 for s in summaries),
                  target_syscalls=target_syscalls,
                  data_calls=sum(s.get('data_calls', 0) for s in summaries),
                  fd_repairs=sum(s.get('fd_repairs', 0) for s in summaries),
                  fallocate_adaptations=sum(s.get('fallocate_adaptations', 0) for s in summaries),
                  data_syscalls=sum(s.get('data_syscalls', 0) for s in summaries),
                  data_success=sum(s.get('data_success', 0) for s in summaries),
                  exploration=dict(
                      independent_transition_calls=sum(s.get('exploration', {}).get('independent_transition_calls', 0)
                                                       for s in summaries),
                      successful_namespace_operations=sum(s.get('exploration', {}).get('successful_namespace_operations', 0)
                                                          for s in summaries),
                      failed_namespace_operations=sum(s.get('exploration', {}).get('failed_namespace_operations', 0)
                                                       for s in summaries),
                      repaired_calls=sum(s.get('exploration', {}).get('repaired_calls', 0) for s in summaries),
                      syscall_stats={}),
                  issues=sorted(set(issues + disk_issues)), disk=meta.get('disk', {}),
                  kernel_fault=kernel_fault, kcov_feedback_gaps=kcov_gaps,
                  counter_validation_passed=counter_validation_passed,
                  kcov_feedback_complete=kcov_complete,
                  kcov_feedback=kcov_summary,
                  concurrency=dict(concurrency),
                  event_sources=dict(event_sources), source_attribution_complete=source_complete,
                  source_attribution_issues=sorted(set(source_attribution_issues)),
                  limitations=['no_cross_thread_cause_propagation',
                               'recording_order_is_not_cross_cpu_mutation_order',
                               'shutdown_replay_is_a_separate_domain',
                               'new_boot_requires_new_campaign_and_new_stable_anchors',
                               'disk_layout_support_is_bounded_by_check_disk_validation',
                               'validation_duration_does_not_prove_absence_of_long_term_bugs'])
    # Merge per-syscall counters so A/B runs can compare effective operation
    # density without reconstructing every lifecycle report.
    merged_syscalls = {}
    for summary in summaries:
        for name, row in summary.get('exploration', {}).get('syscalls', {}).items():
            dst = merged_syscalls.setdefault(name, dict(executions=0, successful=0, failed=0,
                repaired=0, counter_event_calls=0, independent_transition_calls=0,
                successful_transition_calls=0, failed_transition_calls=0, errors={}, point_hits={}))
            for key in ('executions', 'successful', 'failed', 'repaired', 'counter_event_calls',
                        'independent_transition_calls', 'successful_transition_calls',
                        'failed_transition_calls'):
                dst[key] += row.get(key, 0)
            for key in ('errors', 'point_hits'):
                for item, count in row.get(key, {}).items():
                    dst[key][item] = dst[key].get(item, 0) + count
    for row in merged_syscalls.values():
        n = row['executions']
        row['success_rate'] = row['successful'] / n if n else 0.0
        row['event_rate'] = row['counter_event_calls'] / n if n else 0.0
        row['conversion_density'] = row['independent_transition_calls'] / n if n else 0.0
        row['successful_conversion_density'] = (row['successful_transition_calls'] / row['successful']
                                                 if row['successful'] else 0.0)
    report['exploration']['syscall_stats'] = dict(sorted(merged_syscalls.items()))
    report['exploration']['namespace_stats'] = {name: merged_syscalls.get(name, {}) for name in
                                                ('linkat', 'unlinkat', 'rmdir', 'renameat2', 'mkdirat', 'symlinkat')}
    from point_report import campaign_points
    if budget is None or budget > 16*1024**2:
        points = campaign_points(run, output, reanalyzed, coverage)
    else:
        points = {'observed_hits': 0, 'counting_scope': 'not_exported_due_to_output_quota',
                  'issues': ['point_output_quota']}
    report['point_checks'] = {'schema': 1, 'observed_hits': points['observed_hits'],
                             'counting_scope': points['counting_scope'],
                             'report': 'point-report.md', 'interactive_report': 'point-report.html',
                             'statistics': 'point-report.json', 'hits': 'point-hits.jsonl.gz',
                             'issues': points['issues']}
    atomic_json(output / 'campaign-report.json', report)
    lines = [f'# ext4 campaign {meta["run_id"]}', '',
             f'Counter validation: {"PASS" if passed else "INCOMPLETE / FAIL"}',
             f'KCOV feedback complete: {kcov_complete} (separate from counter validation).',
             f'Source attribution complete: {source_complete} (separate from both).',
             f'Complete segments: {report["complete_segments"]}/{len(summaries)}; '
             f'native syscall pairs: {report["native_verified"]}/{report["native_syscalls"]}.', '',
             f'Generated syscall budget: {report["generated_syscalls"]}; '
             f'target: {report["target_syscalls"] or "disabled"}.', '',
             f'Native successful returns: {report["native_success"]}/{report["native_syscalls"]}; '
             f'data syscalls successful: {report["data_success"]}/{report["data_syscalls"]}; '
             f'fd repairs: {report["fd_repairs"]}; independent transitions: '
             f'{report["exploration"]["independent_transition_calls"]}; namespace successes/failures: '
             f'{report["exploration"]["successful_namespace_operations"]}/'
             f'{report["exploration"]["failed_namespace_operations"]}.', '',
             '## Syscall 有效转换密度', '',
             '转换密度 = 触发独立公式转换的调用数 / 已观测调用数；成功转换密度只在 syscall 返回成功的调用中计算。', '',
             '| syscall | 调用 | 成功 | 失败 | 成功率 | 公式转换 | 转换密度 | 成功转换密度 |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for name, row in report['exploration'].get('syscall_stats', {}).items():
        lines.append(f'| {name} | {row.get("executions",0)} | {row.get("successful",0)} | '
                     f'{row.get("failed",0)} | {row.get("success_rate",0):.2%} | '
                     f'{row.get("independent_transition_calls",0)} | {row.get("conversion_density",0):.2%} | '
                     f'{row.get("successful_conversion_density",0):.2%} |')
    lines += [
             '',
             f'Elapsed seconds: {report["elapsed_seconds"]}; stop: {report["stop_reason"]}.', '',
             '',
             '## 并发交错与稳定快照', '',
             f'共享对象模式 segment：{report["concurrency"].get("shared_segments", 0)}；'
             f'ext4 scope 交错：{report["concurrency"].get("scope_operation_pairs", 0)} 对'
             f'（共享 inode/目录对象 {report["concurrency"].get("scope_shared_object_pairs", 0)} 对）；'
             f'native syscall 时间窗交错：{report["concurrency"].get("native_syscall_pairs", 0)} 对'
             f'（共享 inode/目录对象 {report["concurrency"].get("native_shared_object_pairs", 0)} 对）；'
             f'去重并集：{report["concurrency"].get("operation_pairs", 0)} 对，'
             f'其中共享对象 {report["concurrency"].get("shared_object_pairs", 0)} 对。', '',
             f'有效静止快照：{report["concurrency"].get("quiescent_valid_snapshots", 0)}；'
             f'中断或无效快照：{report["concurrency"].get("rejected_or_interrupted_snapshots", 0)}。', '',
             '| Classification | Episodes |', '| --- | ---: |']
    lines += [f'| {c} | {totals[c]} |' for c in CLASSES]
    lines += ['', '## 插桩点逐次检查', '',
              f'实验设备原始事件命中：{points["observed_hits"]:,} 次，按插桩点和公式逐次统计，带入锚点不重复计数。',
              '[现场与重建比较汇总](point-report.md) · [交互明细](point-report.html) · '
              '[全部逐次 CSV](point-hits.csv.gz)', '']
    lines += ['', '| Segment | Kind / mode | Complete | Issues |', '| --- | --- | --- | --- |']
    lines += [f'| {s["segment_id"]} | {s["kind"]}/{s["mode"]} | {s["coverage_complete"]} | '
              + ', '.join(s['issues']) + ' |' for s in summaries]
    if report['issues']:
        lines += ['', '## Evidence gaps / failures', ''] + ['- ' + x for x in report['issues']]
    if report['kcov_feedback_gaps']:
        lines += ['', '## KCOV coverage feedback gaps', '',
                  'These segments kept every syscall-level trace and counter-trace check, but their',
                  'KCOV sample could not prove complete coverage. Saturated/invalid samples are rejected.',
                  'Only complete samples contributing new PCs are admitted to the corpus.', '']
        lines += ['- segment %s: %s' % (g['segment_id'], ', '.join(g['errors']))
                  for g in report['kcov_feedback_gaps']]
    if kernel_fault:
        lines += ['', '## Guest kernel diagnostic', '',
                  f'Kind: `{kernel_fault.get("kind", "kernel_fault")}`', '',
                  'The guest emitted a kernel BUG/Oops/panic. The serial console evidence is retained in `serial.log`.', '']
        lines += ['```text'] + kernel_fault.get('evidence', []) + ['```']
    lines += ['', '## Limits', ''] + ['- ' + x for x in report['limitations']]
    (output / 'campaign-report.md').write_text('\n'.join(lines) + '\n')
    return report
