#!/usr/bin/env python3
"""Rebuild a campaign report, including interrupted prefixes, without a VM."""
import argparse
import gzip
import json
import pathlib
import shutil
from campaign import aggregate, compress
from trace_lifecycle import analyze

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run')
    parser.add_argument('--output', help='separate directory for aggregate output')
    parser.add_argument('--reanalyze', action='store_true', help='recompute lifecycles in a separate output directory')
    args = parser.parse_args()
    if args.reanalyze:
        if not args.output:
            parser.error('--reanalyze requires --output; archived evidence stays immutable')
        root = pathlib.Path(args.output)
        for segment in sorted((pathlib.Path(args.run) / 'segments').glob('segment-*')):
            source = segment / 'analysis'
            if not (source / 'chain-manifest.json').exists():
                continue
            target = root / 'reanalyzed' / segment.name
            target.mkdir(parents=True, exist_ok=True)
            try:
                for path in source.iterdir():
                    if path.suffix == '.json' or path.name.endswith('-stats.txt'):
                        shutil.copy2(path, target / path.name)
                trace = source / 'trace.txt.gz'
                if not trace.exists():
                    trace = source / 'trace.txt'
                opener = gzip.open if trace.suffix == '.gz' else open
                with opener(trace, 'rb') as src, (target / 'trace.txt').open('wb') as dest:
                    shutil.copyfileobj(src, dest)
                analyze(target)
            except (ValueError, KeyError, OSError, EOFError) as error:
                (target / 'analysis-error.txt').write_text(str(error) + '\n')
            if (target / 'trace.txt').exists():
                compress(target / 'trace.txt')
            for path in [*list((target / 'lifecycle').glob('*.json')), target / 'lifecycle/timeline.jsonl']:
                if path.exists():
                    compress(path)
    report = aggregate(args.run, args.output,
                       pathlib.Path(args.output) / 'reanalyzed' if args.reanalyze else None)
    print(json.dumps({k: report[k] for k in ('passed', 'coverage_complete', 'segments', 'issues')}, indent=2))
    raise SystemExit(0 if report['passed'] else 1)
