#!/usr/bin/env python3
"""Explicit boot/run/sequence identity for the finite lifecycle experiment."""
import json
import pathlib
import sys
from trace_lifecycle import parse_trace

directory = pathlib.Path(sys.argv[1])
events, _ = parse_trace(directory / 'trace.txt')
intervals = []
for kind in ('regular', 'bigalloc', 'inline'):
    for mode in ('single', 'concurrent'):
        name = f'{kind}-{mode}'
        start = json.loads((directory / (name + '-start.json')).read_text())
        devs = {e['dev'] for e in events if e['epoch'] == start['epoch']}
        if len(devs) != 1:
            raise ValueError(f'{name}: device identity ambiguous')
        intervals.append({'name': name, 'mode': mode, 'dev': next(iter(devs)),
                          'start': name + '-start.json', 'end': name + '-end.json',
                          'snapshots': [p.name for p in sorted(directory.glob(name + '-*.json'))
                                        if 'snapshot_id' in json.loads(p.read_text())]})
manifest = {'schema': 2, 'run_id': directory.parent.name,
            'boot_id': (directory / 'boot-id.txt').read_text().strip(),
            'require_disk': True, 'require_native_syscalls': True, 'require_single_writer': True, 'intervals': intervals}
(directory / 'chain-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
