#!/usr/bin/env python3
"""Pointwise ext4 counter trajectories. No derived value is a live sample.

Recording order is not mutation order across threads. Only complete windows,
valid stable anchors, and scopes without foreign writers support a precise
trajectory. Unknown causality remains in the output.
"""
from __future__ import annotations

import collections
import json
import os
import pathlib
import re
from formula_relations import summarize as relation_summary, markdown as relation_markdown, TRANSITION_POINTS

PREFIX = re.compile(r'(?P<comm>.+)-(?P<pid>\d+)\s+\[(?P<cpu>\d+)\]\s+\S+\s+(?P<time>\d+\.\d+):\s*(?P<body>.*)')
NUMERIC = {'epoch', 'seq', 'op', 'parent', 'ino', 'group', 'tid', 'actual',
           'expected', 'delta', 'update_delta', 'status', 'comparison_valid',
           'nlink_before', 'nlink_after', 'dir_nlink_before', 'dir_nlink_after',
           'step', 'schema', 'dir_ino', 'ref_delta', 'result', 'generation', 'is_dir', 'call_index',
           'block', 'slot', 'adapted', 'original_length', 'executed_length',
           'object_ino', 'object_dev', 'object_is_dir'}
COUNTERS = {
    'R-BDELTA': {'field': 'free_clusters', 'group': 'R-BG', 'sum': 'R-BSUM',
                 'independent': {'BLOCK.BITMAP.TRANSITION'},
                 'descriptor': {'BLOCK.DESCRIPTOR.LOCAL', 'GROUP.LAZY.BLOCK.DESCRIPTOR'},
                 'global': {'BLOCK.ALLOC.GLOBAL', 'BLOCK.FREE.GLOBAL', 'BLOCK.BIGALLOC.RERESERVE'}},
    'R-IDELTA': {'field': 'free_inodes', 'group': 'R-IG', 'sum': 'R-ISUM',
                 'independent': {'INODE.ALLOC.BITMAP', 'INODE.FREE.BITMAP'},
                 'descriptor': {'INODE.ALLOC.DESCRIPTOR', 'INODE.FREE.DESCRIPTOR'},
                 'global': {'INODE.ALLOC.GLOBAL', 'INODE.FREE.GLOBAL'}},
    'R-UDELTA': {'field': 'dirs', 'group': 'R-UG', 'sum': 'R-USUM',
                 'independent': {'INODE.ALLOC.LIFETIME', 'INODE.FREE.LIFETIME'},
                 'descriptor': {'INODE.ALLOC.DESCRIPTOR', 'INODE.FREE.DESCRIPTOR'},
                 'global': {'INODE.ALLOC.GLOBAL', 'INODE.FREE.GLOBAL'}},
}
SPECIAL_PREFIXES = ('FAST_COMMIT.', 'RESIZE.', 'BITMAP.CORRUPT.', 'BLOCK.GROUP_ADD.')
CLASSES = ('transient_mismatch', 'operation_exit_mismatch', 'persistent_mismatch',
           'concurrent_or_ambiguous', 'unsupported_or_unverified')


def fields(text):
    out = dict(re.findall(r'(\w+)=([^\s]+)', text))
    for key in out.keys() & NUMERIC:
        out[key] = int(out[key])
    return out


def canonical_device(value):
    """Normalize trace `major,minor` and stat(2) st_dev to one identity."""
    if value is None:
        return None
    try:
        if isinstance(value, str) and ',' in value:
            major, minor = value.split(',', 1)
            major, minor = int(major), int(minor)
        else:
            device = int(value)
            major, minor = os.major(device), os.minor(device)
    except (TypeError, ValueError, OverflowError):
        return None
    if major < 0 or minor < 0:
        return None
    return f'{major}:{minor}'


def object_key(kind, ino, device):
    """An inode number is only unique within a filesystem device."""
    if not ino:
        return None
    device_id = canonical_device(device)
    if device_id is None:
        return None
    return f'{kind}:{device_id}:{ino}'


def trace_integer(value):
    """Decode an unprefixed hexadecimal syscall argument from ftrace."""
    value = str(value).strip().lower()
    try:
        if value.startswith('-'):
            return int(value, 10)
        parsed = int(value, 16)
        return parsed - (1 << 64) if parsed >= (1 << 63) else parsed
    except ValueError:
        return None


def parse_trace(path):
    events, lines = [], []
    with pathlib.Path(path).open(errors='replace') as stream:
        for number, raw in enumerate(stream, 1):
            m = PREFIX.search(raw.strip())
            if not m:
                if 'ext4_counter_event:' in raw:
                    raise ValueError(f'line {number}: undecodable counter prefix')
                continue
            seconds, fraction = m['time'].split('.')
            prefix = {'line': number, 'pid': int(m['pid']), 'cpu': int(m['cpu']),
                      'comm': m['comm'].strip(),
                      'timestamp_ns': int(seconds) * 10**9 + int(fraction.ljust(9, '0'))}
            body = m['body']
            lines.append(dict(prefix, body=body))
            if body.startswith('ext4_counter_event:'):
                e = dict(prefix, **fields(body))
                required = {'dev', 'epoch', 'seq', 'op', 'parent', 'point', 'formula',
                            'actual', 'expected', 'update_delta', 'status'}
                if not required <= e.keys():
                    raise ValueError(f'line {number}: missing counter fields')
                e.setdefault('schema', 1)
                events.append(e)
    return events, lines


def correlate_syscalls(lines):
    """Use native entry/exit, not the userspace marker, for kernel boundaries."""
    pending, pairs, envelopes, opened, problems = {}, [], [], {}, []
    for line in lines:
        body, pid = line['body'], line['pid']
        entry = re.search(r'(?:raw_syscalls:)?sys_enter:\s+NR\s+(-?\d+)\s+\(([^)]*)\)', body)
        exit_ = re.search(r'(?:raw_syscalls:)?sys_exit:\s+NR\s+(-?\d+)\s+=\s+(-?\d+)', body)
        if entry:
            if pid in pending:
                problems.append({'pid': pid, 'line': line['line'], 'reason': 'native_entry_without_exit'})
            arguments = entry[2].split(',')
            pending[pid] = dict(line, nr=int(entry[1]),
                                first_arg=trace_integer(arguments[0]) if arguments else None)
        elif exit_:
            start = pending.pop(pid, None)
            if start and start['nr'] == int(exit_[1]):
                pairs.append({'pid': pid, 'nr': start['nr'], 'enter_line': start['line'],
                              'exit_line': line['line'], 'enter_ns': start['timestamp_ns'],
                              'exit_ns': line['timestamp_ns'], 'raw_result': int(exit_[2]),
                              'first_arg': start.get('first_arg')})
            # Collection can start/end halfway through unrelated syscalls.
        if 'tracing_mark_write: CT_SYSCALL ' in body:
            marker = dict(line, **fields(body.split('CT_SYSCALL ', 1)[1]))
            ident = (pid, marker.get('id'))
            if marker.get('phase') == 'BEGIN':
                if ident in opened:
                    problems.append({'pid': pid, 'line': line['line'], 'reason': 'duplicate_syscall_marker'})
                opened[ident] = marker
            elif marker.get('phase') == 'END':
                start = opened.pop(ident, None)
                if start is None:
                    problems.append({'pid': pid, 'line': line['line'], 'reason': 'unmatched_syscall_marker'})
                else:
                    if any(start.get(key) != marker.get(key) for key in
                           ('nr', 'name', 'program', 'call_index', 'object_ino', 'object_dev',
                            'object_is_dir')):
                        problems.append({'pid': pid, 'line': line['line'], 'reason': 'syscall_marker_identity_changed'})
                    envelopes.append((start, marker))
    for start in opened.values():
        problems.append({'pid': start['pid'], 'line': start['line'], 'reason': 'unclosed_syscall_marker'})
    calls = []
    for begin, end in envelopes:
        nr = int(begin['nr'])
        try:
            # CT_SYSCALL markers render this value as decimal, while ftrace
            # renders syscall arguments as hexadecimal words.
            expected_first_arg = int(begin.get('executed_fd'))
        except (TypeError, ValueError):
            expected_first_arg = None
        selected = [p for p in pairs if p['pid'] == begin['pid'] and p['nr'] == nr
                    and begin['line'] < p['enter_line'] < p['exit_line'] < end['line']
                    and (expected_first_arg is None or p.get('first_arg') == expected_first_arg)]
        call = {'id': begin['id'], 'pid': begin['pid'], 'name': begin.get('name'), 'nr': nr,
                'program': begin.get('program'), 'call_index': begin.get('call_index'),
                'object_ino': begin.get('object_ino'), 'object_dev': begin.get('object_dev'),
                'object_is_dir': begin.get('object_is_dir'),
                'repaired': int(begin.get('repaired', 0)),
                'original_fd': begin.get('original_fd'), 'executed_fd': begin.get('executed_fd'),
                'repair_version': begin.get('repair_version'),
                'marker_begin_line': begin['line'], 'marker_end_line': end['line'],
                'user_result': int(end['ret']), 'user_errno': int(end['errno']),
                'verified': False, 'reason': 'native_pair_missing_or_nonunique'}
        if len(selected) == 1:
            call.update(selected[0])
            raw = call['raw_result']
            errno = -raw if -4095 <= raw < 0 else 0
            expected_user = -1 if errno else raw
            call.update(errno=errno, verified=(call['user_errno'] == errno and
                        call['user_result'] == expected_user), reason='native_return')
            if not call['verified']:
                call['reason'] = 'native_userspace_result_disagrees'
        calls.append(call)
    return calls, problems


PHASE_SOURCES = {'seed_setup', 'runtime_init', 'runtime_adaptation', 'cleanup',
                 'stable_snapshot', 'fault_reset'}


def attribute_sources(events, lines, calls):
    """Same-PID native boundaries and explicit windows; never temporal proximity."""
    opened, windows, problems = {}, collections.defaultdict(list), []
    markers_present = False
    for line in lines:
        if 'tracing_mark_write: CT_PHASE ' not in line['body']:
            continue
        markers_present = True
        marker = fields(line['body'].split('CT_PHASE ', 1)[1])
        try:
            pid = int(marker['target_pid'])
            ident = (pid, marker['id'])
            source = marker['source']
            if source not in PHASE_SOURCES:
                raise ValueError('unknown_source')
            if marker['phase'] == 'BEGIN':
                if ident in opened:
                    problems.append('duplicate_phase_begin:' + str(ident))
                opened[ident] = (line['line'], source)
            elif marker['phase'] == 'END':
                begin = opened.pop(ident, None)
                if not begin or begin[1] != source:
                    problems.append('unmatched_phase_end:' + str(ident))
                else:
                    windows[pid].append((begin[0], line['line'], source))
            else:
                raise ValueError('invalid_phase_edge')
        except (KeyError, ValueError) as error:
            problems.append(f'phase_line_{line["line"]}:{error}')
    problems.extend('unclosed_phase:' + str(key) for key in opened)
    native = collections.defaultdict(list)
    for call in calls:
        if call.get('verified'):
            native[call['pid']].append(call)
    counts = collections.Counter()
    generated_window_events = 0
    generated_unattributed_events = 0
    for event in events:
        selected = [c for c in native[event['pid']]
                    if c['enter_line'] < event['line'] < c['exit_line']]
        if selected:
            generated_window_events += 1
            if len(selected) != 1:
                generated_unattributed_events += 1
        phases = [w[2] for w in windows[event['pid']] if w[0] < event['line'] < w[1]]
        if len(selected) == 1:
            source = 'generated_syscall'
        elif len(phases) == 1:
            source = phases[0]
        else:
            source = 'background_unattributed' if markers_present else 'outside_generated_call'
        event['event_source'] = source
        event['source_format'] = 'explicit_phases' if markers_present else 'legacy_inside_outside'
        counts[source] += 1
    return dict(counts=dict(counts), total_events=len(events), issues=problems,
                format='explicit_phases' if markers_present else 'legacy_inside_outside',
                source_attribution_complete=bool(events) and markers_present and not problems and
                    not counts['background_unattributed'],
                generated_syscall_event_count=generated_window_events,
                generated_syscall_unattributed_events=generated_unattributed_events,
                generated_syscall_attribution_complete=bool(markers_present and not problems and
                    generated_unattributed_events == 0))


def exploration_metrics(events, calls):
    independent = set().union(*(c['independent'] for c in COUNTERS.values()))
    by_call = collections.defaultdict(list)
    native = collections.defaultdict(list)
    for c in calls:
        if c.get('verified'):
            native[c['pid']].append(c)
    for e in events:
        selected = [c for c in native[e['pid']] if c['enter_line'] < e['line'] < c['exit_line']]
        if len(selected) == 1:
            by_call[selected[0]['id']].append(e)
    namespace = {'mkdirat', 'rmdir', 'unlinkat', 'renameat2', 'linkat', 'symlinkat'}
    syscall_stats = {}
    transitions = 0
    for call in calls:
        row = syscall_stats.setdefault(call.get('name'), dict(executions=0, successful=0, failed=0,
            repaired=0, counter_event_calls=0, independent_transition_calls=0,
            successful_transition_calls=0, failed_transition_calls=0, errors={}, point_hits={}))
        row['executions'] += 1
        success = isinstance(call.get('raw_result'), int) and call['raw_result'] >= 0
        row['successful' if success else 'failed'] += 1
        row['repaired'] += bool(call.get('repaired'))
        if not success:
            key = str(call.get('errno', 'unverified'))
            row['errors'][key] = row['errors'].get(key, 0) + 1
        hits = by_call[call['id']]
        row['counter_event_calls'] += bool(hits)
        changed = any((e['point'] in independent and e.get('update_delta')) or
                      (e['formula'] == 'R-LINK' and
                       (e.get('ref_delta') or e.get('step') in (32, 64))) or
                      (e['point'] in TRANSITION_POINTS and e.get('update_delta')) for e in hits)
        row['independent_transition_calls'] += changed
        row['successful_transition_calls' if success else 'failed_transition_calls'] += changed
        transitions += changed
        for e in hits:
            row['point_hits'][e['point']] = row['point_hits'].get(e['point'], 0) + 1
    for row in syscall_stats.values():
        executions = row['executions']
        verified = row['successful'] + row['failed']
        row['verified'] = verified
        row['success_rate'] = row['successful'] / executions if executions else 0.0
        row['event_rate'] = row['counter_event_calls'] / executions if executions else 0.0
        row['conversion_density'] = row['independent_transition_calls'] / executions if executions else 0.0
        row['successful_conversion_density'] = (row['successful_transition_calls'] / row['successful']
                                                 if row['successful'] else 0.0)
        row['verified_rate'] = verified / executions if executions else 0.0
    return dict(syscalls=syscall_stats, independent_transition_calls=transitions,
                successful_namespace_operations=sum(row['successful'] for name, row in syscall_stats.items()
                                                     if name in namespace),
                failed_namespace_operations=sum(row['failed'] for name, row in syscall_stats.items()
                                                if name in namespace),
                repaired_calls=sum(row['repaired'] for row in syscall_stats.values()),
                namespace_calls={name: row for name, row in syscall_stats.items() if name in namespace})


def loss_evidence(directory):
    paths = sorted(pathlib.Path(directory).glob('cpu*-stats.txt'))
    issues, stats = [], {}
    if not paths:
        issues.append('missing_per_cpu_loss_stats')
    for path in paths:
        data = {k: int(v) for k, v in re.findall(r'^([\w ]+):\s*(\d+)$', path.read_text(), re.M)}
        stats[path.name] = data
        for key in ('overrun', 'commit overrun', 'dropped events'):
            if key not in data:
                issues.append(f'{path.name}: missing {key}')
            elif data[key]:
                issues.append(f'{path.name}: {key}={data[key]}')
    return stats, issues


def scope_tree(events):
    scopes, issues = {}, []
    for e in events:
        if e['formula'] == 'BEGIN':
            if e['op'] in scopes:
                issues.append(f'duplicate_begin:{e["op"]}')
            scopes[e['op']] = {'id': e['op'], 'parent': e['parent'], 'point': e['point'],
                              'pid': e['pid'], 'begin_seq': e['seq'], 'begin_ns': e['timestamp_ns'],
                              'begin_line': e['line'], 'end_seq': None}
        elif e['formula'] == 'END':
            scope = scopes.get(e['op'])
            if scope is None or scope['end_seq'] is not None or scope['point'] != e['point']:
                issues.append(f'unmatched_end:{e["op"]}')
            else:
                scope.update(end_seq=e['seq'], end_ns=e['timestamp_ns'], end_line=e['line'],
                             result=e.get('result', e['actual']))
                if scope['pid'] != e['pid'] or scope['parent'] != e['parent']:
                    issues.append(f'scope_identity_changed:{e["op"]}')
    for scope in scopes.values():
        if scope['end_seq'] is None:
            issues.append(f'unclosed_scope:{scope["id"]}')
        parent = scope['parent']
        if parent and (parent not in scopes or not scopes[parent]['begin_seq'] < scope['begin_seq'] or
                       (scopes[parent]['end_seq'] is not None and scope['end_seq'] is not None
                        and scopes[parent]['end_seq'] <= scope['end_seq'])):
            issues.append(f'invalid_parent:{scope["id"]}')
    for e in events:
        if e['formula'] in ('BEGIN', 'END') or not e['op']:
            continue
        scope = scopes.get(e['op'])
        if scope is None or not scope['begin_seq'] < e['seq'] < (scope['end_seq'] or 2**64):
            issues.append(f'event_outside_scope:{e["seq"]}')
        elif scope['pid'] != e['pid'] or scope['parent'] != e['parent']:
            issues.append(f'event_scope_identity_changed:{e["seq"]}')
    return scopes, issues


def root_for(op, scopes):
    visited = set()
    while op in scopes and scopes[op]['parent']:
        if op in visited:
            return None
        visited.add(op)
        op = scopes[op]['parent']
    return op if op in scopes else None


def unique_operation_ids(rows):
    """Flatten and de-duplicate operation ids while preserving event order.

    A syscall can contain more than one ext4 root scope, represented as a
    list in its operation context. These IDs are metadata, not hash keys.
    """
    result = []

    def append(value):
        if isinstance(value, (list, tuple)):
            for item in value:
                append(item)
        elif value is not None and value not in result:
            result.append(value)

    for row in rows:
        append(row['operation'].get('op'))
    return result


def native_for(e, calls):
    found = [c for c in calls if c['verified'] and c['pid'] == e['pid']
             and c['enter_line'] < e['line'] < c['exit_line']]
    return found[0]['id'] if len(found) == 1 else None


def value_kind(e):
    if e['formula'] == 'END':
        return 'ext4_helper_return'
    if e['formula'] == 'BEGIN':
        return 'ext4_helper_begin'
    if e.get('comparison_valid') or e['status'] in (1, 2):
        return 'sampled_comparison'
    if e['point'].startswith('WRITE.'):
        return 'authoritative_write_audit'
    if e['formula'] == 'R-LINK.EXCLUDED':
        return 'excluded_internal_inode_transition'
    if e['formula'] == 'R-LINK':
        if e.get('ref_delta'):
            return 'applied_reference_conversion'
        if e.get('step') in (4, 8):
            return 'sampled_nlink_transition'
        if e.get('step') == 32:
            return 'inode_birth'
        if e.get('step') == 64:
            return 'inode_death'
        return 'namespace_helper_result'
    if 'GLOBAL' in e['point'] or e['point'] == 'BLOCK.BIGALLOC.RERESERVE':
        return 'publication_delta_not_global_sample'
    if 'BITMAP' in e['point'] or 'LIFETIME' in e['point']:
        return 'independent_conversion'
    if 'DESCRIPTOR' in e['point']:
        return 'descriptor_sample_or_dirty_result'
    return 'raw_or_unverified'


def write_coverage(events, scopes):
    """Schema 3: reconcile authoritative writes with semantic publications.

    No sequence check can detect a probe that never emitted an event. This
    second stream guards missing semantic events, including net-zero pairs.
    It cannot replace the separately reviewed, hash-bound source inventory.
    """
    if not any(e.get('schema', 1) >= 3 for e in events):
        return {'available': False, 'reason': 'legacy_schema_without_write_audit', 'issues': []}
    audit, semantic = collections.defaultdict(list), collections.defaultdict(list)
    formulas = {'WRITE-BG': ('R-BDELTA', 'descriptor'),
                'WRITE-IG': ('R-IDELTA', 'descriptor'),
                'WRITE-UG': ('R-UDELTA', 'descriptor'),
                'WRITE-B': ('R-BDELTA', 'global'),
                'WRITE-I': ('R-IDELTA', 'global'),
                'WRITE-U': ('R-UDELTA', 'global')}
    excluded = 0
    for e in events:
        # A resize/corruption/replay helper can be nested below a normal
        # operation (for example bitmap corruption during inode allocation).
        # Walk the complete ancestry so its writes stay in the special domain.
        ancestor = e.get('op')
        special_ancestor = False
        visited = set()
        while ancestor in scopes and ancestor not in visited:
            visited.add(ancestor)
            scope = scopes[ancestor]
            if scope.get('point', '').startswith(SPECIAL_PREFIXES):
                special_ancestor = True
                break
            ancestor = scope.get('parent', 0)
        root = scopes.get(root_for(e['op'], scopes), {})
        if (special_ancestor or root.get('point', '').startswith(SPECIAL_PREFIXES) or
                e['point'].startswith(SPECIAL_PREFIXES) or e.get('reason', '').startswith('replay_')):
            excluded += 1
            continue
        f = e['formula']
        if e['point'] in ('WRITE.DESCRIPTOR', 'WRITE.GLOBAL') and f in formulas:
            formula, lane = formulas[f]
            key = (e['op'], formula, lane, e['group'] if lane == 'descriptor' else None)
            audit[key].append(e)
        elif f in COUNTERS:
            spec = COUNTERS[f]
            lane = next((k for k in ('descriptor', 'global') if e['point'] in spec[k]), None)
            if lane:
                key = (e['op'], f, lane, e['group'] if lane == 'descriptor' else None)
                semantic[key].append(e)
    issues, checks = [], []
    for key in sorted(set(audit) | set(semantic), key=str):
        left, right = audit[key], semantic[key]
        # Match every publication individually in same-task scope order,
        # so opposite missing writes cannot cancel in an interval sum.
        a = [e['update_delta'] for e in left]
        b = [e['update_delta'] for e in right]
        ok = a == b
        checks.append({'op': key[0], 'formula': key[1], 'lane': key[2], 'group': key[3],
                       'write_seqs': [e['seq'] for e in left],
                       'semantic_seqs': [e['seq'] for e in right],
                       'write_deltas': a, 'semantic_deltas': b, 'matched': ok})
        if not ok:
            issues.append(f'authoritative_write_semantic_disagrees:{key}')
        for e in left:
            if e['point'] == 'WRITE.DESCRIPTOR' and e['actual'] - e['expected'] != e['update_delta']:
                issues.append(f'descriptor_write_payload_disagrees:{e["seq"]}')
    return {'available': True, 'issues': issues, 'checks': checks,
            'special_events_delegated': excluded,
            'source_completeness': 'requires_reviewed_source_inventory'}


class Lifecycle:
    def __init__(self, name, events, snapshots, integrity, calls):
        self.name, self.events, self.snapshots = name, events, snapshots
        self.scopes, scope_issues = scope_tree(events)
        self.write_coverage = write_coverage(events, self.scopes)
        self.integrity = list(integrity) + scope_issues + self.write_coverage['issues']
        self.valid = not self.integrity
        self.calls = calls
        self.state, self.links = {}, {}
        self.episodes, self.open_episodes, self.timeline = [], {}, []
        self.operations, self.snapshot_reports, self.unverified = [], [], collections.Counter()
        self.anchor = None
        self.touched = collections.defaultdict(set)
        self.current = None
        self.last_gaps = {}
        self.segment_issues = set()
        self.groups = collections.defaultdict(dict)
        self.snapshot_by_op = {}
        for path, sample in snapshots:
            self.snapshot_by_op[sample['snapshot_id']] = (path, sample)
        self.samples = collections.defaultdict(list)
        for e in events:
            if e['point'] == 'SNAPSHOT.MEMORY.STABLE' and e['formula'] not in ('BEGIN', 'END'):
                self.samples[e['op']].append(e)
        self.root_scopes = {s['id']: s for s in self.scopes.values()
                            if not s['parent'] and s['point'] != 'SNAPSHOT.MEMORY.STABLE'}
        self.root_objects = collections.defaultdict(set)
        for e in events:
            root = root_for(e['op'], self.scopes)
            if root not in self.root_scopes:
                continue
            inode = object_key('inode', e.get('ino'), e.get('dev'))
            directory = object_key('directory', e.get('dir_ino'), e.get('dev'))
            if inode:
                self.root_objects[root].add(inode)
            if directory:
                self.root_objects[root].add(directory)
        self.root_contexts = {}
        self.call_roots = collections.defaultdict(list)
        for op, scope in self.root_scopes.items():
            matched = [c for c in calls if c.get('verified') and c['pid'] == scope['pid'] and
                       c['enter_line'] < scope['begin_line'] < scope['end_line'] < c['exit_line']]
            call = matched[0] if len(matched) == 1 else {}
            context = {'op': op, 'point': scope['point'], 'pid': scope['pid'],
                       'begin_seq': scope['begin_seq'], 'end_seq': scope['end_seq'],
                       'syscall_id': call.get('id'), 'syscall_name': call.get('name'),
                       'program': call.get('program'), 'call_index': call.get('call_index'),
                       'objects': sorted(self.root_objects[op])}
            self.root_contexts[op] = context
            if call:
                self.call_roots[call['id']].append(op)
        self.overlapping_roots = collections.defaultdict(list)
        self.interleavings = []
        active = []
        writers = sorted(self.root_scopes.values(), key=lambda s: s['begin_seq'])
        for scope in writers:
            active = [other for other in active if (other['end_seq'] or 2**64) > scope['begin_seq']]
            for other in active:
                if other['pid'] == scope['pid']:
                    continue
                shared_objects = sorted(self.root_objects[scope['id']] &
                                        self.root_objects[other['id']])
                left, right = self.root_contexts[other['id']], self.root_contexts[scope['id']]
                self.overlapping_roots[scope['id']].append(
                    {'operation': left, 'shared_objects': shared_objects})
                self.overlapping_roots[other['id']].append(
                    {'operation': right, 'shared_objects': shared_objects})
                self.interleavings.append({'left': left, 'right': right,
                                           'shared_objects': shared_objects,
                                           'basis': ['ext4_scope_interval']})
            active.append(scope)

        # Ext4 probes can bracket only the mutation-critical portion of a
        # syscall. Two verified syscalls may overlap at the native boundary
        # even when ext4's narrower helper scopes happen to serialize. Keep
        # both observations and attribute shared inode/directory access at the
        # syscall boundary so those races are not lost from the report.
        self.native_interleavings = []
        self.syscall_overlaps = collections.defaultdict(list)
        call_contexts = {}
        for call in calls:
            if not call.get('verified') or call.get('enter_ns') is None or call.get('exit_ns') is None:
                continue
            roots = self.call_roots.get(call['id'], [])
            base = dict(self.root_contexts[roots[0]]) if roots else {}
            objects = set()
            for e in events:
                if e['pid'] != call['pid'] or not call['enter_line'] < e['line'] < call['exit_line']:
                    continue
                inode = object_key('inode', e.get('ino'), e.get('dev'))
                directory = object_key('directory', e.get('dir_ino'), e.get('dev'))
                if inode:
                    objects.add(inode)
                if directory:
                    objects.add(directory)
            # Some ext4 allocation paths only expose block-group counters and
            # have no inode in their probe payload. The syscall wrapper records
            # fstat identity for fd based operations before the native window.
            if call.get('object_ino'):
                object_type = 'directory' if call.get('object_is_dir') else 'inode'
                identity = object_key(object_type, call['object_ino'], call.get('object_dev'))
                if identity:
                    objects.add(identity)
            base.update(op=(roots[0] if len(roots) == 1 else roots or call['id']),
                        scope_ids=roots, pid=call['pid'],
                        syscall_id=call['id'], syscall_name=call.get('name'),
                        program=call.get('program'), call_index=call.get('call_index'),
                        enter_ns=call['enter_ns'], exit_ns=call['exit_ns'],
                        objects=sorted(objects))
            call_contexts[call['id']] = (call, base, objects)

        scope_pairs_by_syscall = {}
        for row in self.interleavings:
            left_id, right_id = row['left'].get('syscall_id'), row['right'].get('syscall_id')
            if left_id and right_id:
                scope_pairs_by_syscall[frozenset((left_id, right_id))] = row
        contexts = list(call_contexts.values())
        for index, (left_call, left, left_objects) in enumerate(contexts):
            for right_call, right, right_objects in contexts[index+1:]:
                if left_call['pid'] == right_call['pid'] or not (
                        left_call['enter_ns'] < right_call['exit_ns'] and
                        right_call['enter_ns'] < left_call['exit_ns']):
                    continue
                shared_objects = sorted(left_objects & right_objects)
                row = {'left': left, 'right': right,
                       'shared_objects': shared_objects,
                       'basis': ['native_syscall_interval']}
                self.native_interleavings.append(row)
                pair_key = frozenset((left_call['id'], right_call['id']))
                existing = scope_pairs_by_syscall.get(pair_key)
                if existing:
                    existing['basis'] = sorted(set(existing.get('basis', [])) | {'native_syscall_interval'})
                    existing['shared_objects'] = sorted(
                        set(existing.get('shared_objects', [])) | set(shared_objects))
                    aggregate_row = existing
                else:
                    self.interleavings.append(row)
                    scope_pairs_by_syscall[pair_key] = row
                    aggregate_row = row
                self.syscall_overlaps[left_call['id']].append(
                    {'operation': right, 'shared_objects': shared_objects})
                self.syscall_overlaps[right_call['id']].append(
                    {'operation': left, 'shared_objects': shared_objects})
                if shared_objects:
                    for root in self.call_roots.get(left_call['id'], []):
                        self.overlapping_roots[root].append(
                            {'operation': right, 'shared_objects': shared_objects})
                    for root in self.call_roots.get(right_call['id'], []):
                        self.overlapping_roots[root].append(
                            {'operation': left, 'shared_objects': shared_objects})

    def confidence(self, e):
        if not self.valid or self.anchor is None or self.segment_issues:
            return 'unsupported_or_unverified'
        root = root_for(e['op'], self.scopes)
        if root is None or self.overlapping_roots.get(root):
            return 'concurrent_or_ambiguous'
        return 'precise_single_writer_recording_order'

    def observe_gap(self, key, gap, e, source='derived', object_=None, stable=False):
        item = self.open_episodes.get(key)
        confidence = ('stable_snapshot' if stable and self.valid else self.confidence(e))
        if gap == 0 and item is None:
            return
        if item is None:
            item = {'id': len(self.episodes) + len(self.open_episodes) + 1,
                    'stream': key, 'object': object_, 'onset_seq': e['seq'],
                    'onset_point': e['point'], 'onset_ns': e['timestamp_ns'],
                    'peak_abs_gap': abs(gap), 'peak_gap': gap, 'peak_seq': e['seq'],
                    'source': source, 'confidence': confidence,
                    'operation_ids': [], 'syscall_ids': [], 'exit_evidence': [],
                    'stable_evidence': [], 'classification': None, 'recovered_seq': None}
            self.open_episodes[key] = item
        if confidence in ('concurrent_or_ambiguous', 'unsupported_or_unverified'):
            item['confidence'] = confidence if item['confidence'] != 'unsupported_or_unverified' else item['confidence']
        if abs(gap) > item['peak_abs_gap']:
            item.update(peak_abs_gap=abs(gap), peak_gap=gap, peak_seq=e['seq'])
        for field, ident in [('operation_ids', root_for(e['op'], self.scopes)),
                             ('syscall_ids', native_for(e, self.calls))]:
            if ident is not None and ident not in item[field]:
                item[field].append(ident)
        item.update(last_seq=e['seq'], last_gap=gap)
        if stable:
            item['stable_evidence'].append({'snapshot_id': e['op'], 'seq': e['seq'], 'gap': gap})
        if gap == 0:
            item.update(recovered_seq=e['seq'], recovery_point=e['point'],
                        recovery_ns=e['timestamp_ns'], duration_ns=max(0, e['timestamp_ns'] - item['onset_ns']),
                        timestamp_order_valid=e['timestamp_ns'] >= item['onset_ns'])
            self.finish(key)

    def finish(self, key):
        item = self.open_episodes.pop(key)
        if any(x['gap'] for x in item['stable_evidence']):
            item['classification'] = 'persistent_mismatch'
        elif item['confidence'] in ('unsupported_or_unverified', 'concurrent_or_ambiguous'):
            item['classification'] = item['confidence']
        elif item['exit_evidence']:
            item['classification'] = 'operation_exit_mismatch'
        elif item['recovered_seq'] is not None:
            item['classification'] = 'transient_mismatch'
        else:
            item['classification'] = 'unsupported_or_unverified'
            item['unverified_reason'] = 'no_recovery_or_valid_stable_confirmation'
        item['id'] = len(self.episodes) + 1
        self.episodes.append(item)

    def gaps(self):
        result = {}
        for formula, state in self.state.items():
            result[formula + ':descriptor-independent'] = state['descriptor'] - state['independent']
            result[formula + ':global-independent'] = state['global'] - state['independent']
        for formula, groups in self.groups.items():
            for group, state in groups.items():
                result[f'{formula}:group:{group}:descriptor-independent'] = state['descriptor'] - state['independent']
        for ino, state in self.links.items():
            if (state.get('refs') is not None and state.get('nlink') is not None and
                    not state.get('unsupported') and not state.get('tainted')):
                result[f'R-LINK:{ino}:{state["lifetime"]}'] = state['nlink'] - state['refs']
        return result

    def counter(self, e):
        f, point = e['formula'], e['point']
        spec = COUNTERS.get(f)
        if spec is None:
            return
        if point.startswith(SPECIAL_PREFIXES) or e.get('reason', '').startswith('replay_'):
            self.segment_issues.add('special_domain_requires_own_anchor')
            self.unverified['special_domain_requires_own_anchor'] += 1
            return
        lane = next((k for k in ('independent', 'descriptor', 'global') if point in spec[k]), None)
        # schema 1 recorded block descriptors after the lock/dirty helper;
        # legacy delta is usable only as an explicitly unverified trajectory.
        if e['schema'] < 2 and point == 'BLOCK.DESCRIPTOR.PUBLISH':
            lane = 'descriptor'
            self.segment_issues.add('legacy_descriptor_order')
        if lane:
            if f not in self.state:
                self.unverified['counter_before_valid_anchor'] += 1
                return
            self.state[f][lane] += e['update_delta']
            root = root_for(e['op'], self.scopes)
            self.touched[root].add(f)
            if e['group'] != 4294967295 and lane != 'global':
                group = self.groups[f].get(e['group'])
                if group is not None:
                    group[lane] += e['update_delta']
        elif e['update_delta'] and point != 'BLOCK.BIGALLOC.RERESERVE.DIRTY':
            self.segment_issues.add(f'unmodeled_conversion:{point}')
            self.unverified[f'unmodeled_conversion:{point}'] += 1

    def namespace(self, e):
        if e['formula'] != 'R-LINK' or e['point'] == 'SNAPSHOT.MEMORY.STABLE':
            return
        root = self.scopes.get(root_for(e['op'], self.scopes), {})
        if root.get('point', '').startswith(SPECIAL_PREFIXES) or e['point'].startswith(SPECIAL_PREFIXES):
            self.segment_issues.add('special_namespace_requires_own_anchor')
            self.unverified['special_namespace_requires_own_anchor'] += 1
            return
        ino, step = e['ino'], e.get('step', 0)
        if not ino:
            return
        if e['schema'] < 2:
            self.unverified['legacy_namespace_without_applied_ref_delta'] += 1
            return
        state = self.links.get(ino)
        root_id = root_for(e['op'], self.scopes)
        same_inode_interleaving = any(
            object_key('inode', ino, e.get('dev')) in conflict['shared_objects']
            for conflict in self.overlapping_roots.get(root_id, ()))
        if same_inode_interleaving:
            if state is None:
                state = {'lifetime': f'concurrent-at-{e["seq"]}', 'refs': None,
                         'nlink': None, 'generation': e.get('generation'),
                         'is_dir': e.get('is_dir', 0), 'dead': False, 'tainted': True}
                self.links[ino] = state
            else:
                state['tainted'] = True
            self.unverified['concurrent_shared_inode_trajectory'] += 1
            return
        if state and state.get('tainted'):
            return
        if step == 32:
            if state and not state.get('dead'):
                self.segment_issues.add('inode_rebirth_without_death')
            state = {'lifetime': f'birth-{e["seq"]}-gen-{e.get("generation", 0)}',
                     'refs': 0, 'nlink': e['nlink_after'], 'generation': e.get('generation'),
                     'is_dir': e.get('is_dir', 0), 'dead': False, 'tainted': False}
            self.links[ino] = state
        elif state is None:
            self.unverified['namespace_inode_without_baseline_or_birth'] += 1
            return
        elif step == 16:
            state['refs'] += e.get('ref_delta', 0)
        elif step in (4, 8):
            generation = e.get('generation')
            if state.get('generation') is not None and generation != state['generation']:
                state['unsupported'] = 'inode_generation_changed_without_birth'
                self.unverified['inode_generation_changed_without_birth'] += 1
            elif state.get('generation') is None:
                state['generation'] = generation
            before, after = e['nlink_before'], e['nlink_after']
            # Only an immediately repeated wrapper is redundant. Any other
            # discontinuity is evidence of missing coverage or concurrency.
            if state['nlink'] != before and not (state['nlink'] == after and
                    state.get('last_transition') == (before, after, e['op'])):
                state['tainted'] = True
                self.unverified['nlink_before_discontinuity'] += 1
                return
            state.update(nlink=after, last_transition=(before, after, e['op']))
            if e.get('is_dir') and after == 1:
                state['unsupported'] = 'dir_nlink_sentinel_or_unmaintained'
                self.unverified['dir_nlink_sentinel_or_unmaintained'] += 1
        elif step == 64:
            # Death does not erase an unexplained residual reference count.
            state.update(nlink=e['nlink_after'], dead=True)
        else:
            return  # Helper result is metadata, never an independent conversion.
        root = root_for(e['op'], self.scopes)
        self.touched[root].add(f'R-LINK:{ino}:{state["lifetime"]}')

    def snapshot(self, e):
        entry = self.snapshot_by_op.get(e['op'])
        if not entry:
            self.unverified['snapshot_without_json_manifest'] += 1
            return
        path, sample = entry
        rows = self.samples[e['op']]
        scope = self.scopes[e['op']]
        overlapping_operations = [self.root_contexts[op] for op, writer in self.root_scopes.items()
                                  if writer['pid'] != scope['pid'] and
                                  writer['begin_seq'] < scope['end_seq'] and
                                  (writer['end_seq'] or 2**64) > scope['begin_seq']]
        overlap = bool(overlapping_operations)
        base_valid = self.valid and sample.get('valid', False) and not overlap
        ns_valid = base_valid and sample.get('namespace_valid', False)
        item = {'file': path, 'snapshot_id': e['op'], 'seq': e['seq'], 'valid': base_valid,
                'namespace_valid': ns_valid, 'reason': sample.get('reason'),
                'namespace_reason': sample.get('namespace_reason'),
                'relations_valid': sample.get('relations_valid'),
                'relations_reason': sample.get('relations_reason'),
                'observed_mismatches': sample.get('observed_mismatches'),
                'quiescent': not overlap,
                'overlapping_operations': overlapping_operations,
                'coverage': {k: v for k, v in sample.items() if k.startswith('namespace_')},
                'reconciliation': {}}
        for f, spec in COUNTERS.items():
            groups = [r for r in rows if r['formula'] == spec['group'] and r['status'] in (1, 2)]
            total = [r for r in rows if r['formula'] == spec['sum'] and r['status'] in (1, 2)]
            if not (ns_valid if f == 'R-UDELTA' else base_valid) or len(groups) != sample.get('groups') or len(total) != 1:
                self.unverified[f'unverified_stable_anchor:{f}'] += 1
                continue
            observed = {'independent': sum(r['expected'] for r in groups),
                        'descriptor': sum(r['actual'] for r in groups), 'global': total[0]['actual']}
            if total[0]['expected'] != observed['descriptor'] or sample.get(spec['field']) != observed['global']:
                self.integrity.append(f'{path}:snapshot_payload_disagrees:{f}')
                self.valid = False
                item['valid'] = item['namespace_valid'] = False
                continue
            if f in self.state:
                difference = {k: observed[k] - self.state[f][k] for k in observed}
                item['reconciliation'][f] = difference
                if any(difference.values()):
                    self.unverified[f'trajectory_snapshot_discontinuity:{f}'] += 1
                    self.segment_issues.add(f'trajectory_snapshot_discontinuity:{f}')
            self.state[f] = dict(observed, anchor_seq=e['seq'], snapshot_id=e['op'])
            self.groups[f] = {r['group']: {'independent': r['expected'], 'descriptor': r['actual']}
                              for r in groups}
            for row in groups:
                self.observe_gap(f'{f}:group:{row["group"]}:descriptor-independent',
                                 row['actual'] - row['expected'], e, source='stable_sample',
                                 object_=row['group'], stable=True)
            for lane in ('descriptor', 'global'):
                self.observe_gap(f'{f}:{lane}-independent', observed[lane] - observed['independent'],
                                 e, source='stable_sample', stable=True)
        if ns_valid:
            link_rows = [r for r in rows if r['formula'] == 'R-LINK' and r['status'] in (1, 2) and r['ino']]
            if len(link_rows) != sample.get('namespace_compared'):
                self.integrity.append(f'{path}:namespace_sample_count_disagrees')
                self.valid = False
                item['namespace_valid'] = False
            else:
                for r in link_rows:
                    state = self.links.setdefault(r['ino'], {'lifetime': f'existing-at-{e["seq"]}',
                                                          'generation': None, 'is_dir': None,
                                                          'dead': False, 'tainted': False})
                    key = f'R-LINK:{r["ino"]}:{state["lifetime"]}'
                    if (not state.get('tainted') and state.get('refs') is not None and
                            (state['refs'] != r['expected'] or state['nlink'] != r['actual'])):
                        self.unverified['namespace_snapshot_discontinuity'] += 1
                        self.segment_issues.add('namespace_snapshot_discontinuity')
                        item['reconciliation'][key] = {'refs': r['expected'] - state['refs'],
                                                       'nlink': r['actual'] - state['nlink']}
                    state.update(refs=r['expected'], nlink=r['actual'], tainted=False)
                    # This valid scan proves the gap even when prior attribution
                    # was ambiguous. It does not retroactively prove the path.
                    self.observe_gap(key, r['actual'] - r['expected'], e, source='stable_sample',
                                     object_=r['ino'], stable=True)
        if base_valid:
            self.anchor = e['seq']
            self.segment_issues.clear()
        else:
            self.unverified['invalid_snapshot'] += 1
        self.snapshot_reports.append(item)

    def boundary(self, e, kind='ext4_operation'):
        op = e.get('op')
        scope = self.scopes.get(op)
        if scope is None or scope['point'] == 'SNAPSHOT.MEMORY.STABLE':
            return
        root = root_for(op, self.scopes)
        relevant = self.touched.get(root, set())
        gap_map = self.gaps()
        gaps = {k: v for k, v in gap_map.items() if k in relevant or k.split(':')[0] in relevant}
        confidence = self.confidence(e)
        overlapping_ids = unique_operation_ids(self.overlapping_roots.get(root, []))
        # Publication helpers end at different stages. Inner MARK_CONTEXT and
        # rereserve frees are permitted to leave work for their parent.
        eligible = op == root
        record = {'kind': kind, 'op': op, 'root_op': root, 'parent': e['parent'],
                  'point': e['point'], 'seq': e['seq'], 'result': scope['result'],
                  'syscall_id': native_for(e, self.calls), 'gaps': gaps,
                  'overlapping_operation_ids': overlapping_ids,
                  'confidence': confidence, 'publication_boundary': eligible,
                  'classification': 'consistent' if not any(gaps.values()) else
                      ('pending_parent_publication' if not eligible else
                       ('operation_exit_mismatch' if confidence == 'precise_single_writer_recording_order' else confidence))}
        self.operations.append(record)
        if eligible and confidence == 'precise_single_writer_recording_order':
            for key, gap in gaps.items():
                if gap and key in self.open_episodes:
                    self.open_episodes[key]['exit_evidence'].append({'kind': kind, 'op': op,
                                                                  'seq': e['seq'], 'gap': gap,
                                                                  'result': scope['result']})

    def run(self):
        for e in self.events:
            self.current = e
            if e['formula'] == 'END' and e['point'] == 'SNAPSHOT.MEMORY.STABLE':
                self.snapshot(e)
            else:
                self.counter(e)
                self.namespace(e)
                gaps = self.gaps()
                for key, gap in gaps.items():
                    if gap != self.last_gaps.get(key, 0):
                        self.observe_gap(key, gap, e)
                self.last_gaps = gaps
                # Valid local comparisons remain evidence even with no anchor.
                if e['status'] == 2 and e['point'] != 'SNAPSHOT.MEMORY.STABLE':
                    self.observe_gap(f'LOCAL:{e["formula"]}:{e["seq"]}', e['actual'] - e['expected'],
                                     e, source='sampled_local')
                if e['formula'] == 'END':
                    self.boundary(e)
            root = root_for(e['op'], self.scopes)
            syscall_id = native_for(e, self.calls)
            native = next((c for c in self.calls if c['id'] == syscall_id), {}) if syscall_id else {}
            overlaps = self.overlapping_roots.get(root, [])
            call_overlaps = self.syscall_overlaps.get(syscall_id, []) if syscall_id else []
            overlap_ids = unique_operation_ids(overlaps)
            interference = any(row.get('shared_objects') for row in call_overlaps)
            self.timeline.append({'run': self.name, 'event': e, 'value_kind': value_kind(e),
                                  'root_op': root, 'syscall_id': syscall_id,
                                  'program': native.get('program'), 'call_index': native.get('call_index'),
                                  'syscall_name': native.get('name'),
                                  'overlapping_operation_ids': overlap_ids,
                                  'overlapping_syscall_ids': [row['operation']['syscall_id']
                                                              for row in call_overlaps],
                                  'shared_object_interference': interference,
                                  'event_source': e.get('event_source', 'generated_syscall' if syscall_id else 'outside_generated_call'),
                                  'attribution': ('same_task_native_syscall_with_interference'
                                                  if syscall_id and (overlaps or interference) else
                                                  'same_task_native_syscall' if syscall_id else
                                                  'cross_thread_interleaving' if overlaps else
                                                  'no_cross_thread_cause'),
                                  'confidence': self.confidence(e), 'anchor_seq': self.anchor,
                                  'sampled_comparison': {'valid': e['status'] in (1, 2),
                                      'actual': e['actual'] if e['status'] in (1, 2) else None,
                                      'expected': e['expected'] if e['status'] in (1, 2) else None,
                                      'delta': e['actual']-e['expected'] if e['status'] in (1, 2) else None},
                                  'derived_counters': {f: dict(s) for f, s in self.state.items()},
                                  'derived_gaps': self.gaps(),
                                  'derived_groups': {f: {g: dict(state) for g, state in groups.items()}
                                                     for f, groups in self.groups.items()},
                                  'derived_inode': dict(self.links[e['ino']]) if e['ino'] in self.links else None})
            self.last_gaps = self.gaps()
        for key in list(self.open_episodes):
            self.finish(key)
        # Native syscall return is assessed using the last counter event inside
        # its native pair, never the userspace marker or ext4 END return.
        for call in self.calls:
            rows = [r for r in self.timeline if r['syscall_id'] == call['id']]
            if not rows:
                continue
            last = max(rows, key=lambda r: r['event']['seq'])
            roots = {r['root_op'] for r in rows if r['root_op'] is not None}
            touched = set().union(*(self.touched.get(r, set()) for r in roots))
            gaps = {k: v for k, v in last['derived_gaps'].items() if k in touched or k.split(':')[0] in touched}
            confidence = last['confidence']
            overlapping_rows = []
            for root in self.call_roots.get(call['id'], []):
                overlapping_rows.extend(self.overlapping_roots.get(root, []))
            overlapping_operation_ids = unique_operation_ids(overlapping_rows)
            if overlapping_operation_ids:
                confidence = 'concurrent_or_ambiguous'
            record = {'kind': 'native_syscall_return', 'syscall_id': call['id'], 'name': call['name'],
                      'program': call.get('program'), 'call_index': call.get('call_index'),
                      'pid': call['pid'], 'enter_line': call['enter_line'], 'exit_line': call['exit_line'],
                      'raw_result': call['raw_result'], 'errno': call['errno'], 'seq': last['event']['seq'],
                      'gaps': gaps, 'confidence': confidence,
                      'overlapping_operation_ids': overlapping_operation_ids,
                      'classification': 'consistent' if not any(gaps.values()) else
                          ('operation_exit_mismatch' if confidence == 'precise_single_writer_recording_order' else confidence)}
            self.operations.append(record)
            if record['classification'] == 'operation_exit_mismatch':
                for episode in self.episodes:
                    gap = gaps.get(episode['stream'], 0)
                    seq = last['event']['seq']
                    if gap and episode['onset_seq'] <= seq and (episode['recovered_seq'] is None or
                                                                 episode['recovered_seq'] > seq):
                        episode['exit_evidence'].append({'kind': 'native_syscall_return',
                            'syscall_id': call['id'], 'seq': seq, 'exit_line': call['exit_line'],
                            'gap': gap, 'raw_result': call['raw_result'], 'errno': call['errno']})
                        if episode['classification'] == 'transient_mismatch':
                            episode['classification'] = 'operation_exit_mismatch'
        classes = collections.Counter(e['classification'] for e in self.episodes)
        return {'name': self.name, 'events': len(self.events), 'trajectory_integrity': not self.integrity,
                'integrity_issues': self.integrity, 'classification_counts': {c: classes[c] for c in CLASSES},
                'unverified_reasons': dict(self.unverified), 'snapshots': self.snapshot_reports,
                'final_derived_gaps': self.gaps(), 'episodes': self.episodes, 'boundaries': self.operations,
                'interleavings': self.interleavings,
                'scope_interleavings': [row for row in self.interleavings
                                        if 'ext4_scope_interval' in row.get('basis', [])],
                'native_syscall_interleavings': self.native_interleavings,
                'shared_object_interleavings': sum(bool(row['shared_objects']) for row in self.interleavings),
                'scope_count': len(self.scopes), 'write_coverage': self.write_coverage,
                'object_checks': relation_summary(self.events, not self.integrity)}


def analyze(directory, output=None):
    directory = pathlib.Path(directory)
    output = pathlib.Path(output) if output else directory / 'lifecycle'
    output.mkdir(parents=True, exist_ok=True)
    events, lines = parse_trace(directory / 'trace.txt')
    calls, syscall_issues = correlate_syscalls(lines)
    sources = attribute_sources(events, lines, calls)
    exploration = exploration_metrics(events, calls)
    stats, loss_issues = loss_evidence(directory)
    manifest_path = directory / 'chain-manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    intervals = manifest.get('intervals')
    if intervals is None:
        intervals = [{'name': p.name[:-11], 'start': p.name,
                      'end': p.name[:-11] + '-end.json'} for p in sorted(directory.glob('*-start.json'))]
    reports, timeline = [], []
    snapshot_payloads = {}
    global_issues = list(loss_issues)
    observed_cpus = {e['cpu'] for e in events}
    if any(f'cpu{cpu}-stats.txt' not in stats for cpu in observed_cpus):
        global_issues.append('missing_loss_stats_for_observed_cpu')
    header = re.search(r'#P:(\d+)', (directory / 'trace.txt').read_text()[:4096])
    if header and any(f'cpu{cpu}-stats.txt' not in stats for cpu in range(int(header[1]))):
        global_issues.append('missing_loss_stats_for_trace_cpu')
    if not intervals:
        global_issues.append('no_explicit_sequence_intervals')
    for interval in intervals:
        name = interval['name']
        start = json.loads((directory / interval['start']).read_text())
        end = json.loads((directory / interval['end']).read_text())
        issues = list(global_issues)
        selected = sorted((e for e in events if e['epoch'] == start['epoch'] and
                           start['seq'] < e['seq'] <= end['seq'] and
                           ('dev' not in interval or e['dev'] == interval['dev'])), key=lambda e: e['seq'])
        seqs = [e['seq'] for e in selected]
        if end['epoch'] != start['epoch'] or seqs != list(range(start['seq'] + 1, end['seq'] + 1)):
            issues.append('sequence_missing_duplicate_or_wrong_epoch')
        if len({e['dev'] for e in selected}) != 1:
            issues.append('device_identity_not_unique')
        if start.get('active') != 0 or end.get('active') != 0:
            issues.append('active_interval_endpoint')
        snapshots = []
        candidates = interval.get('snapshots')
        paths = [directory / n for n in candidates] if candidates is not None else sorted(directory.glob(name + '-*.json'))
        for path in paths:
            sample = json.loads(path.read_text())
            if 'snapshot_id' in sample:
                snapshots.append((path.name, sample))
                snapshot_payloads[path.name] = sample
        if candidates is None:
            for path in sorted(directory.glob(name + '-*.jsonl')):
                if path.name.endswith('-concurrent.jsonl'):
                    for number, line in enumerate(path.read_text().splitlines(), 1):
                        sample = json.loads(line)
                        if 'snapshot_id' in sample:
                            ident = f'{path.name}:line{number}'
                            snapshots.append((ident, sample))
                            snapshot_payloads[ident] = sample
        engine = Lifecycle(name, selected, snapshots, issues, calls)
        report = engine.run()
        report.update(epoch=start['epoch'], dev=selected[0]['dev'] if selected else None,
                      start_seq=start['seq'], end_seq=end['seq'], mode=interval.get('mode', 'unspecified'))
        reports.append(report)
        timeline.extend(engine.timeline)
    classified = collections.Counter(e['classification'] for r in reports for e in r['episodes'])
    precise = [r for r in reports if r['mode'] == 'single']
    blockers = []
    for r in reports:
        if r['object_checks']['mismatch_count']:
            blockers.append(r['name'] + ':object_postcondition_or_relation_mismatch')
        if r['integrity_issues']:
            blockers.append(r['name'] + ':invalid_trace')
        if any(any(d.values()) for s in r['snapshots'] for k, d in s['reconciliation'].items()):
            blockers.append(r['name'] + ':trajectory_snapshot_discontinuity')
        if any(s.get('relations_valid') is False for s in r['snapshots'] if s['valid']):
            blockers.append(r['name'] + ':unverified_stable_object_relations')
        if not r['snapshots'] or not any(s['valid'] and s['namespace_valid'] for s in r['snapshots']):
            blockers.append(r['name'] + ':no_valid_stable_confirmation')
        if any(r['final_derived_gaps'].values()):
            blockers.append(r['name'] + ':unrecovered_final_gap')
        if r['classification_counts']['persistent_mismatch'] or r['classification_counts']['operation_exit_mismatch']:
            blockers.append(r['name'] + ':confirmed_mismatch')
        # A single-writer interval may contain events before its first valid
        # stable snapshot.  These two reasons describe that expected prefix;
        # they are retained in the report but cannot establish an episode by
        # themselves.  Other unverified reasons remain blocking evidence.
        anchor_prefix_reasons = {
            'counter_before_valid_anchor',
            'snapshot_without_json_manifest',
        }
        unverified_reasons = set(r['unverified_reasons']) - anchor_prefix_reasons
        if r['mode'] == 'single' and (r['classification_counts']['unsupported_or_unverified'] or
                                      unverified_reasons):
            blockers.append(r['name'] + ':single_writer_unverified_episode')
    if manifest.get('require_native_syscalls') and (not calls or any(not c['verified'] for c in calls) or syscall_issues):
        blockers.append('native_syscall_correlation_incomplete')
    if manifest.get('require_single_writer') and not precise:
        blockers.append('missing_single_writer_interval')
    disk = {}
    for path in sorted(directory.glob('*-disk.json')):
        item = json.loads(path.read_text())
        disk[path.name] = item
        if not item.get('valid'):
            blockers.append(path.name + ':invalid_disk_check')
        kind = path.name[:-len('-disk.json')]
        related = [r for r in reports if r['name'].startswith(kind + '-') or r['name'] == kind]
        if related:
            final = max((s for r in related for s in r['snapshots'] if s['valid']),
                        key=lambda s: s['seq'], default=None)
            if final:
                sample = snapshot_payloads[final['file']]
                observed = {'free_clusters': item['superblock']['free_clusters'],
                            'free_inodes': item['superblock']['free_inodes'],
                            'dirs': item['descriptor_totals']['dirs']}
                item['matches_final_stable_snapshot'] = all(sample[k] == v for k, v in observed.items())
                if not item['matches_final_stable_snapshot']:
                    blockers.append(path.name + ':disk_stable_snapshot_disagrees')
    if manifest.get('require_disk') and not disk:
        blockers.append('missing_disk_check')
    blockers.extend(global_issues)
    point_stats = {}
    for row in timeline:
        e = row['event']
        ident = e['formula'] + ':' + e['point']
        item = point_stats.setdefault(ident, {'hits': 0, 'statuses': {}, 'value_kinds': {},
                                             'sampled_delta_distribution': {}, 'unverified_reasons': {}})
        item['hits'] += 1
        for field, value in [('statuses', str(e['status'])), ('value_kinds', row['value_kind'])]:
            item[field][value] = item[field].get(value, 0) + 1
        if e['status'] in (1, 2):
            delta = str(e['actual'] - e['expected'])
            item['sampled_delta_distribution'][delta] = item['sampled_delta_distribution'].get(delta, 0) + 1
        elif e['status'] == 3:
            reason = e.get('reason', 'unspecified')
            item['unverified_reasons'][reason] = item['unverified_reasons'].get(reason, 0) + 1
    relations = relation_summary([row['event'] for row in timeline], not global_issues and all(r['trajectory_integrity'] for r in reports))
    (output / 'formula-relations.json').write_text(json.dumps(relations, indent=2) + '\n')
    (output / 'formula-relations.md').write_text(relation_markdown(relations))
    report = {'object_checks': relations, 'schema': 2, 'run_id': manifest.get('run_id', directory.name),
              'boot_id': manifest.get('boot_id'), 'passed': not blockers, 'blockers': blockers,
              'classification_counts': {c: classified[c] for c in CLASSES}, 'runs': reports,
              'point_statistics': point_stats, 'event_sources': sources, 'exploration': exploration,
              'units': {'R-BDELTA': 'free_clusters', 'R-IDELTA': 'free_inodes',
                        'R-UDELTA': 'directory_inodes', 'R-LINK': 'live_directory_references'},
              'native_syscalls': calls, 'syscall_issues': syscall_issues, 'loss_stats': stats, 'disk': disk,
              'limitations': ['recording_order_is_not_cross_thread_mutation_order',
                             'derived_global_is_not_an_instantaneous_global_sample',
                             'background_cross_thread_causality_unverified',
                             'persistent_requires_valid_stable_scan',
                             'snapshot_namespace_coverage_is_bounded_and_skips_reserved_EA_dir_nlink_sentinel']}
    from point_report import lifecycle_points
    points = lifecycle_points(timeline, reports, output, report['run_id'], report['boot_id'])
    report['point_checks'] = {'schema': points['schema'], 'observed_hits': points['observed_hits'],
                             'report': 'point-report.md', 'interactive_report': 'point-report.html',
                             'statistics': 'point-report.json', 'hits': 'point-hits.jsonl.gz'}
    with (output / 'timeline.jsonl').open('w') as stream:
        for row in timeline:
            row.update(run_id=report['run_id'], boot_id=report['boot_id'])
            stream.write(json.dumps(row, separators=(',', ':')) + '\n')
    (output / 'episodes.json').write_text(json.dumps([dict(run=r['name'], **e) for r in reports for e in r['episodes']], indent=2) + '\n')
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    md = [f'# Counter lifecycle: {report["run_id"]}', '', f'Validation: {"PASS" if report["passed"] else "FAIL"}', '',
          '| Classification | Episodes |', '| --- | ---: |']
    md.extend(f'| {c} | {classified[c]} |' for c in CLASSES)
    md += ['', '[Per-probe hit counts and comparison outcomes](point-report.md) · '
           '[Interactive per-probe report](point-report.html)', '']
    md += ['', 'Every timeline row retains the raw event, value semantics, derived values, anchor and attribution.',
           'Derived global values are cumulative publications anchored to stable samples.', '',
           f'Native syscall pairs: {sum(c["verified"] for c in calls)}/{len(calls)}.', '',
           '## Intervals', '']
    for r in reports:
        md += [f'### {r["name"]}', '', f'{r["events"]} events; integrity={r["trajectory_integrity"]}; mode={r["mode"]}.', '',
               f'Final gaps: `{json.dumps({k:v for k,v in r["final_derived_gaps"].items() if v})}`.', '',
               f'Unverified reasons: `{json.dumps(r["unverified_reasons"], sort_keys=True)}`.', '']
    md += ['## Limits', ''] + ['- ' + x for x in report['limitations']]
    if blockers:
        md += ['', '## Validation blockers', ''] + ['- ' + x for x in blockers]
    (output / 'report.md').write_text('\n'.join(md) + '\n')
    return report
