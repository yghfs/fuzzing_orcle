#!/usr/bin/env python3
"""Semantic lifecycle tests, including zero-net transients and invalid evidence."""
import json
import os
import pathlib
import sys
import tempfile
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from trace_lifecycle import (Lifecycle, analyze, correlate_syscalls, parse_trace,
                             write_coverage, scope_tree, attribute_sources, exploration_metrics)


class Fixture:
    def __init__(self):
        self.events = []
        self.snapshots = []
        self.op = 0
    def e(self, point, formula, **kw):
        seq = len(self.events) + 1
        e = dict(seq=seq, line=seq, timestamp_ns=seq*1000, cpu=0, pid=100, comm='worker',
                 dev='253,16', epoch=42, op=self.op, parent=0, ino=0, group=0, tid=0,
                 actual=0, expected=0, delta=0, update_delta=0, status=0, comparison_valid=0,
                 schema=2, step=0, generation=0, is_dir=0, ref_delta=0, nlink_before=0,
                 nlink_after=0, result=0, point=point, formula=formula)
        e.update(kw)
        self.events.append(e)
        return e
    def begin(self, point, op, parent=0, pid=100):
        self.op = op
        self.e(point, 'BEGIN', parent=parent, pid=pid)
    def end(self, point, op=None, parent=0, pid=100, result=0):
        self.e(point, 'END', op=self.op if op is None else op, parent=parent, pid=pid, result=result)
    def snapshot(self, op, b=(100,100,100), inode=(20,20,20), dirs=(2,2,2), links=(), pid=100):
        self.begin('SNAPSHOT.MEMORY.STABLE', op, pid=pid)
        for group_f,sum_f,vals in [('R-BG','R-BSUM',b),('R-IG','R-ISUM',inode),('R-UG','R-USUM',dirs)]:
            independent, descriptor, global_ = vals
            self.e('SNAPSHOT.MEMORY.STABLE',group_f, actual=descriptor,expected=independent,
                   status=1 if descriptor==independent else 2, comparison_valid=1, pid=pid)
            self.e('SNAPSHOT.MEMORY.STABLE',sum_f, actual=global_,expected=descriptor,
                   status=1 if global_==descriptor else 2, comparison_valid=1,
                   group=4294967295, pid=pid)
        for ino, actual, expected in links:
            self.e('SNAPSHOT.MEMORY.STABLE','R-LINK',ino=ino,actual=actual,expected=expected,
                   status=1 if actual==expected else 2,comparison_valid=1, pid=pid)
        self.end('SNAPSHOT.MEMORY.STABLE', pid=pid)
        sample = dict(snapshot_id=op, valid=True, namespace_valid=True, groups=1,
                      namespace_compared=len(links), observed_mismatches=0,
                      free_clusters=b[2], free_inodes=inode[2], dirs=dirs[2])
        self.snapshots.append((f'run-snapshot{op}.json',sample))
    def transition(self, op=10, recover=True, nested=False):
        self.begin('BLOCK.ALLOC',op)
        if nested: self.begin('BLOCK.MARK_CONTEXT',op+1,parent=op)
        self.e('BLOCK.BITMAP.TRANSITION','R-BDELTA',update_delta=-3,parent=op if nested else 0)
        self.e('BLOCK.DESCRIPTOR.LOCAL','R-BDELTA',update_delta=-3,actual=97,expected=97,status=1,
               comparison_valid=1,parent=op if nested else 0)
        if nested:
            self.end('BLOCK.MARK_CONTEXT',parent=op)
            self.op=op
        if recover: self.e('BLOCK.ALLOC.GLOBAL','R-BDELTA',update_delta=-3)
        self.end('BLOCK.ALLOC')
    def run(self, issues=()):
        obj = Lifecycle('run',self.events,self.snapshots,issues,[])
        report = obj.run()
        return obj, report


class LifecycleTests(unittest.TestCase):
    def test_net_zero_transient_has_onset_peak_and_recovery(self):
        f=Fixture();f.snapshot(1);f.transition();f.snapshot(2,b=(97,97,97))
        obj,r=f.run()
        episodes=[e for e in r['episodes'] if e['stream']=='R-BDELTA:global-independent']
        self.assertEqual(len(episodes),1)
        e=episodes[0]
        self.assertEqual(e['classification'],'transient_mismatch')
        self.assertEqual(e['peak_abs_gap'],3)
        self.assertGreater(e['recovered_seq'],e['onset_seq'])
        self.assertFalse(any(r['final_derived_gaps'].values()))
        self.assertEqual(obj.timeline[-1]['derived_counters']['R-BDELTA']['global'],97)
        at_bitmap=next(row for row in obj.timeline if row['event']['point']=='BLOCK.BITMAP.TRANSITION')
        self.assertEqual(at_bitmap['derived_groups']['R-BDELTA'][0],
                         {'independent':97,'descriptor':100})

    def test_operation_exit_then_valid_stable_gap_is_persistent(self):
        f=Fixture();f.snapshot(1);f.transition(recover=False);f.snapshot(2,b=(97,97,100))
        _,r=f.run()
        e=next(e for e in r['episodes'] if e['stream']=='R-BDELTA:global-independent')
        self.assertEqual(e['classification'],'persistent_mismatch')
        self.assertTrue(e['exit_evidence'])
        self.assertEqual(e['stable_evidence'][-1]['gap'],3)

    def test_elapsed_time_or_eof_does_not_prove_persistence(self):
        f=Fixture();f.snapshot(1);f.transition(recover=False)
        _,r=f.run()
        e=next(e for e in r['episodes'] if e['stream']=='R-BDELTA:global-independent')
        self.assertEqual(e['classification'],'operation_exit_mismatch')
        self.assertFalse(e['stable_evidence'])

    def test_nested_helper_gap_waits_for_parent(self):
        f=Fixture();f.snapshot(1);f.transition(nested=True);f.snapshot(2,b=(97,97,97))
        _,r=f.run()
        inner=next(b for b in r['boundaries'] if b['point']=='BLOCK.MARK_CONTEXT')
        self.assertEqual(inner['classification'],'pending_parent_publication')
        self.assertFalse(inner['publication_boundary'])
        self.assertFalse(r['classification_counts']['operation_exit_mismatch'])

    def test_applied_changes_survive_later_error(self):
        f=Fixture();f.snapshot(1,links=[(12,1,1)])
        f.begin('NAMESPACE.unlink',10)
        f.e('NAMESPACE.REF.DELETE','R-LINK',ino=12,step=16,ref_delta=-1)
        f.e('NAMESPACE.DIRENT.DELETE','R-LINK',ino=12,step=2,result=-5)
        f.end('NAMESPACE.unlink',result=-5)
        f.snapshot(2,links=[(12,1,0)])
        _,r=f.run()
        e=next(e for e in r['episodes'] if e['stream'].startswith('R-LINK:12:'))
        self.assertEqual(e['classification'],'persistent_mismatch')
        self.assertEqual(e['peak_gap'],1)
        self.assertEqual(e['exit_evidence'][0]['result'],-5)

    def test_concurrent_recording_is_ambiguous_even_when_net_recovers(self):
        f=Fixture();f.snapshot(1)
        f.begin('BLOCK.ALLOC',10)
        f.begin('BLOCK.ALLOC',20,pid=200)
        f.op=10
        f.e('BLOCK.BITMAP.TRANSITION','R-BDELTA',update_delta=-3)
        f.e('BLOCK.DESCRIPTOR.LOCAL','R-BDELTA',update_delta=-3)
        f.e('BLOCK.ALLOC.GLOBAL','R-BDELTA',update_delta=-3)
        f.end('BLOCK.ALLOC',op=10)
        f.end('BLOCK.ALLOC',op=20,pid=200)
        f.snapshot(2,b=(97,97,97))
        _,r=f.run()
        self.assertGreater(r['classification_counts']['concurrent_or_ambiguous'],0)
        self.assertEqual(r['classification_counts']['operation_exit_mismatch'],0)

    def test_shared_inode_interleaving_names_both_syscalls_and_taints_trajectory(self):
        f=Fixture();f.snapshot(1,links=[(12,1,1)])
        link_begin=len(f.events)+1
        f.begin('NAMESPACE.linkat',10,pid=100)
        unlink_begin=len(f.events)+1
        f.begin('NAMESPACE.unlinkat',20,pid=200)
        f.op=10
        f.e('NAMESPACE.REF.ADD','R-LINK',ino=12,dir_ino=42,step=16,ref_delta=1,pid=100)
        f.op=20
        f.e('NAMESPACE.NLINK.DEC','R-LINK',ino=12,dir_ino=42,step=4,
            nlink_before=2,nlink_after=1,pid=200)
        link_end=len(f.events)+1
        f.end('NAMESPACE.linkat',op=10,pid=100)
        unlink_end=len(f.events)+1
        f.end('NAMESPACE.unlinkat',op=20,pid=200)
        calls=[dict(id='100-link',pid=100,name='linkat',program='b1-w0',call_index=3,
                    verified=True,enter_line=link_begin-1,exit_line=link_end+1,
                    raw_result=0,errno=0),
               dict(id='200-unlink',pid=200,name='unlinkat',program='b1-w1',call_index=4,
                    verified=True,enter_line=unlink_begin-1,exit_line=unlink_end+1,
                    raw_result=0,errno=0)]
        f.snapshot(2,links=[(12,1,1)])
        engine=Lifecycle('run',f.events,f.snapshots,[],calls)
        report=engine.run()
        self.assertEqual(report['shared_object_interleavings'],1)
        interleaving=report['interleavings'][0]
        self.assertEqual(interleaving['shared_objects'],['directory:253:16:42','inode:253:16:12'])
        self.assertEqual({interleaving['left']['syscall_name'],
                          interleaving['right']['syscall_name']},{'linkat','unlinkat'})
        event=next(row for row in engine.timeline
                   if row['event']['point']=='NAMESPACE.REF.ADD')
        self.assertEqual(event['syscall_id'],'100-link')
        self.assertEqual(event['attribution'],'same_task_native_syscall_with_interference')
        self.assertEqual(event['overlapping_operation_ids'],[20])
        self.assertTrue(event['derived_inode']['tainted'])
        self.assertFalse(engine.links[12]['tainted'])
        self.assertIn('concurrent_shared_inode_trajectory',report['unverified_reasons'])

    def test_native_syscall_overlap_finds_shared_directory_when_ext4_scopes_serialize(self):
        f=Fixture();f.snapshot(1)
        f.begin('NAMESPACE.mkdir',10,pid=100)
        f.e('NAMESPACE.DIRENT.ADD','R-LINK',ino=51,dir_ino=42,step=16,ref_delta=1,pid=100)
        f.end('NAMESPACE.mkdir',op=10,pid=100)
        f.begin('NAMESPACE.mkdir',20,pid=200)
        f.e('NAMESPACE.DIRENT.ADD','R-LINK',ino=52,dir_ino=42,step=16,ref_delta=1,pid=200)
        f.end('NAMESPACE.mkdir',op=20,pid=200)
        for event,line in zip(f.events[-6:],(20,21,22,30,31,32)):
            event['line']=line
        calls=[
            dict(id='100-mkdir',pid=100,name='mkdirat',program='b1-w0',call_index=0,
                 verified=True,enter_line=19,exit_line=25,enter_ns=100,exit_ns=300,
                 raw_result=0,errno=0),
            dict(id='200-mkdir',pid=200,name='mkdirat',program='b1-w1',call_index=0,
                 verified=True,enter_line=25,exit_line=33,enter_ns=200,exit_ns=400,
                 raw_result=0,errno=0)]
        engine=Lifecycle('run',f.events,f.snapshots,[],calls)
        report=engine.run()
        self.assertEqual(len(report['scope_interleavings']),0)
        self.assertEqual(len(report['native_syscall_interleavings']),1)
        pair=report['interleavings'][0]
        self.assertEqual(pair['basis'],['native_syscall_interval'])
        self.assertEqual(pair['shared_objects'],['directory:253:16:42'])
        event=next(row for row in engine.timeline if row['event']['pid']==100 and
                   row['event']['point']=='NAMESPACE.DIRENT.ADD')
        self.assertEqual(event['overlapping_syscall_ids'],['200-mkdir'])
        self.assertTrue(event['shared_object_interference'])
        self.assertEqual(event['confidence'],'concurrent_or_ambiguous')

    def test_overlapping_operation_ids_can_contain_multiple_root_scopes(self):
        f=Fixture();f.snapshot(1)
        f.begin('BLOCK.ALLOC',10)
        f.e('BLOCK.BITMAP.TRANSITION','R-BDELTA',update_delta=-1)
        f.end('BLOCK.ALLOC',op=10)
        engine=Lifecycle('run',f.events,f.snapshots,[],[])
        engine.overlapping_roots[10].append({
            'operation': {'op':[20,[30,20]]}, 'shared_objects':[]})

        engine.run()

        event=next(row for row in engine.timeline
                   if row['event']['point']=='BLOCK.BITMAP.TRANSITION')
        self.assertEqual(event['overlapping_operation_ids'],[20,30])
        boundary=next(row for row in engine.operations
                      if row['point']=='BLOCK.ALLOC')
        self.assertEqual(boundary['overlapping_operation_ids'],[20,30])

    def test_native_syscall_overlap_uses_fd_inode_identity_without_inode_probe_payload(self):
        f=Fixture();f.snapshot(1)
        calls=[
            dict(id='100-fallocate',pid=100,name='fallocate',program='b1-w0',call_index=3,
                 verified=True,enter_line=10,exit_line=30,enter_ns=100,exit_ns=300,
                 raw_result=0,errno=0,object_ino=88,object_dev=os.makedev(253,16),object_is_dir=0),
            dict(id='200-fallocate',pid=200,name='fallocate',program='b1-w1',call_index=3,
                 verified=True,enter_line=11,exit_line=31,enter_ns=200,exit_ns=400,
                 raw_result=0,errno=0,object_ino=88,object_dev=os.makedev(253,16),object_is_dir=0)]
        engine=Lifecycle('run',f.events,f.snapshots,[],calls)
        report=engine.run()
        self.assertEqual(len(report['native_syscall_interleavings']),1)
        self.assertEqual(report['native_syscall_interleavings'][0]['shared_objects'],
                         ['inode:253:16:88'])

    def test_fd_stat_device_matches_trace_device_for_shared_directory(self):
        f=Fixture();f.snapshot(1)
        event=f.e('NAMESPACE.DIRENT.ADD','R-LINK',ino=89,dir_ino=42,step=16,
                  pid=100,dev='253,16')
        event['line']=20
        calls=[
            dict(id='100-mkdirat',pid=100,name='mkdirat',program='b1-w0',call_index=6,
                 verified=True,enter_line=10,exit_line=30,enter_ns=100,exit_ns=300,
                 raw_result=0,errno=0),
            dict(id='200-fsync',pid=200,name='fsync',program='b1-w1',call_index=8,
                 verified=True,enter_line=11,exit_line=31,enter_ns=200,exit_ns=400,
                 raw_result=0,errno=0,object_ino=42,object_dev=os.makedev(253,16),
                 object_is_dir=1)]
        engine=Lifecycle('run',f.events,f.snapshots,[],calls)
        report=engine.run()
        self.assertEqual(report['native_syscall_interleavings'][0]['shared_objects'],
                         ['directory:253:16:42'])

    def test_same_inode_number_on_different_devices_is_not_shared(self):
        f=Fixture();f.snapshot(1)
        calls=[
            dict(id='100-fallocate',pid=100,name='fallocate',program='b1-w0',call_index=3,
                 verified=True,enter_line=10,exit_line=30,enter_ns=100,exit_ns=300,
                 raw_result=0,errno=0,object_ino=88,object_dev=os.makedev(253,16),object_is_dir=0),
            dict(id='200-fallocate',pid=200,name='fallocate',program='b1-w1',call_index=3,
                 verified=True,enter_line=11,exit_line=31,enter_ns=200,exit_ns=400,
                 raw_result=0,errno=0,object_ino=88,object_dev=os.makedev(253,32),object_is_dir=0)]
        engine=Lifecycle('run',f.events,f.snapshots,[],calls)
        report=engine.run()
        self.assertEqual(len(report['native_syscall_interleavings']),1)
        self.assertEqual(report['native_syscall_interleavings'][0]['shared_objects'],[])

    def test_stable_snapshot_overlapped_by_writer_is_rejected_and_later_reanchors(self):
        f=Fixture();f.snapshot(1)
        f.begin('BLOCK.ALLOC',10,pid=200)
        f.snapshot(2,pid=100)
        f.end('BLOCK.ALLOC',op=10,pid=200)
        f.snapshot(3,pid=100)
        _,report=f.run()
        self.assertFalse(report['snapshots'][1]['quiescent'])
        self.assertFalse(report['snapshots'][1]['valid'])
        self.assertEqual(report['snapshots'][1]['overlapping_operations'][0]['pid'],200)
        self.assertTrue(report['snapshots'][2]['quiescent'])
        self.assertTrue(report['snapshots'][2]['valid'])

    def test_inode_reuse_has_distinct_lifetimes_and_duplicate_wrapper_is_not_delta(self):
        f=Fixture();f.snapshot(1)
        for op in (10,20):
            f.begin('NAMESPACE.create',op)
            f.e('NAMESPACE.INODE.BIRTH','R-LINK',ino=12,step=32,generation=op,nlink_after=1)
            f.e('NAMESPACE.REF.ADD','R-LINK',ino=12,step=16,ref_delta=1)
            f.e('NAMESPACE.NLINK.DEC','R-LINK',ino=12,step=4,nlink_before=1,nlink_after=0)
            f.e('NAMESPACE.NLINK.DEC.WRAPPER','R-LINK',ino=12,step=4,nlink_before=1,nlink_after=0)
            f.e('NAMESPACE.REF.DELETE','R-LINK',ino=12,step=16,ref_delta=-1)
            f.e('NAMESPACE.INODE.DEATH','R-LINK',ino=12,step=64)
            f.end('NAMESPACE.create')
        f.snapshot(2)
        _,r=f.run()
        lives={e['stream'] for e in r['episodes'] if e['stream'].startswith('R-LINK:12:')}
        self.assertEqual(len(lives),2)
        self.assertNotIn('nlink_before_discontinuity',r['unverified_reasons'])

    def test_loss_invalidates_trajectory_and_stable_claim(self):
        f=Fixture();f.snapshot(1);f.transition(recover=False);f.snapshot(2,b=(97,97,100))
        _,r=f.run(['lost_event'])
        self.assertFalse(r['trajectory_integrity'])
        self.assertEqual(r['classification_counts']['persistent_mismatch'],0)
        self.assertTrue(all(not s['valid'] for s in r['snapshots']))

    def test_group_cancellation_is_not_hidden_by_total(self):
        f=Fixture();f.snapshot(1)
        # Existing group model need not have a global gap to expose a group discrepancy.
        engine=Lifecycle('run',f.events,f.snapshots,[],[]);engine.run()
        engine.groups['R-BDELTA']={0:{'independent':40,'descriptor':41},1:{'independent':60,'descriptor':59}}
        gaps=engine.gaps()
        self.assertEqual(gaps['R-BDELTA:descriptor-independent'],0)
        self.assertEqual(gaps['R-BDELTA:group:0:descriptor-independent'],1)
        self.assertEqual(gaps['R-BDELTA:group:1:descriptor-independent'],-1)

    def test_native_exit_uses_kernel_return_independently_of_helper_result(self):
        f=Fixture();f.snapshot(1)
        begin_line=len(f.events)
        f.transition(recover=False)
        end_line=len(f.events)+1
        f.snapshot(2,b=(97,97,100))
        calls=[dict(id='100-1',pid=100,name='fallocate',nr=285,verified=True,
                    enter_line=begin_line,exit_line=end_line,raw_result=-28,errno=28)]
        engine=Lifecycle('run',f.events,f.snapshots,[],calls)
        r=engine.run()
        native=next(b for b in r['boundaries'] if b['kind']=='native_syscall_return')
        self.assertEqual(native['classification'],'operation_exit_mismatch')
        self.assertEqual(native['raw_result'],-28)
        self.assertEqual(native['errno'],28)
        self.assertEqual(next(b for b in r['boundaries'] if b.get('point')=='BLOCK.ALLOC')['result'],0)
        episode=next(e for e in r['episodes'] if e['stream']=='R-BDELTA:global-independent')
        self.assertEqual(episode['classification'],'persistent_mismatch')
        self.assertTrue(any(x['kind']=='native_syscall_return' for x in episode['exit_evidence']))

    def test_background_no_scope_is_explicitly_ambiguous(self):
        f=Fixture();f.snapshot(1)
        f.op=0
        f.e('BLOCK.BITMAP.TRANSITION','R-BDELTA',update_delta=-3)
        f.e('BLOCK.DESCRIPTOR.LOCAL','R-BDELTA',update_delta=-3)
        f.e('BLOCK.ALLOC.GLOBAL','R-BDELTA',update_delta=-3)
        f.snapshot(2,b=(97,97,97))
        _,r=f.run()
        self.assertTrue(r['classification_counts']['concurrent_or_ambiguous'])
        self.assertFalse(r['classification_counts']['operation_exit_mismatch'])

    def test_event_outside_its_scope_invalidates_evidence(self):
        f=Fixture();f.snapshot(1);f.transition()
        f.e('BLOCK.ALLOC.GLOBAL','R-BDELTA',op=10,update_delta=0)
        _,r=f.run()
        self.assertFalse(r['trajectory_integrity'])
        self.assertTrue(any('event_outside_scope' in i for i in r['integrity_issues']))

    def test_trace_loss_duplicate_and_snapshot_payload_tampering(self):
        f=Fixture();f.snapshot(1);f.transition();f.snapshot(2,b=(97,97,97))
        def serialize(e):
            values = ' '.join(f'{k}={v}' for k,v in e.items() if k not in
                              {'line','timestamp_ns','cpu','pid','comm'})
            return f"worker-{e['pid']} [000] ..... {e['timestamp_ns']/1e9:.9f}: ext4_counter_event: {values}\n"
        with tempfile.TemporaryDirectory() as tmp:
            directory=pathlib.Path(tmp)
            original=''.join(serialize(e) for e in f.events)
            trace=directory/'trace.txt';trace.write_text(original)
            (directory/'run-start.json').write_text(json.dumps({'epoch':42,'seq':0,'active':0}))
            (directory/'run-end.json').write_text(json.dumps({'epoch':42,'seq':len(f.events),'active':0}))
            for name,sample in f.snapshots:
                (directory/name).write_text(json.dumps(sample))
            stats=directory/'cpu0-stats.txt'
            clean='overrun: 0\ncommit overrun: 0\ndropped events: 0\n'
            stats.write_text(clean)
            self.assertTrue(analyze(directory)['passed'])
            victim=next(e for e in f.events if e['point']=='BLOCK.BITMAP.TRANSITION')
            trace.write_text(original.replace(serialize(victim),''))
            self.assertFalse(analyze(directory)['passed'])
            trace.write_text(original+serialize(victim))
            self.assertFalse(analyze(directory)['passed'])
            trace.write_text(original);stats.write_text(clean.replace('overrun: 0','overrun: 1',1))
            self.assertFalse(analyze(directory)['passed'])
            stats.write_text(clean)
            name,sample=f.snapshots[-1];sample=dict(sample,free_clusters=98)
            (directory/name).write_text(json.dumps(sample))
            self.assertFalse(analyze(directory)['passed'])

    def test_native_syscall_result_and_same_nr_helper_writes_not_confused(self):
        def line(n,body): return dict(line=n,pid=100,cpu=0,comm='w',timestamp_ns=n*1000,body=body)
        lines=[line(1,'sys_enter: NR 1 (0,0)'),
               line(2,'tracing_mark_write: CT_SYSCALL phase=BEGIN id=100-1 nr=1 name=write executed_fd=3 object_ino=88 object_dev=253 object_is_dir=0'),
               line(3,'sys_exit: NR 1 = 80'), # begin marker write to fd 0
               line(4,'sys_enter: NR 1 (3,0)'),line(5,'sys_exit: NR 1 = -28'),
               line(6,'sys_enter: NR 1 (0,0)'),
               line(7,'tracing_mark_write: CT_SYSCALL phase=END id=100-1 nr=1 name=write ret=-1 errno=28 executed_fd=3 object_ino=88 object_dev=253 object_is_dir=0'),
               line(8,'sys_exit: NR 1 = 80')]
        calls,issues=correlate_syscalls(lines)
        self.assertFalse(issues);self.assertTrue(calls[0]['verified'])
        self.assertEqual(calls[0]['enter_line'],4);self.assertEqual(calls[0]['exit_line'],5)
        self.assertEqual(calls[0]['errno'],28)
        self.assertEqual((calls[0]['object_ino'],calls[0]['object_dev'],calls[0]['object_is_dir']),
                         (88,253,0))
        calls,_=correlate_syscalls([l for l in lines if l['line']!=5])
        self.assertFalse(calls[0]['verified'])

    def test_fd_argument_matching_disambiguates_actual_write_from_trace_marker_fd(self):
        def line(n,body): return dict(line=n,pid=100,cpu=0,comm='w',timestamp_ns=n*1000,body=body)
        lines=[line(1,'tracing_mark_write: CT_SYSCALL phase=BEGIN id=100-1 nr=1 name=write executed_fd=6'),
               line(2,'sys_enter: NR 1 (c8, 7ffc, a3, 0, 0, 0)'),
               line(3,'sys_exit: NR 1 = 163'),
               line(4,'sys_enter: NR 1 (6, 200000000080, 4, 0, 0, 0)'),
               line(5,'sys_exit: NR 1 = 4'),
               line(6,'tracing_mark_write: CT_SYSCALL phase=END id=100-1 nr=1 name=write executed_fd=6 ret=4 errno=0')]
        calls,issues=correlate_syscalls(lines)
        self.assertFalse(issues)
        self.assertTrue(calls[0]['verified'])
        self.assertEqual(calls[0]['enter_line'],4)
        self.assertEqual(calls[0]['raw_result'],4)

    def test_decimal_marker_fd_matches_hexadecimal_ftrace_argument(self):
        def line(n,body): return dict(line=n,pid=100,cpu=0,comm='w',timestamp_ns=n*1000,body=body)
        lines=[line(1,'tracing_mark_write: CT_SYSCALL phase=BEGIN id=100-7 nr=84 name=rmdir executed_fd=35184372089408'),
               line(2,'sys_enter: NR 84 (200000000240, 0, 0, 0, 0, 0)'),
               line(3,'sys_exit: NR 84 = 0'),
               line(4,'tracing_mark_write: CT_SYSCALL phase=END id=100-7 nr=84 name=rmdir executed_fd=35184372089408 ret=0 errno=0')]
        calls,issues=correlate_syscalls(lines)
        self.assertFalse(issues)
        self.assertTrue(calls[0]['verified'])
        self.assertEqual(calls[0]['enter_line'],2)

    def test_explicit_phase_attribution_is_mutually_exclusive_and_legacy_falls_back(self):
        def line(n, pid, body):
            return dict(line=n, pid=pid, cpu=0, comm='w', timestamp_ns=n*1000, body=body)
        events = [dict(pid=100,line=3), dict(pid=200,line=8), dict(pid=300,line=30)]
        lines = [line(1,100,'tracing_mark_write: CT_PHASE phase=BEGIN source=seed_setup target_pid=100 id=s1'),
                 line(4,100,'tracing_mark_write: CT_PHASE phase=END source=seed_setup target_pid=100 id=s1'),
                 line(4,200,'tracing_mark_write: CT_PHASE phase=BEGIN source=cleanup target_pid=200 id=c1'),
                 line(9,200,'tracing_mark_write: CT_PHASE phase=END source=cleanup target_pid=200 id=c1')]
        result = attribute_sources(events, lines, [])
        self.assertEqual(result['counts'], {'seed_setup':1, 'cleanup':1, 'background_unattributed':1})
        self.assertEqual(sum(result['counts'].values()), len(events))
        self.assertFalse(result['source_attribution_complete'])
        legacy = attribute_sources([dict(pid=300,line=30)], [], [])
        self.assertEqual(legacy['format'], 'legacy_inside_outside')
        self.assertEqual(legacy['counts'], {'outside_generated_call':1})

    def test_exploration_metrics_separate_failed_and_transition_calls(self):
        events = [dict(pid=1,line=3,point='BLOCK.BITMAP.TRANSITION',update_delta=-1,
                       formula='R-BDELTA',step=0,ref_delta=0)]
        calls = [dict(id='a',pid=1,enter_line=2,exit_line=4,verified=True,name='rmdir',
                      raw_result=-39,errno=39,repaired=0)]
        result = exploration_metrics(events,calls)
        self.assertEqual(result['independent_transition_calls'],1)
        self.assertEqual(result['failed_namespace_operations'],1)
        self.assertEqual(result['successful_namespace_operations'],0)

class WriteCoverageTests(unittest.TestCase):
    def fixture(self):
        f=Fixture();f.begin('BLOCK.ALLOC',10)
        for delta in (-3,3):
            f.e('WRITE.DESCRIPTOR','WRITE-BG',actual=100+delta,expected=100,update_delta=delta)
            f.e('BLOCK.DESCRIPTOR.LOCAL','R-BDELTA',update_delta=delta)
            f.e('WRITE.GLOBAL','WRITE-B',update_delta=delta)
            f.e('BLOCK.ALLOC.GLOBAL','R-BDELTA',update_delta=delta)
        f.end('BLOCK.ALLOC')
        for e in f.events:e['schema']=3
        return f
    def check(self,f):
        scopes,issues=scope_tree(f.events);self.assertFalse(issues)
        return write_coverage(f.events,scopes)
    def test_complete_pairing(self):
        r=self.check(self.fixture());self.assertTrue(r['available']);self.assertFalse(r['issues'])
    def test_opposite_missing_semantics_cannot_cancel(self):
        f=self.fixture();f.events=[e for e in f.events if e['point']!='BLOCK.DESCRIPTOR.LOCAL']
        self.assertTrue(self.check(f)['issues'])
    def test_missing_authority_event(self):
        f=self.fixture();f.events=[e for e in f.events if e['point']!='WRITE.GLOBAL']
        self.assertTrue(self.check(f)['issues'])
    def test_descriptor_payload_tampering(self):
        f=self.fixture();f.events[1]['actual']+=1
        self.assertTrue(any('payload' in i for i in self.check(f)['issues']))
    def test_lazy_repair_is_a_separate_publication(self):
        f=self.fixture();f.events[2]['point']='GROUP.LAZY.BLOCK.DESCRIPTOR'
        self.assertFalse(self.check(f)['issues'])
    def test_special_domain_is_explicitly_delegated(self):
        f=self.fixture();f.events[0]['point']=f.events[-1]['point']='FAST_COMMIT.DOMAIN'
        f.events=[e for e in f.events if e['point']!='BLOCK.DESCRIPTOR.LOCAL']
        r=self.check(f);self.assertFalse(r['issues']);self.assertTrue(r['special_events_delegated'])


if __name__=='__main__': unittest.main()
