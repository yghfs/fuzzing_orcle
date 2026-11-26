"""Program-level joint trajectories, exact planned/actual injections and mutation."""
from __future__ import annotations
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
BASE = PROJECT.parent/'ext4-counter-trace'
sys.path.insert(0, str(BASE/'scripts'))
import trace_lifecycle as lifecycle

KEYS = ('call', 'site_id', 'occurrence', 'context')


def identity(event): return tuple(int(event[k]) for k in KEYS)


def plan_text(plan):
    return ''.join('plan ' + ' '.join(map(str, item))+'\n' for item in sorted(plan))


def load_plan(path):
    return [tuple(int(line[k]) for k in KEYS) for line in json.loads(Path(path).read_text())]


def mutate(plan, candidates, seen, maximum=64):
    """Flip each actually reached eligible point, including fault combinations."""
    current = frozenset(plan)
    children = []
    for point in candidates:
        child = current ^ {identity(point)}
        key = tuple(sorted(child))
        if child and len(child) <= maximum and key not in seen:
            seen.add(key)
            children.append(key)
    return children


def parse_session(path):
    lines = Path(path).read_text().splitlines()
    summary = {k: int(v) for k, v in re.findall(r'(\w+)=(\d+)', lines[0])}
    plans = []
    for line in lines[1:]:
        values = dict(re.findall(r'(\w+)=([^\s]+)', line))
        plans.append(dict(status=values.pop('status'), **{k:int(v) for k,v in values.items()}))
    return summary, plans


def trajectory(lines, calls, policies, run, device=None):
    """Independently verify the kernel gate against native syscall windows."""
    events, candidates, pending, issues = [], {}, {}, []
    occurrences = Counter()
    gate = False
    last_order = 0
    known = {p['site_id']: p for p in policies}
    next_counters, next_by_pid = {}, {}
    for line in reversed(lines):
        if line['body'].startswith('ext4_counter_event:'):
            next_by_pid[line['pid']] = line
        elif line['body'].startswith('efi_formula:'):
            next_counters[line['line']] = next_by_pid.get(line['pid'])
    for line in lines:
        body = line['body']
        if not body.startswith(('efi_site:', 'efi_formula:')): continue
        data = dict(re.findall(r'(\w+)=([^\s]+)', body))
        for key in data.keys() - {'point','formula','dev','phase'}:
            data[key] = int(data[key])
        data.update(trace_line=line['line'], pid=line['pid'], timestamp_ns=line['timestamp_ns'])
        call = next((c for c in calls if c['verified'] and c.get('call_index') == data['call'] and
                     c['pid'] == line['pid'] and c['enter_line'] < line['line'] < c['exit_line']), None)
        if data['run'] != run or call is None:
            issues.append(f'event_outside_native_program_window:{line["line"]}')
            continue
        if device is not None and call.get('object_dev') != device:
            issues.append(f'event_on_other_device:{line["line"]}')
            continue
        if body.startswith('efi_formula:'):
            data['kind'] = 'formula'
            if data['point'].startswith(('SNAPSHOT.', 'MOUNT.', 'UNMOUNT.')) or data['formula'] in ('BEGIN','END','ALL'):
                issues.append(f'administrative_gate:{line["line"]}')
                continue
            if device is not None:
                if data['dev'] != f'{os.major(device)},{os.minor(device)}':
                    issues.append(f'formula_on_other_device:{line["line"]}')
                    continue
                peer = next_counters.get(line['line'])
                if peer and peer['line'] >= call['exit_line']: peer = None
                fields = lifecycle.fields(peer['body']) if peer else {}
                if any(fields.get(k) != data[k] for k in ('dev', 'point', 'formula')):
                    issues.append(f'formula_without_counter_event:{line["line"]}')
                    continue
            gate = True
        else:
            data['kind'] = 'error'
            if data['site_id'] not in known:
                issues.append(f'unknown_site:{data["site_id"]}'); continue
            if data['phase'] not in ('enter', 'return'):
                issues.append(f'invalid_site_phase:{line["line"]}'); continue
            if any(data[k] not in (0, 1) for k in ('eligible', 'allowed', 'injected', 'pointer')):
                issues.append(f'invalid_site_flags:{line["line"]}')
            if bool(data['eligible']) != gate and data['phase'] == 'enter':
                issues.append(f'incorrect_formula_gate:{line["line"]}')
            if data['injected'] and not data['eligible']:
                issues.append(f'injected_before_formula:{line["line"]}')
            if data['injected'] and (not data['allowed'] or not known[data['site_id']]['supported']):
                issues.append(f'injected_without_failure_contract:{line["line"]}')
            key = identity(data)
            if data['phase'] == 'enter':
                if data['order'] in pending: issues.append('duplicate_enter')
                pending[data['order']] = data
                occurrence_key = (data['call'], data['site_id'])
                occurrences[occurrence_key] += 1
                if data['occurrence'] != occurrences[occurrence_key]:
                    issues.append(f'incorrect_site_occurrence:{line["line"]}')
                if data['eligible'] and data['allowed'] and known[data['site_id']]['supported']:
                    candidates[key] = {k: data[k] for k in KEYS}
            else:
                start = pending.pop(data['order'], None)
                if not start or identity(start) != key or any(start[k] != data[k] for k in ('injected', 'eligible', 'allowed')):
                    issues.append(f'unpaired_return:{line["line"]}')
                data['result_class'] = result_class(data, known[data['site_id']])
        if data.get('phase') != 'return':
            if data['order'] != last_order + 1: issues.append('missing_or_duplicate_program_order')
            last_order = data['order']
        events.append(data)
    if pending: issues.append('missing_returns')
    return events, list(candidates.values()), issues


def result_class(event, policy):
    """Do not admit allocator addresses or arbitrary byte counts as novelty."""
    value = event['result']
    if policy['callee'] == 'get_zeroed_page_noprof': return 'allocated' if value else 'null'
    if -4095 <= value < 0: return f'errno:{value}'
    if value < 0: return 'negative_non_errno'
    if policy['callee'] in ('copy_from_user', 'copy_to_user'):
        return 'uncopied' if value else 'copied'
    return 'positive' if value else 'zero'


def reconcile_session(session, outcomes, joint, plan, run, calls):
    issues = []
    if session.get('run') != run or session.get('calls') != calls:
        issues.append('session_program_mismatch')
    if len(outcomes) != len(set(map(identity, outcomes))) or {identity(p) for p in outcomes} != set(plan):
        issues.append('session_plan_mismatch')
    enters = {identity(e): e for e in joint if e['kind'] == 'error' and e['phase'] == 'enter'}
    fired = {key for key, event in enters.items() if event['injected']}
    if fired != {identity(p) for p in outcomes if p['status'] == 'injected'}:
        issues.append('session_trace_injection_disagreement')
    for key, expected in [('formulas', sum(e['kind'] == 'formula' for e in joint)),
                          ('sites', len(enters)), ('injected', len(fired))]:
        if session.get(key) != expected: issues.append('session_trace_count_disagreement:' + key)
    for outcome in outcomes:
        event = enters.get(identity(outcome))
        status = outcome['status']
        if status not in ('injected', 'blocked', 'not_reached') or (
                status == 'not_reached' and event is not None) or (
                status == 'blocked' and (event is None or (event['eligible'] and event['allowed']))):
            issues.append('session_plan_outcome_disagreement')
    return issues


def analyze_round(folder, metadata, policies, plan, run):
    folder = Path(folder)
    events, lines = lifecycle.parse_trace(folder/'trace.txt')
    calls, syscall_issues = lifecycle.correlate_syscalls(lines)
    calls = [c for c in calls if c.get('program') == 'p']
    issues = []
    if syscall_issues: issues.append('native_syscall_issues')
    expected = metadata['calls']
    if len(calls) != len(expected): issues.append('incomplete_program_calls')
    for index, target in enumerate(expected):
        actual = [c for c in calls if c.get('call_index') == index]
        if len(actual) != 1 or not actual[0]['verified'] or actual[0]['nr'] != target['nr']:
            issues.append(f'call_not_verified:{index}')
    device = int((folder/'target-device.txt').read_text())
    joint, candidates, trajectory_issues = trajectory(lines, calls, policies, run, device)
    issues += trajectory_issues
    stats, loss_issues = lifecycle.loss_evidence(folder)
    issues += loss_issues
    if not stats: issues.append('missing_trace_loss_evidence')
    session, outcomes = {}, []
    if (folder/'session.txt').is_file():
        try:
            session, outcomes = parse_session(folder/'session.txt')
            issues += reconcile_session(session, outcomes, joint, plan, run, len(expected))
        except (ValueError, KeyError, IndexError):
            issues.append('malformed_session_end')
    else: issues.append('missing_session_end')
    # Reuse the existing checker, including precise syscall returns and stable
    # snapshots. Observer events remain present but never seed fault mutation.
    manifest = dict(run_id=f'efi-{run}', boot_id=(folder/'boot-id.txt').read_text().strip(),
                    require_native_syscalls=True, require_single_writer=True,
                    intervals=[dict(name='target', start='target-start.json', end='target-end.json',
                                    snapshots=['target-before.json','target-after.json'], mode='single',
                                    dev=f'{os.major(device)},{os.minor(device)}')])
    (folder/'chain-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    checker = lifecycle.analyze(folder, folder/'checker')
    before = json.loads((folder/'target-before.json').read_text())
    after = json.loads((folder/'target-after.json').read_text())
    stable = all(sample['valid'] and sample['namespace_valid'] and sample['relations_valid'] for sample in (before, after))
    stable_mismatch = any(sample['valid'] and sample['namespace_valid'] and sample['relations_valid'] and
                          any(sample[k] for k in ('observed_mismatches','namespace_mismatched','relations_mismatches'))
                          for sample in (before, after))
    if not stable: issues.append('invalid_stable_snapshot')
    confirmed = bool(checker['classification_counts']['persistent_mismatch'] or
                     checker['classification_counts']['operation_exit_mismatch'] or
                     any(r['object_checks']['mismatch_count'] for r in checker['runs']))
    for blocker in checker['blockers']:
        if blocker.split(':')[-1] not in ('confirmed_mismatch', 'object_postcondition_or_relation_mismatch'):
            issues.append('checker:' + blocker)
    diagnostics = re.findall(r'^.*(?:BUG:|KASAN:|Oops:|WARNING:|possible circular locking).*$', (folder/'dmesg.txt').read_text(), re.M)
    if (folder/'dmesg-before.txt').exists():
        previous = set((folder/'dmesg-before.txt').read_text().splitlines())
        diagnostics = [line for line in diagnostics if line not in previous]
    exit_code = int((folder/'exit-code.txt').read_text())
    if exit_code: issues.append(f'program_exit:{exit_code}')
    signature_items = []
    for e in joint:
        if e['kind']=='formula': signature_items.append(['formula', e['call'], e['point'],e['formula']])
        elif e['phase']=='return': signature_items.append(['error', *identity(e), e['injected'], e['result_class']])
    signature = hashlib.sha256(json.dumps(signature_items,separators=(',',':')).encode()).hexdigest()
    report = dict(schema=1, run=run, complete=not issues, issues=issues,
                  formula_events=sum(e['kind']=='formula' for e in joint),
                  error_enters=sum(e['kind']=='error' and e['phase']=='enter' for e in joint),
                  candidates=candidates, plans=outcomes, session=session,
                  actual_faults=[{k:e[k] for k in KEYS} for e in joint
                                 if e['kind']=='error' and e['phase']=='enter' and e['injected']],
                  signature=signature, state_policy='continuous_mounted_filesystem',
                  stable_valid=stable, stable_mismatch=stable_mismatch,
                  checker_finding=confirmed,
                  kernel_diagnostics=diagnostics, checker_passed=checker['passed'],
                  checker_blockers=checker['blockers'],
                  checker_classifications=checker['classification_counts'],
                  fault_return_is_not_a_bug=True)
    mapping = {s['site_id']:s for s in json.loads((folder.parent.parent/'sites.json').read_text())['sites']}
    for event in joint:
        if event['kind'] == 'error':
            source = mapping[event['site_id']]
            event.update(source=source['source'], source_line=source['line'],
                         caller=source['caller'], callee=source['callee'])
    with (folder/'joint.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['trace_line', 'order', 'call', 'kind', 'phase',
                                'point', 'formula', 'site_id', 'occurrence', 'context', 'eligible',
                                'allowed', 'injected', 'result', 'result_class', 'source', 'source_line',
                                'caller', 'callee'], extrasaction='ignore')
        writer.writeheader(); writer.writerows(joint)
    (folder/'joint.jsonl').write_text(''.join(json.dumps(e,separators=(',',':'))+'\n' for e in joint))
    (folder/'feedback.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Re-analyze a saved EFI round and write joint.csv with source locations.')
    parser.add_argument('round', type=Path)
    args = parser.parse_args()
    folder = args.round.resolve()
    metadata = json.loads((folder.parent/'program.json').read_text())
    policies = json.loads((folder.parent.parent/'injection.json').read_text())['policies']
    run = json.loads((folder/'round-state.json').read_text())['run']
    result = analyze_round(folder, metadata, policies, load_plan(folder/'plan.json'), run)
    print(json.dumps({k:v for k,v in result.items() if k not in ('candidates', 'plans', 'actual_faults')}, indent=2))
    raise SystemExit(0 if result['complete'] and result['checker_passed'] and not result['kernel_diagnostics'] else 1)
