#!/usr/bin/env python3
"""Summarize per-segment and campaign stage timing/throughput evidence."""
from __future__ import annotations
import argparse, datetime, gzip, json, pathlib, re, statistics

STAGES = ('generation', 'compilation', 'upload', 'guest_execution', 'workload',
          'snapshot_one', 'fetch', 'feedback', 'trace_wait', 'analysis',
          'fault_reset', 'quota_reconciliation', 'finish_disk', 'disk_fsck',
          'disk_checker', 'aggregate')

ANALYSIS_EVENT = re.compile(
    r'^\[(?P<stamp>[^]]+)\] ANALYSIS (?P<event>START|END) segment=(?P<segment>\S+)')

def values(rows, stage):
    out = []
    for row in rows:
        item = row.get('stage_timings', {}).get(stage, {})
        if item.get('seconds') is not None:
            out.append(float(item['seconds']))
    return out

def percentile(items, p):
    if not items: return None
    return round(statistics.quantiles(items, n=100, method='inclusive')[p-1] if len(items) > 1 else items[0], 6)

def analysis_timings_from_progress(run):
    """Recover owning-segment analysis time from logs, including legacy runs.

    The runner finalizes the previous segment after opening the next segment's
    timing bucket. Progress events carry the segment identity, so they are the
    authoritative per-segment source when old manifests contain shifted data.
    """
    path = pathlib.Path(run) / 'progress.log'
    if not path.is_file():
        return {}
    starts, totals = {}, {}
    for line in path.read_text(errors='replace').splitlines():
        match = ANALYSIS_EVENT.match(line)
        if not match:
            continue
        try:
            stamp = datetime.datetime.fromisoformat(match['stamp'])
        except ValueError:
            continue
        segment = match['segment']
        if match['event'] == 'START':
            starts[segment] = stamp
        elif segment in starts:
            elapsed = max(0.0, (stamp - starts.pop(segment)).total_seconds())
            row = totals.setdefault(segment, dict(seconds=0.0, calls=0))
            row['seconds'] += elapsed
            row['calls'] += 1
    return {name: dict(seconds=round(row['seconds'], 6), calls=row['calls'])
            for name, row in totals.items()}

def build(run):
    run = pathlib.Path(run)
    meta = json.loads((run / 'campaign.json').read_text())
    summary_path = run / 'segment-summary.jsonl'
    summary_gz = run / 'segment-summary.jsonl.gz'
    if summary_path.is_file() or summary_gz.is_file():
        path = summary_path if summary_path.is_file() else summary_gz
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt') as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
    else:
        rows = []
        for manifest in sorted((run / 'segments').glob('segment-*/manifest.json')):
            try:
                item = json.loads(manifest.read_text())
            except (OSError, ValueError):
                continue
            item.setdefault('stage_timings', {})
            rows.append(item)
    progress_analysis = analysis_timings_from_progress(run)
    analysis_rows = [(row.get('stage_timings') or {}).get('analysis') for row in rows]
    campaign_analysis = (meta.get('stage_timings') or {}).get('analysis') or {}
    # Older timing instrumentation charged finalize(previous) to the next
    # segment's bucket. Detect that exact shape (first row missing, every
    # later row present, one campaign call per segment) and shift the recorded
    # durations back. Infer the last duration from the campaign-wide total.
    if (len(rows) > 1 and analysis_rows[0] is None and
            all(item is not None for item in analysis_rows[1:]) and
            int(campaign_analysis.get('calls', 0)) == len(rows)):
        total = float(campaign_analysis.get('seconds', 0.0))
        shifted = [float(item['seconds']) for item in analysis_rows[1:]]
        final_seconds = total - sum(shifted)
        if final_seconds >= 0:
            for index in range(len(rows) - 1):
                rows[index].setdefault('stage_timings', {})['analysis'] = analysis_rows[index + 1]
            rows[-1].setdefault('stage_timings', {})['analysis'] = {
                'seconds': round(final_seconds, 6), 'calls': 1,
            }
    else:
        # If there is no per-segment timer (e.g. a legacy/partial campaign),
        # use the explicit segment-labelled log interval as a lower-bound
        # fallback instead of silently presenting a different segment's time.
        for row in rows:
            segment_id = row.get('segment_id')
            if isinstance(segment_id, int):
                segment_name = f'segment-{segment_id:06d}'
            else:
                segment_name = row.get('segment') or row.get('name')
            if (segment_name in progress_analysis and
                    (not campaign_analysis or
                     not (row.get('stage_timings') or {}).get('analysis'))):
                row.setdefault('stage_timings', {})['analysis'] = progress_analysis[segment_name]
    stage_names = list(STAGES)
    stage_names.extend(sorted({stage for row in rows
                               for stage in (row.get('stage_timings') or {})
                               if stage not in stage_names}))
    timing = {}
    for stage in stage_names:
        vals = values(rows, stage)
        timing[stage] = dict(count=len(vals), total=round(sum(vals), 6), p50=percentile(vals, 50),
                             p95=percentile(vals, 95), first10=round(sum(vals[:10]), 6),
                             last10=round(sum(vals[-10:]), 6))
    campaign_timing = {}
    for stage, item in (meta.get('stage_timings') or {}).items():
        if isinstance(item, dict):
            campaign_timing[stage] = dict(seconds=float(item.get('seconds', 0.0)),
                                          calls=int(item.get('calls', 0)))
    def throughput(subset):
        elapsed = sum(float(r.get('segment_elapsed_seconds') or 0) for r in subset)
        calls = sum(int(r.get('native_verified', 0)) for r in subset)
        transitions = sum(int(r.get('exploration', {}).get('independent_transition_calls', 0)) for r in subset)
        return dict(segments=len(subset), seconds=round(elapsed, 6), verified_syscalls=calls,
                    syscalls_per_second=round(calls / elapsed, 4) if elapsed else 0,
                    formula_transitions=transitions,
                    transitions_per_second=round(transitions / elapsed, 4) if elapsed else 0)
    result = dict(schema=1, run_id=meta.get('run_id'), elapsed_seconds=meta.get('elapsed_seconds'),
                  collection_elapsed_seconds=meta.get('collection_elapsed_seconds'), stages=timing,
                  campaign_stages=campaign_timing,
                  nested_stage_groups={'guest_execution_contains': ['workload', 'snapshot_one']},
                  first10=throughput(rows[:10]), last10=throughput(rows[-10:]),
                  all=throughput(rows), segment_count=len(rows),
                  limits=meta.get('limits', {}))
    return result

def render(result):
    lines = [f'# Performance report: {result["run_id"]}', '',
             f'Segments: {result["segment_count"]}; collection seconds: {result.get("collection_elapsed_seconds")}.', '',
             '| Stage | Count | Total s | p50 s | p95 s | First 10 s | Last 10 s |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for stage, row in result['stages'].items():
        lines.append(f'| {stage} | {row["count"]} | {row["total"]} | {row["p50"]} | {row["p95"]} | {row["first10"]} | {row["last10"]} |')
    lines += ['', 'Per-segment nested phases (for example `workload` and `snapshot_one`) are included in `guest_execution`; do not sum nested rows as independent wall time.']
    if result.get('campaign_stages'):
        lines += ['', '## Campaign-level stages', '',
                  '| Stage | Calls | Total s |', '| --- | ---: | ---: |']
        for stage, row in result['campaign_stages'].items():
            lines.append(f'| {stage} | {row["calls"]} | {row["seconds"]} |')
    lines += ['', '| Window | Segments | Verified syscall/s | Formula transition/s |', '| --- | ---: | ---: | ---: |']
    for name in ('first10', 'last10', 'all'):
        row = result[name]
        lines.append(f'| {name} | {row["segments"]} | {row["syscalls_per_second"]} | {row["transitions_per_second"]} |')
    return '\n'.join(lines) + '\n'

def main():
    p = argparse.ArgumentParser(); p.add_argument('run'); p.add_argument('--output')
    a = p.parse_args(); run = pathlib.Path(a.run); result = build(run)
    out = pathlib.Path(a.output) if a.output else run
    (out / 'performance-report.json').write_text(json.dumps(result, indent=2) + '\n')
    (out / 'performance-report.md').write_text(render(result))
    print(json.dumps(result, indent=2))

if __name__ == '__main__': main()
