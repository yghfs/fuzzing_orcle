#!/usr/bin/env python3
"""Validate real VM probe hits, natural EFAULT, disabled tracing and snapshots."""
import csv
import importlib.util
import json
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('site_report', PROJECT/'scripts/report-sites.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def stats(path):
    with path.open(newline='') as stream:
        return {int(row['site_id']): (int(row['hits']), int(row['returns']))
                for row in csv.DictReader(stream, delimiter='\t')}


def verify(run):
    manifest = json.loads((run/'sites.json').read_text())
    known = {site['site_id'] for site in manifest['sites']}
    output = run/'error-site-results'
    failures, evidence = [], {}
    initial = stats(output/'stats-disabled.tsv')
    if set(initial) != known or any(pair != (0, 0) for pair in initial.values()):
        failures.append('initial_stats_not_disabled_or_wrong_site_table')
    for kind in ('regular', 'inline'):
        evidence[kind] = {}
        for phase in ('mount', 'workload'):
            summary, events = report.analyze((output/f'{kind}-{phase}.trace').read_text(), manifest)
            evidence[kind][phase] = {key: summary[key] for key in
                                    ('reached_sites', 'enter_events', 'return_events', 'trace_complete_pairs')}
            if not summary['trace_complete_pairs'] or not summary['enter_events']:
                failures.append(f'{kind}_{phase}_missing_or_unpaired_events:{summary["issues"]}')
            if phase == 'workload':
                reached = {event['site_id'] for event in events if event['phase'] == 'enter'}
                expected = {371, 379, 374, 380, 285}
                if not expected <= reached:
                    failures.append(f'{kind}_missing_expected_workload_sites:{sorted(expected-reached)}')
                if not any(event['site_id'] == 285 and event['phase'] == 'return' and
                           event['kind'] == 'integer' and event['result'] > 0 for event in events):
                    failures.append(f'{kind}_missing_natural_copy_from_user_failure')
                if kind == 'inline' and not {175, 176} <= reached:
                    failures.append('inline_shared_header_allocation_probe_not_exercised')
        current = stats(output/f'{kind}-stats.tsv')
        if set(current) != known or not any(hit > 0 for hit, _ in current.values()):
            failures.append(f'{kind}_no_runtime_counters')
        for name in ('before', 'after'):
            snapshot = json.loads((output/f'{kind}-{name}.json').read_text())
            if not snapshot['valid'] or not snapshot['namespace_valid'] or not snapshot['relations_valid']:
                failures.append(f'{kind}_{name}_unstable_snapshot')
            if any(snapshot[key] for key in ('observed_mismatches', 'namespace_mismatched', 'relations_mismatches')):
                failures.append(f'{kind}_{name}_snapshot_mismatch')
        for path in output.glob(f'{kind}-*-cpu*-stats.txt'):
            for line in path.read_text().splitlines():
                if line.startswith(('overrun:', 'commit overrun:', 'dropped events:')) and int(line.split(':', 1)[1]):
                    failures.append(f'trace_loss:{path.name}:{line}')
    if stats(output/'stats-before-disabled-workload.tsv') != stats(output/'stats-after-disabled-workload.tsv'):
        failures.append('disabled_probes_changed_counters')
    disabled, _ = report.analyze((output/'disabled-workload.trace').read_text(), manifest)
    if disabled['enter_events'] or disabled['return_events']:
        failures.append('disabled_probes_emitted_events')
    if re.search(r'BUG:|KASAN:|Oops:|possible circular locking|WARNING:', (output/'dmesg.txt').read_text()):
        failures.append('kernel_diagnostic')
    result = dict(passed=not failures, failures=failures, realistic_records=len(known),
                  evidence=evidence, disabled_probes_unchanged='disabled_probes_changed_counters' not in failures,
                  natural_copy_from_user_failure=not any('natural_copy_from_user_failure' in failure for failure in failures))
    (run/'verification.json').write_text(json.dumps(result, indent=2)+'\n')
    return result


if __name__ == '__main__':
    result = verify(Path(sys.argv[1]))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
