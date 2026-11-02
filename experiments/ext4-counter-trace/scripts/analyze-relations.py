#!/usr/bin/env python3
"""Acceptance of the bounded normal-syscall object-relation experiment."""
import collections
import json
import pathlib
import sys
from trace_lifecycle import analyze, parse_trace

run = pathlib.Path(sys.argv[1])
directory = run / 'ct-relations'
intervals = []
for kind in ('regular', 'bigalloc', 'inline', 'legacy'):
    intervals.append(dict(name=kind, mode='single', start=kind+'-start.json',
        end=kind+'-end.json', snapshots=[kind+'-'+stage+'.json' for stage in
            ('before', 'shared', 'detached', 'open-unlinked', 'evicted', 'final')]))
(directory/'chain-manifest.json').write_text(json.dumps(dict(intervals=intervals,
    require_single_writer=True, require_disk=True), indent=2)+'\n')
report = analyze(directory)
events, _ = parse_trace(directory/'trace.txt')
issues, results = list(report['blockers']), {}
for interval in intervals:
    name = interval['name']
    epoch = json.loads((directory/interval['start']).read_text())['epoch']
    selected = [e for e in events if e['epoch'] == epoch]
    counts = collections.Counter((e['point'], e['formula'], e['status']) for e in selected)
    required = [('INODE.EVICT.TARGET.EXIT', 'R-IEVICT'),
                ('INODE.EVICT.DESCRIPTOR.EXIT', 'R-IDELTA-EVICT'),
                ('INODE.EVICT.GLOBAL.EXIT', 'R-IDELTA-EVICT'),
                ('XATTR.INODE.DETACH.EXIT', 'XATTR-REF-DELTA'),
                ('SNAPSHOT.MEMORY.STABLE', 'XATTR-REF')]
    if name != 'legacy':
        required += [('ORPHAN.FILE.DELETE.EXIT', 'O-LIFE'),
                     ('ORPHAN.FILE.FREE.EXIT', 'O-LIFE-DELTA'),
                     ('INODE.ALLOC.HIGHWATER', 'R-IH'),
                     ('SNAPSHOT.MEMORY.STABLE', 'O-LIFE-FREE'),
                     ('SNAPSHOT.MEMORY.STABLE', 'XATTR-REF.CSUM')]
    for point, formula in required:
        if not counts[point, formula, 1]:
            issues.append(f'{name}:missing_consistent:{formula}:{point}')
    shared = json.loads((directory/(name+'-shared.json')).read_text())['snapshot_id']
    if not any(e['op'] == shared and e['formula'] == 'XATTR-REF' and
               e['actual'] == e['expected'] == 2 and e['status'] == 1 for e in selected):
        issues.append(name+':no_independent_shared_refcount_two')
    if name != 'legacy':
        opened = json.loads((directory/(name+'-open-unlinked.json')).read_text())['snapshot_id']
        if not any(e['op'] == opened and e['formula'] == 'O-LIFE' and
                   e['ino'] and e['status'] == 1 for e in selected):
            issues.append(name+':no_live_orphan_slot_confirmation')
    else:
        if not counts['INODE.ALLOC.HIGHWATER', 'R-IH', 3]:
            issues.append('legacy:missing_highwater_out_of_scope')
    results[name] = {f'{p}:{f}:status{s}': n for (p, f, s), n in counts.items()
                     if f in {'R-IH', 'R-IEVICT', 'R-IDELTA-EVICT', 'O-LIFE',
                              'O-LIFE-DELTA', 'O-LIFE-FREE', 'XATTR-REF',
                              'XATTR-REF-DELTA', 'XATTR-REF.CSUM'}}
result = dict(passed=not issues, issues=issues, formulas=results,
              lifecycle_report='ct-relations/lifecycle/report.md',
              point_report='ct-relations/lifecycle/formula-relations.md')
(run/'relations-analysis.json').write_text(json.dumps(result, indent=2)+'\n')
print(json.dumps(result, indent=2))
sys.exit(not result['passed'])
