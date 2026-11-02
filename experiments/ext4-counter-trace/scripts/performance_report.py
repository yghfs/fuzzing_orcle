#!/usr/bin/env python3
"""Summarize per-segment and campaign stage timing/throughput evidence."""
from __future__ import annotations
import argparse, gzip, json, pathlib, statistics

STAGES = ('generation', 'compilation', 'upload', 'guest_execution', 'fetch',
          'feedback', 'analysis', 'finish_disk', 'aggregate', 'quota_reconciliation')

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
    timing = {}
    for stage in STAGES:
        vals = values(rows, stage)
        timing[stage] = dict(count=len(vals), total=round(sum(vals), 6), p50=percentile(vals, 50),
                             p95=percentile(vals, 95), first10=round(sum(vals[:10]), 6),
                             last10=round(sum(vals[-10:]), 6))
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
