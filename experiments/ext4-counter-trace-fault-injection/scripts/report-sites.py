#!/usr/bin/env python3
"""Join error_site trace events to reviewed sites; retain invocation ordering.

Natural integer/pointer returns are observations, not generic failure labels.
Pairing is by invocation, so recursion and interleaved workers are supported.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
EVENT = re.compile(r'\berror_site:\s+schema=1 site_id=(\d+) invocation=(\d+) '
                   r'phase=(enter|return) result=(-?\d+) kind=(pending|pointer|integer)\b')


def analyze(trace, manifest):
    known = {int(s['site_id']): s for s in manifest['sites']}
    hits, returns, active, seen, events, issues = Counter(), Counter(), {}, set(), [], []
    for line_number, line in enumerate(trace.splitlines(), 1):
        match = EVENT.search(line)
        if not match:
            if 'error_site:' in line:
                issues.append(f'malformed_event:{line_number}')
            continue
        site_id, invocation = int(match[1]), int(match[2])
        phase, result, kind = match[3], int(match[4]), match[5]
        if site_id not in known:
            issues.append(f'unknown_site:{site_id}:{line_number}')
            continue
        site = known[site_id]
        event = dict(trace_line=line_number, trace_prefix=line[:match.start()].strip(), site_id=site_id, invocation=invocation,
                     phase=phase, result=result, kind=kind,
                     **{key: site[key] for key in ('translation_unit', 'source', 'line', 'caller', 'callee')})
        events.append(event)
        if phase == 'enter':
            hits[site_id] += 1
            if invocation in seen:
                issues.append(f'duplicate_invocation:{invocation}:{line_number}')
            seen.add(invocation)
            active[invocation] = site_id
            if kind != 'pending':
                issues.append(f'invalid_enter_kind:{line_number}')
        else:
            returns[site_id] += 1
            if active.pop(invocation, None) != site_id:
                issues.append(f'unpaired_return:{invocation}:{line_number}')
            if kind == 'pending':
                issues.append(f'invalid_return_kind:{line_number}')
    issues += [f'unpaired_enter:{invocation}:{site_id}' for invocation, site_id in sorted(active.items())]
    summary = dict(schema=1, mode='observe_only', trace_complete_pairs=not issues,
                   site_count=len(known), reached_sites=len(hits), enter_events=sum(hits.values()),
                   return_events=sum(returns.values()), issues=issues,
                   sites=[dict(site_id=site_id, hits=hits[site_id], returns=returns[site_id],
                               **{key: known[site_id][key] for key in ('source', 'line', 'caller', 'callee', 'translation_unit')})
                          for site_id in sorted(hits.keys() | returns.keys())])
    return summary, events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=Path)
    parser.add_argument('--manifest', type=Path, default=PROJECT/'sites.json')
    parser.add_argument('--output', type=Path, required=True, help='report directory')
    args = parser.parse_args()
    summary, events = analyze(args.trace.read_text(), json.loads(args.manifest.read_text()))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    with (args.output/'events.csv').open('w', newline='') as stream:
        columns = ['trace_line', 'trace_prefix', 'invocation', 'phase', 'site_id', 'source', 'line',
                   'translation_unit', 'caller', 'callee', 'kind', 'result']
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(events)
    print(json.dumps(summary, indent=2))
    return 0 if summary['trace_complete_pairs'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
