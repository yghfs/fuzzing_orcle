#!/usr/bin/env python3
"""Read retained campaign evidence; write only this comparison directory."""
from __future__ import annotations

import collections
import csv
import datetime
import gzip
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TAGS = ('lhze43mu', 'yk3_8nzt', 'dzq91z1h')
KINDS = ('regular', 'bigalloc', 'inline')


def read(path):
    with (gzip.open(path, 'rt') if path.suffix == '.gz' else path.open()) as stream:
        return json.load(stream)


def ratio(n, d):
    return round(n / d, 6) if d else None


def main():
    result, csv_rows = {}, []
    for tag in TAGS:
        path = ROOT / 'results' / ('campaign-' + tag)
        meta, report, points = [read(path / name) for name in
                                ('campaign.json', 'campaign-report.json', 'point-report.json')]
        with gzip.open(path / 'segment-summary.jsonl.gz', 'rt') as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
        fault = report['fault_injection']
        hits = [x for x in fault['hit_segments'] if x['operation'] != 'off']
        hit_ids = {x['segment_id'] for x in hits}
        resolved = [x for x in hits if x['operation'] != 'unresolved']
        candidate_ids = {x['segment_id'] for x in fault['candidates'] if 'segment_id' in x}
        start = meta.get('measurement_start_wall_ns') or rows[0]['start_wall_ns']
        d = dict(run_id=meta['run_id'], limits=meta['limits'],
                 started_local=datetime.datetime.fromtimestamp(meta['started_wall_ns']/1e9,
                     datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
                 collection_seconds=report['collection_elapsed_seconds'],
                 total_elapsed_seconds=report['elapsed_seconds'],
                 status=report['status'], raw_complete=report['raw_coverage_complete'],
                 coverage_complete=report['coverage_complete'],
                 counter_validation_passed=report['counter_validation_passed'],
                 kernel_fault=report['kernel_fault'], segments=len(rows),
                 coverage_complete_segments=report['complete_segments'],
                 native_verified=report['native_verified'], native_success=report['native_success'],
                 classification_counts=report['classification_counts'],
                 by_kind_classifications=report['by_kind_mode'],
                 reported_fault_hits=fault['hit_count'], filtered_fault_hits=len(hits),
                 resolved_fault_hits=len(resolved),
                 hit_operations=dict(collections.Counter(x['operation'] for x in hits)),
                 hit_errno=dict(collections.Counter(str(x.get('errno')) for x in hits)),
                 hit_admission=dict(collections.Counter(x.get('admission','missing') for x in hits)),
                 hit_attribution=dict(collections.Counter((x.get('attribution') or {}).get('status','missing') for x in hits)),
                 hit_syscalls=dict(collections.Counter(((x.get('attribution') or {}).get('native_envelope') or {}).get('name','absent') for x in hits)),
                 hit_call_indexes=dict(collections.Counter(str(((x.get('attribution') or {}).get('native_envelope') or {}).get('call_index','absent')) for x in hits)),
                 hook_errno_differs_from_syscall=sum(
                     isinstance(((x.get('attribution') or {}).get('native_envelope') or {}).get('raw_result'), int)
                     and ((x.get('attribution') or {}).get('native_envelope') or {})['raw_result'] != x.get('errno')
                     for x in hits),
                 signatures_hook_errno=len({(x['operation'],x.get('errno')) for x in resolved}),
                 signatures_kind_hook_errno=len({(x['kind'],x['operation'],x.get('errno')) for x in resolved}),
                 candidate_records=len(fault['candidates']), candidate_segments=len(candidate_ids),
                 candidate_sources=dict(collections.Counter(x['source'] for x in fault['candidates'])),
                 candidate_operations=dict(collections.Counter(x['operation'] for x in hits if x['segment_id'] in candidate_ids)),
                 unverified_records=fault['unverified_count'],
                 unverified_segments=len({x['segment_id'] for x in fault['unverified'] if 'segment_id' in x}),
                 source_attribution_issues=len(report['source_attribution_issues']),
                 event_sources=report['event_sources'], point_event_sources=points['event_sources'],
                 disk=report['disk'], concurrency=report['concurrency'],
                 quality={}, by_kind={}, hourly=[],
                 exploration={k:v for k,v in report['exploration'].items() if not isinstance(v,(dict,list))},
                 syscalls={k:{j:v for j,v in a.items() if j!='point_hits'}
                           for k,a in report['exploration']['syscall_stats'].items()},
                 kcov={k:v for k,v in report['kcov_feedback'].items() if k!='gaps'},
                 performance=read(path/'performance-report.json'),
                 issues=dict(collections.Counter(x.split(':')[1] if x.startswith('segment-') else x for x in report['issues'])))
        local_counts, reasons = collections.Counter(), collections.Counter()
        for point in points['points'].values():
            local = point['checks'].get('local',{})
            local_counts.update(local.get('counts',{})); reasons.update(local.get('reasons',{}))
        d['points'] = dict(observed_hits=points['observed_hits'], point_formula_pairs=len(points['points']),
                           incomplete_capture_hits=sum(x['incomplete_capture_hits'] for x in points['points'].values()),
                           local_counts=dict(local_counts), reasons=dict(reasons),
                           inconsistent_points={k:v['checks']['local']['counts']['inconsistent']
                               for k,v in points['points'].items() if v['checks']['local']['counts']['inconsistent']})
        quality, snapshot_counts, blocker_counts = collections.Counter(), collections.Counter(), collections.Counter()
        block_by_kind = {k:collections.Counter() for k in KINDS}
        snapshots_by_role = collections.defaultdict(collections.Counter)
        for row in rows:
            sid, kind = row['segment_id'], row['kind']
            folder = path/'segments'/f'segment-{sid:06d}'
            analysis_path=path/f'segment-{sid:06d}-analysis.txt'
            try:
                analysis=json.loads(analysis_path.read_text())
            except (OSError,ValueError):
                analysis={'passed':False,'blockers':['missing_or_unparseable_analysis_summary']}
            passed=bool(analysis.get('passed'))
            invalid_trace=any(b.endswith(':invalid_trace') for b in analysis.get('blockers',[]))
            group='hit' if sid in hit_ids else 'no_hit'
            quality['analysis_passed']+=passed
            quality[f'{group}_segments']+=1
            quality[f'{group}_analysis_failed']+=not passed
            quality['coverage_complete_but_analysis_failed']+=bool(row['coverage_complete']) and not passed
            quality['filesystem_resets']+=bool(row.get('filesystem_reset_completed'))
            quality['invalid_trace_segments']+=invalid_trace
            quality['reset_with_invalid_trace']+=bool(row.get('filesystem_reset_completed')) and invalid_trace
            quality['fault_tainted_workload_segments']+=bool(row.get('fault_tainted'))
            for b in set(analysis.get('blockers',[])):
                blocker_counts[b]+=1
                prefix,_,reason=b.partition(':')
                if prefix.removesuffix('-single') in block_by_kind:
                    block_by_kind[prefix.removesuffix('-single')][reason]+=1
            own_valid=True; other_valid=True
            for fs in KINDS:
                for phase in ('before','after'):
                    snap=read(folder/f'{fs}-{phase}.json')
                    valid=all(snap.get(k,False) for k in ('valid','namespace_valid','relations_valid'))
                    role='target' if fs==kind else 'other'
                    snapshots_by_role[role]['samples']+=1
                    snapshots_by_role[role]['all_valid']+=valid
                    if not valid:snapshot_counts[f'{role}:{snap.get("reason")}:{snap.get("namespace_reason")}']+=1
                    if fs==kind:own_valid=own_valid and valid
                    else:other_valid=other_valid and valid
            quality[f'{group}_target_snapshot_pair_invalid']+=not own_valid
            quality[f'{group}_other_snapshot_pair_invalid']+=not other_valid
            csv_rows.append(dict(campaign=tag,segment_id=sid,kind=kind,
                elapsed_seconds=max(0,(row['end_wall_ns']-start)/1e9),
                fault_hit=sid in hit_ids,analysis_passed=passed,
                coverage_complete=row['coverage_complete'],status=row['status'],
                target_snapshot_pair_valid=own_valid,other_snapshot_pairs_valid=other_valid,
                reset=bool(row.get('filesystem_reset_completed')),
                invalid_trace=invalid_trace,
                operation_exit_mismatch=row['classification_counts'].get('operation_exit_mismatch',0)))
        d['quality']=dict(quality)
        d['blockers']=dict(blocker_counts)
        d['blockers_by_filesystem']={k:dict(v) for k,v in block_by_kind.items()}
        d['snapshot_validity']={k:dict(v) for k,v in snapshots_by_role.items()}
        d['snapshot_invalid_reasons']=dict(snapshot_counts)
        cleanup_errors=collections.Counter()
        cleanup_samples=[]
        for candidate in fault['candidates']:
            if candidate['source']!='workload_exit':continue
            sid=candidate['segment_id']
            text=(path/'segments'/f'segment-{sid:06d}'/'workload.txt').read_text(errors='replace')
            errors=[line for line in text.splitlines() if line.startswith(
                ('rmdir tree:','unlink tree:','lstat tree:','open tree:'))]
            cleanup_errors.update(errors)
            cleanup_samples.append(dict(segment_id=sid,errors=errors,
                hook=next((h['operation'] for h in hits if h['segment_id']==sid),None)))
        d['cleanup_exit_errors']=dict(cleanup_errors)
        d['cleanup_exit_samples']=cleanup_samples
        for kind in KINDS:
            subset=[r for r in rows if r['kind']==kind]; ids={r['segment_id'] for r in subset}
            z=[x for x in csv_rows if x['campaign']==tag and x['kind']==kind]
            d['by_kind'][kind]=dict(segments=len(subset),hits=sum(x['kind']==kind for x in hits),
                coverage_complete=sum(r['coverage_complete'] for r in subset),
                analysis_passed=sum(r['analysis_passed'] for r in z),
                candidate_segments=len(candidate_ids&ids),
                resets=sum(bool(r.get('filesystem_reset_completed')) for r in subset))
        # Hourly checkpoints use only segments whose collected end precedes the deadline.
        max_hour=round(report['collection_elapsed_seconds']/3600)
        for hour in range(1,max_hour+1):
            subset=[r for r in rows if r['end_wall_ns']<=start+hour*3600*1e9]
            if not subset:continue
            sid=subset[-1]['segment_id'];ids={r['segment_id'] for r in subset}
            manifest=read(path/'segments'/f'segment-{sid:06d}'/'manifest.json')
            feedback=manifest['joint_feedback']['campaign_summary']
            hh=[x for x in hits if x['segment_id'] in ids]
            d['hourly'].append(dict(hour=hour,segments=len(subset),last_segment=sid,
                total_coverage=feedback['total_coverage'],ext4_pc_coverage=feedback['ext4_pc_coverage'],
                jbd2_pc_coverage=feedback['jbd2_pc_coverage'],target_pc_coverage=feedback['target_pc_coverage'],
                corpus=feedback['corpus_size'],semantic_features=feedback['total_semantic_features'],
                edges=feedback['total_edges'],hits=len(hh),hooks=len({x['operation'] for x in hh}),
                hook_errno_signatures=len({(x['operation'],x.get('errno')) for x in hh}),
                candidates=len([x for x in fault['candidates'] if x.get('segment_id') in ids]),
                candidate_segments=len(candidate_ids&ids),
                transitions=sum(r['exploration']['independent_transition_calls'] for r in subset)))
        episode_counts, episode_streams, exit_sources = collections.Counter(), collections.defaultdict(collections.Counter),collections.Counter()
        transient_durations=[]; abnormal=[]
        with gzip.open(path/'episodes.jsonl.gz','rt') as stream:
            for line in stream:
                e=json.loads(line); c=e['classification'];episode_counts[c]+=1
                episode_streams[c][e['stream'].split(':')[0]]+=1
                if c=='transient_mismatch' and isinstance(e.get('duration_ns'),int):
                    transient_durations.append(e['duration_ns'])
                if c!='transient_mismatch':
                    abnormal.append({k:e.get(k) for k in ('segment_id','kind','classification','stream','onset_point','peak_abs_gap','confidence')})
                    if c=='operation_exit_mismatch':exit_sources['hit' if e['segment_id'] in hit_ids else 'no_hit']+=1
        transient_durations.sort()
        d['episodes']=dict(counts=dict(episode_counts),streams={k:dict(v) for k,v in episode_streams.items()},
            exit_mismatch_source=dict(exit_sources),
            transient_duration_us={str(q):transient_durations[min(len(transient_durations)-1,int((len(transient_durations)-1)*q))]/1000
                                   for q in (.5,.95,.99)} if transient_durations else {},abnormal=abnormal)
        d['rates']=dict(segments_per_hour=ratio(len(rows)*3600,d['collection_seconds']),
            verified_syscalls_per_wall_second=ratio(d['native_verified'],d['collection_seconds']),
            transitions_per_wall_second=ratio(d['exploration']['independent_transition_calls'],d['collection_seconds']),
            hits_per_segment=ratio(len(hits),len(rows)),hits_per_hour=ratio(len(hits)*3600,d['collection_seconds']),
            candidate_records_per_hour=ratio(d['candidate_records']*3600,d['collection_seconds']),
            candidate_segments_per_hit=ratio(d['candidate_segments'],len(hits)),
            lifecycle_pass_rate=ratio(quality['analysis_passed'],len(rows)),
            transition_density=ratio(d['exploration']['independent_transition_calls'],d['native_verified']),
            native_success_rate=ratio(d['native_success'],d['native_verified']))
        assert len(rows)==report['segments']
        assert sum(local_counts.values())==points['observed_hits']
        assert all(episode_counts[k]==v for k,v in report['classification_counts'].items())
        assert sum(x['coverage_complete'] for x in rows)==report['complete_segments']
        result[tag]=d
        print(tag,json.dumps({k:d[k] for k in ('segments','quality','by_kind','rates','hit_attribution','snapshot_validity')},ensure_ascii=False))
    a,b=result['yk3_8nzt'],result['dzq91z1h']
    result['comparison']=dict(common_hooks=sorted(set(a['hit_operations'])&set(b['hit_operations'])),
        only_yk=sorted(set(a['hit_operations'])-set(b['hit_operations'])),
        only_dzq=sorted(set(b['hit_operations'])-set(a['hit_operations'])),
        hooks_union=len(set(a['hit_operations'])|set(b['hit_operations'])))
    repro=[]
    for f in sorted((ROOT/'results'/'repro-yk3-20261030').glob('*/fault-evidence.json')):
        x=read(f)
        repro.append(dict(run=f.parent.name,**{k:x.get(k) for k in (
            'classification','passed','hit_count','finding_reproduced','persistent_postcondition_mismatch',
            'pre_recovery_fsck_clean','pre_recovery_disk_verified','recovery_verified','syscall_result','syscall_errno')}))
    result['repro_yk']=dict(runs=len(repro),
        persistent_mismatch=sum(bool(x['persistent_postcondition_mismatch']) for x in repro),
        classifications=dict(collections.Counter(x['classification'] for x in repro)),rows=repro)
    (HERE/'metrics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    with (HERE/'segments.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(csv_rows[0]));writer.writeheader();writer.writerows(csv_rows)
    print('comparison',result['comparison'])


if __name__=='__main__':
    main()
