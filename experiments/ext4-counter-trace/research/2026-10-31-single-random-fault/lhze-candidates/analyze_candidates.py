#!/usr/bin/env python3
"""Read-only reanalysis of retained lhze evidence; write derived ledgers here."""
from __future__ import annotations

import collections
import csv
from decimal import Decimal
import gzip
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
OUT = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[5]
BASE = ROOT / 'experiments/ext4-counter-trace/results/campaign-lhze43mu'
KINDS = ('regular', 'bigalloc', 'inline')
CANONICAL_CATALOG = ROOT / 'experiments/ext4-counter-trace/fault-errno-catalog.json'


def read(path):
    with (gzip.open(path, 'rt') if path.suffix == '.gz' else path.open()) as stream:
        return json.load(stream)


def save(name, obj):
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def table(name, rows):
    with (OUT / name).open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def fields(line):
    return dict(re.findall(r'\b([a-z_]+)=([^\s]+)', line))


def timestamp(line):
    match = re.search(r'\s(\d+\.\d+):\s', line)
    return int(Decimal(match[1]) * 10**9) if match else None


def main():
    report = read(BASE / 'campaign-report.json')
    original = report['fault_injection']
    canonical = read(CANONICAL_CATALOG)
    canonical_errnos = {
        hook: {-int(item['code']) for item in spec.get('native_errnos', [])}
        for hook, spec in canonical['hooks'].items()
    }
    segments = sorted((BASE / 'segments').glob('segment-*'))
    snapshots, manifests = [], {}
    for folder in segments:
        if not (folder / 'manifest.json').exists():
            continue
        manifest = read(folder / 'manifest.json')
        segment = manifest['segment_id']
        manifests[segment] = manifest
        for kind in KINDS:
            for phase in ('before', 'after'):
                p = folder / f'{kind}-{phase}.json'
                if not p.exists():
                    continue
                snap = read(p)
                snapshots.append(dict(segment=segment, workload_kind=manifest['kind'],
                    kind=kind, phase=phase, valid=snap.get('valid'),
                    namespace_valid=snap.get('namespace_valid'),
                    namespace_reason=snap.get('namespace_reason'),
                    namespace_compared=snap.get('namespace_compared'),
                    namespace_mismatched=snap.get('namespace_mismatched'),
                    observed_mismatches=snap.get('observed_mismatches'),
                    relations_valid=snap.get('relations_valid'),
                    relations_mismatches=snap.get('relations_mismatches'),
                    free_inodes=snap.get('free_inodes'), dirs=snap.get('dirs'),
                    free_clusters=snap.get('free_clusters')))
    table('snapshot-timeline.csv', snapshots)
    snap_index = {(s['segment'], s['kind'], s['phase']): s for s in snapshots}
    timeline = {}
    for kind in KINDS:
        relevant = [s for s in snapshots if s['kind'] == kind]
        bad = [s for s in relevant if s['namespace_valid'] is False]
        first_bad = bad[0] if bad else None
        changes = []
        for segment, manifest in manifests.items():
            before = snap_index.get((segment, kind, 'before'))
            after = snap_index.get((segment, kind, 'after'))
            if before and after and any(before[k] != after[k] for k in
                    ('free_inodes', 'dirs', 'namespace_valid')):
                changes.append(dict(segment=segment, workload_kind=manifest['kind'],
                    before={k: before[k] for k in ('free_inodes', 'dirs', 'namespace_valid')},
                    after={k: after[k] for k in ('free_inodes', 'dirs', 'namespace_valid')}))
        timeline[kind] = dict(snapshots=len(relevant), namespace_bad=len(bad),
            first_bad=first_bad,
            recovered_after_first_bad=any(s['namespace_valid'] is True for s in
                relevant[relevant.index(first_bad) + 1:]) if first_bad else False,
            changes=changes)

    hits, snippets = [], []
    for segment, manifest in manifests.items():
        policy = manifest.get('fault_injection') or {}
        status = policy.get('status') or {}
        if not policy.get('hit') or not status.get('enabled') or status.get('operation') == 'off':
            continue
        folder = BASE / 'segments' / f'segment-{segment:06d}'
        with gzip.open(folder / 'trace.txt.gz', 'rt', errors='replace') as stream:
            lines = stream.readlines()
        events = [(n + 1, line, fields(line)) for n, line in enumerate(lines)
                  if 'ext4_fault_inject:' in line and fields(line).get('injection') == '1'
                  and fields(line).get('pid') == str(status['target_pid'])
                  and fields(line).get('target_pid') == str(status['target_pid'])]
        assert len(events) == 1, (segment, len(events))
        line_no, line, event = events[0]
        lifecycle = read(folder / 'analysis/lifecycle/report.json.gz')
        event_ns = timestamp(line)
        calls = [c for c in lifecycle['native_syscalls']
                 if c.get('pid') == int(event['pid']) and c.get('enter_ns') is not None
                 and c.get('exit_ns') is not None and c['enter_ns'] <= event_ns <= c['exit_ns']]
        assert len(calls) <= 1, segment
        call = calls[0] if calls else {}
        before = snap_index[segment, manifest['kind'], 'before']
        after = snap_index[segment, manifest['kind'], 'after']
        operation = event['operation']
        choices = policy['supported_errno_codes'][operation]
        allowed_names = policy['errno_by_operation'][operation]
        choices = [choices[policy['supported_errnos'][operation].index(n)] for n in allowed_names]
        idx = int(event['errno_choice_index'])
        verified = (int(event['errno']) == -choices[idx]
                    and int(event['errno_choice_draw']) % len(choices) == idx
                    and int(event['random_seed']) == policy['seed']
                    and int(event['random_draw']) < policy['probability_ppm']
                    and status['errno_operation'] == operation
                    and status['errno'] == int(event['errno'])
                    and status['errno_choice_index'] == idx)
        target = int(event['block']) if operation != 'mballoc_mark' else None
        exits = [(n+1, x, fields(x)) for n,x in enumerate(lines)
                 if 'point=INODE.EVICT.TARGET.EXIT ' in x
                 and fields(x).get('ino') == str(target)
                 and timestamp(x) is not None and timestamp(x) >= event_ns]
        target_exit = exits[0] if exits and operation == 'inode_evict_final_dirty' else None
        row = dict(segment=segment, kind=manifest['kind'], operation=operation,
            errno=int(event['errno']), target_inode=target, trace_line=line_no,
            timestamp_ns=event_ns, pid=int(event['pid']), raw_errno_choice_verified=verified,
            canonical_native_errno=(int(event['errno']) in canonical_errnos.get(operation, set())),
            manifest_event_missing=policy.get('event') is None,
            syscall=call.get('name', 'outside_generated_syscall_envelope'),
            call_index=call.get('call_index'), syscall_result=call.get('raw_result'),
            user_errno=call.get('user_errno'), syscall_verified=call.get('verified'),
            namespace_before=before['namespace_valid'], namespace_after=after['namespace_valid'],
            free_inodes_before=before['free_inodes'], free_inodes_after=after['free_inodes'],
            dirs_before=before['dirs'], dirs_after=after['dirs'],
            free_clusters_before=before['free_clusters'],free_clusters_after=after['free_clusters'],
            target_exit_line=target_exit[0] if target_exit else None,
            target_exit_actual=int(target_exit[2]['actual']) if target_exit else None,
            target_exit_expected=int(target_exit[2]['expected']) if target_exit else None,
            target_exit_comparison_valid=target_exit[2]['comparison_valid'] if target_exit else None,
            workload_exit=manifest.get('guest_exit'))
        hits.append(row)
        if operation == 'inode_evict_final_dirty':
            op = target_exit[2]['op']
            scoped = [(n+1,x) for n,x in enumerate(lines)
                      if fields(x).get('op') == op and 'ext4_counter' in x
                      and fields(x).get('dev') == event['dev']]
            bitmap = [(n+1,x.strip()) for n,x in enumerate(lines)
                      if 'point=INODE.FREE.BITMAP ' in x and fields(x).get('ino') == str(target)
                      and fields(x).get('dev') == event['dev']]
            snippet = dict(segment=segment, kind=manifest['kind'], target_inode=target,
                event=dict(line=line_no, text=line.strip()),
                object_findings=lifecycle['object_checks']['findings'],
                eviction_scope=[dict(line=n,text=x.strip()) for n,x in scoped],
                target_free_bitmap_events_in_segment=[dict(line=n,text=x) for n,x in bitmap],
                target_free_bitmap_events_after_hit=[dict(line=n,text=x) for n,x in bitmap
                    if timestamp(x) >= event_ns],
                nearby_trace=[dict(line=n+1,text=lines[n].strip()) for n in
                    range(max(0,line_no-8),min(len(lines),line_no+8))])
            if not call:
                snippet['last_generated_call'] = lifecycle['native_syscalls'][-1]
                snippet['raw_exit_syscalls'] = [dict(line=n+1,text=x.strip()) for n,x in enumerate(lines)
                    if ('sys_enter: NR 231 ' in x or 'sys_enter: NR 60 ' in x)
                    and re.search(r'-'+event['pid']+r'\s',x)
                    and timestamp(x) is not None and 0 <= event_ns-timestamp(x) < 10**9]
            snippets.append(snippet)
    table('real-hits.csv', hits)
    save('eviction-evidence.json', snippets)

    ledger = []
    for ordinal,c in enumerate(original['candidates'], 1):
        segment = c.get('segment_id')
        snap = snap_index.get((segment,c.get('kind'),c.get('phase')))
        policy = manifests.get(segment,{}).get('fault_injection',{})
        state = policy.get('status',{})
        before = snap_index.get((segment,c['kind'],'before'),{})
        classification = ('offline_confirmation' if c['source']=='offline_disk' else
            'first_namespace_invalid' if before.get('namespace_valid') is True else
            'stale_off_state_repeat' if state.get('operation')=='off' else 'preexisting_namespace_invalid')
        ledger.append(dict(ordinal=ordinal,source=c['source'],segment=segment,kind=c['kind'],
            phase=c.get('phase'),classification=classification,
            status_enabled=state.get('enabled'),status_operation=state.get('operation'),
            namespace_reason=snap.get('namespace_reason') if snap else None,
            namespace_compared=snap.get('namespace_compared') if snap else None,
            namespace_mismatched=snap.get('namespace_mismatched') if snap else None,
            observed_mismatches=snap.get('observed_mismatches') if snap else None,
            relations_mismatches=snap.get('relations_mismatches') if snap else None,
            evidence=c.get('evidence') or c.get('disk_evidence')))
    table('candidate-ledger.csv',ledger)

    spec = importlib.util.spec_from_file_location('retained_disk_checker', BASE/'evidence/scripts/check-disk.py')
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    disks={}
    for kind in KINDS:
        image = checker.Image(BASE/f'{kind}.raw')
        allocated={};refs=collections.defaultdict(list);directories=[]
        for group in range(image.groups):
            info=image.group_info(group);bits=image.read_block(info['inode_bitmap'])
            for bit in range(image.valid_inodes(group)):
                if not bits[bit//8] & (1 << (bit%8)):
                    continue
                ino=group*image.inodes_per_group+bit+1
                raw=image.inode_raw(group,bit)
                allocated[ino]=dict(ino=ino,mode=image.u16(0,raw),links=image.u16(26,raw),
                    dtime=image.u32(20,raw),size=image.u32(4,raw),flags=image.u32(32,raw),
                    blockcount=image.u32(28,raw))
                if image.u16(0,raw)&0xf000 == 0x4000:
                    entries=image.directory_refs(ino,raw)
                    directories.append(dict(ino=ino,links=image.u16(26,raw),entries=entries))
                    for target,name,typ in entries:
                        refs[target].append(dict(directory=ino,name=name,file_type=typ))
        zero_links=[dict(x,refs=refs[ino]) for ino,x in allocated.items()
                    if x['links']==0 and ino>=image.first_ino]
        disks[kind]=dict(allocated_zero_link_inodes=zero_links,directories=directories,
                        original_checker=read(BASE/f'{kind}-disk.json'))
        image.fp.close()
        for inode in zero_links:
            for command in ('stat','testi'):
                result=subprocess.run(['debugfs','-R',f'{command} <{inode["ino"]}>',str(BASE/f'{kind}.raw')],
                    capture_output=True,text=True,check=True)
                (OUT/f'debugfs-{kind}-{inode["ino"]}-{command}.txt').write_text(result.stdout+result.stderr)
    save('offline-inodes.json',disks)
    summary=dict(original_hit_count=original['hit_count'],real_hit_count=len(hits),
        real_operations=dict(collections.Counter(h['operation'] for h in hits)),
        raw_errno_choices_verified=sum(h['raw_errno_choice_verified'] for h in hits),
        canonical_native_errno_by_operation=dict(collections.Counter(
            h['operation'] for h in hits if h['canonical_native_errno'])),
        recovered_manifest_events=[h['segment'] for h in hits if h['manifest_event_missing']],
        candidate_count=len(ledger),classifications=dict(collections.Counter(x['classification'] for x in ledger)),
        snapshot_count=len(snapshots),timeline=timeline,
        eviction_instances=[h for h in hits if h['operation']=='inode_evict_final_dirty'])
    assert len(ledger)==44 and len(hits)==20
    assert sum(h['operation']=='inode_evict_final_dirty' for h in hits)==6
    assert summary['raw_errno_choices_verified']==20
    assert all(not s['target_free_bitmap_events_after_hit'] for s in snippets)
    assert all(len(s['object_findings']) == 1 and s['object_findings'][0]['ino'] == s['target_inode']
               for s in snippets)
    assert len(next(s for s in snippets if s['segment'] == 53)['raw_exit_syscalls']) == 1
    assert all(h['free_clusters_before'] == h['free_clusters_after']
               and h['free_inodes_before'] == h['free_inodes_after']
               and h['dirs_before'] == h['dirs_after']
               for h in hits if h['operation'] != 'inode_evict_final_dirty')
    assert summary['classifications']=={'first_namespace_invalid':2,'stale_off_state_repeat':22,
                                       'preexisting_namespace_invalid':18,'offline_confirmation':2}
    for kind in ('inline','bigalloc'):
        assert sorted(h['target_inode'] for h in hits if h['kind']==kind
            and h['operation']=='inode_evict_final_dirty') == sorted(
                x['ino'] for x in disks[kind]['allocated_zero_link_inodes'])
    save('summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
