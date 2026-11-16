#!/usr/bin/env python3
"""Reanalyze retained reset failures in a new directory; never edit raw runs."""
import argparse
import gzip
import pathlib
import shutil
import sys
import tempfile

PROJECT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'scripts'))
from campaign import atomic_json, read_json
from campaign_runner import lifecycle_intervals
from trace_lifecycle import analyze, parse_trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=pathlib.Path, default=PROJECT / 'results')
    args = parser.parse_args()
    output = pathlib.Path(tempfile.mkdtemp(prefix='ext4-reset-reanalysis-'))
    results = []
    retained = [('campaign-9chz7m3j', [97, 693, 954, 1089, 1278, 1298, 1465]),
                ('campaign-fyubc_lv', [326, 1089, 1298])]
    for name, indices in retained:
        root = args.results / name
        devices = read_json(root / 'campaign.json')['devices']
        for index in indices:
            source = root / 'segments' / f'segment-{index:06d}'
            previous = root / 'segments' / f'segment-{index-1:06d}'
            target = output / name / source.name
            target.mkdir(parents=True)
            for path in source.glob('*.json'):
                shutil.copy2(path, target / path.name)
            analysis = target / 'analysis'
            analysis.mkdir()
            with gzip.open(source / 'analysis/trace.txt.gz', 'rb') as src, \
                    (analysis / 'trace.txt').open('wb') as dst:
                shutil.copyfileobj(src, dst)
            for path in (source / 'analysis').glob('cpu*-stats.txt'):
                shutil.copy2(path, analysis / path.name)
            events, _ = parse_trace(analysis / 'trace.txt')
            manifest = read_json(target / 'manifest.json')
            intervals, _, boundaries = lifecycle_intervals(target, manifest, events, devices, previous)
            atomic_json(analysis / 'chain-manifest.json', dict(intervals=intervals,
                require_native_syscalls=True, reset_boundaries=boundaries))
            report = analyze(analysis)
            results.append(dict(campaign=name, segment=index, passed=report['passed'],
                blockers=report['blockers'], reset_boundaries=boundaries,
                trajectory_integrity={r['name']: r['trajectory_integrity'] for r in report['runs']}))
            print(f'{name}/{source.name}: passed={report["passed"]}', flush=True)
    atomic_json(output / 'summary.json', dict(results=results))
    print(f'output={output}')
    return int(not all(result['passed'] for result in results))


if __name__ == '__main__':
    sys.exit(main())
