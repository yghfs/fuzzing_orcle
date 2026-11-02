#!/usr/bin/env python3
"""Per-hit denominators, direct versus derived checks, and carry-anchor deduplication."""
import csv
import gzip
import json
import pathlib
import sys
import tempfile
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from point_report import PointSummary, probe_observation, campaign_points, lifecycle_points


def event(seq=1, point='BLOCK.DESCRIPTOR.LOCAL', formula='R-BDELTA', **kwargs):
    e=dict(dev='253,16',epoch=42,seq=seq,op=1,parent=0,point=point,formula=formula,
           ino=0,group=0,actual=10,expected=10,status=1,comparison_valid=1,update_delta=0)
    e.update(kwargs)
    return e


def row(e, confidence='precise_single_writer_recording_order', **kwargs):
    r=dict(event=e,run='regular-single',confidence=confidence,
           derived_counters={'R-BDELTA':{'independent':10,'descriptor':10,'global':10}},
           derived_groups={'R-BDELTA':{0:dict(independent=10,descriptor=10)}})
    r.update(kwargs)
    return r


class PointTests(unittest.TestCase):
    def test_syzkaller_call_coordinates_survive_hit_export(self):
        hit = probe_observation(row(event(), syscall_id='segment-000000-b1-100-3',
                                    program='b1-w0', call_index=2, syscall_name='openat'))
        self.assertEqual((hit['program'], hit['call_index'], hit['syscall_name']), ('b1-w0', 2, 'openat'))

    def test_one_hundred_hits_with_exact_outcome_partition(self):
        summary=PointSummary()
        for seq in range(100):
            e=event(seq+1,actual=10 if seq<73 else 11,status=1 if seq<73 else 2)
            summary.add(probe_observation(row(e)))
        p=summary.report()['points']['R-BDELTA:BLOCK.DESCRIPTOR.LOCAL']
        self.assertEqual(p['hits'],100)
        c=p['checks']['local']['counts']
        self.assertEqual(c['consistent'],73)
        self.assertEqual(c['inconsistent'],27)
        self.assertEqual(sum(c.values()),100)

    def test_local_consistency_does_not_hide_pending_global_gap(self):
        hit=probe_observation(row(event(),derived_counters={'R-BDELTA':{'independent':10,'descriptor':10,'global':11}}))
        by={c['measurement']:c for c in hit['checks']}
        self.assertEqual(by['local']['result'],'consistent')
        self.assertEqual(by['descriptor']['result'],'consistent')
        self.assertEqual(by['global_']['result'],'inconsistent')
        self.assertEqual(by['global_']['delta'],1)

    def test_raw_event_is_not_a_direct_match_or_raw_zero_comparison(self):
        hit=probe_observation(row(event(point='BLOCK.BITMAP.TRANSITION',actual=0,expected=0,status=0,comparison_valid=0)))
        self.assertEqual(hit['checks'][0]['result'],'raw_only')
        self.assertIsNone(hit['checks'][0]['delta'])
        self.assertEqual(hit['checks'][1]['result'],'consistent')

    def test_concurrent_zero_gap_is_ambiguous_not_proven_consistent(self):
        hit=probe_observation(row(event(),confidence='concurrent_or_ambiguous'))
        self.assertEqual(hit['checks'][0]['result'],'consistent')
        self.assertTrue(all(c['result']=='ambiguous' for c in hit['checks'][1:]))
        self.assertTrue(all(c['observed_relation']=='equal' for c in hit['checks'][1:]))

    def test_loss_and_unanchored_events_are_unverified(self):
        for r in (row(event(),confidence='unsupported_or_unverified'),
                  row(event(),derived_counters={},derived_groups={})):
            checks=probe_observation(r)['checks']
            self.assertTrue(all(c['result']=='unverified' for c in checks[1:]))
        hit=probe_observation(row(event()),{'trajectory_valid':False})
        self.assertTrue(all(c['result']=='unverified' for c in hit['checks'][1:]))

    def test_namespace_is_specific_to_event_inode(self):
        r=row(event(formula='R-LINK',point='NAMESPACE.REF.ADD',ino=12,status=0,comparison_valid=0),
              derived_inode={'nlink':2,'refs':1})
        c=probe_observation(r)['checks'][-1]
        self.assertEqual((c['measurement'],c['actual'],c['expected'],c['delta']),('namespace',2,1,1))
        r['derived_inode']['unsupported']='nlink_before_discontinuity'
        self.assertEqual(probe_observation(r)['checks'][-1]['result'],'unverified')

    def test_invalid_comparison_flag_and_status_inconsistency(self):
        for e in (event(comparison_valid=0),event(actual=11,status=1)):
            self.assertEqual(probe_observation(row(e))['checks'][0]['result'],'unverified')

    def test_missing_derived_value_is_never_a_consistent_or_inconsistent_check(self):
        for confidence in ('precise_single_writer_recording_order','concurrent_or_ambiguous'):
            r=row(event(),confidence=confidence,
                  derived_counters={'R-BDELTA':{'independent':10,'descriptor':10}})
            c={c['measurement']:c for c in probe_observation(r)['checks']}['global_']
            self.assertEqual(c['result'],'unverified')
            self.assertEqual(c['reason'],'missing_comparable_values')
            self.assertIsNone(c['delta'])

    def test_formula_and_mirror_mode_denominators_stay_separate(self):
        summary=PointSummary()
        for formula in ('R-IDELTA','R-UDELTA'):
            for mode in ('single','concurrent'):
                summary.add(probe_observation(row(event(point='INODE.ALLOC.GLOBAL',formula=formula)),
                                              {'kind':'regular','mode':mode}))
        points=summary.report()['points']
        self.assertEqual(len(points),2)
        self.assertTrue(all(p['hits']==2 for p in points.values()))
        self.assertTrue(all(p['dimensions']['regular/single']['hits']==1 for p in points.values()))

    def test_ep_onset_counts_are_distinct_from_hit_counts(self):
        summary=PointSummary()
        summary.add(probe_observation(row(event())))
        for stream in ('R-BDELTA:descriptor-independent','R-BDELTA:global-independent'):
            summary.onset(dict(stream=stream,onset_point='BLOCK.DESCRIPTOR.LOCAL',classification='transient_mismatch',
                               recovery_point='BLOCK.ALLOC.GLOBAL',peak_abs_gap=3),'R-BDELTA')
        r=summary.report()
        self.assertEqual(r['observed_hits'],1)
        self.assertEqual(r['onset_points']['R-BDELTA:BLOCK.DESCRIPTOR.LOCAL']['counts']['transient_mismatch'],2)

    def test_raw_hit_join_counts_carry_once_and_keeps_unmodeled_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=pathlib.Path(tmp)/'run';output=pathlib.Path(tmp)/'out'
            run.mkdir()
            (run/'campaign.json').write_text(json.dumps(dict(run_id='test',boot_id='b',status='complete',
                                                           devices={'regular':'253,16'})))
            events=[event(1),event(2),event(3)]
            for index,owned in enumerate((events[:2],events[2:])):
                f=run/'segments'/f'segment-{index:06d}';(f/'analysis/lifecycle').mkdir(parents=True)
                m=dict(segment_id=index,status='complete',mode='single',trace_file='trace.txt')
                (f/'manifest.json').write_text(json.dumps(m))
                (f/'trace.txt').write_text(''.join(
                    'worker-100 [000] .... 1.000001: ext4_counter_event: '+ ' '.join(f'{k}={v}' for k,v in e.items())+'\n' for e in owned))
                rows=[row(events[0])] if index==0 else [row(events[0]),row(events[1])]
                (f/'analysis/lifecycle/timeline.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
                report={'runs':[dict(name='regular-single',dev='253,16',epoch=42,trajectory_integrity=True,snapshots=[],episodes=[])]}
                (f/'analysis/lifecycle/report.json').write_text(json.dumps(report))
            report=campaign_points(run,output)
            self.assertEqual(report['observed_hits'],3)
            p=report['points']['R-BDELTA:BLOCK.DESCRIPTOR.LOCAL']
            self.assertEqual(p['checks']['local']['counts']['consistent'],3)
            self.assertEqual(p['checks']['descriptor']['counts']['consistent'],2)
            self.assertEqual(p['checks']['descriptor']['counts']['unverified'],1)
            with gzip.open(output/'point-hits.jsonl.gz','rt') as stream:
                hits=[json.loads(line) for line in stream]
            self.assertEqual([h['seq'] for h in hits],[1,2,3])
            self.assertEqual(hits[1]['analysis_segment'],1)
            self.assertEqual(hits[1]['raw_trace'],'segments/segment-000000/trace.txt')
            self.assertEqual(hits[1]['raw_line'],2)
            self.assertEqual(hits[2]['raw_trace'],'segments/segment-000001/trace.txt')
            with gzip.open(output/'point-hits.csv.gz','rt') as stream:
                self.assertEqual(len(list(csv.DictReader(stream))),12)
            self.assertTrue((output/'point-report.html').is_file())


if __name__=='__main__': unittest.main()
