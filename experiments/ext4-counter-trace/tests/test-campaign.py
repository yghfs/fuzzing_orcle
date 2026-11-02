#!/usr/bin/env python3
"""Campaign continuity, recovery and resource-governance regression tests."""
import importlib.util
import gzip
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from campaign import aggregate, atomic_json, digest, KINDS, LOSS_KEYS
from campaign_runner import (Collector, CampaignError, Runner, carry_start_line,
                             directory_bytes, program_input_plan, random_fault_policy,
                             RANDOM_FAULT_OPERATIONS,
                             fault_status_matches_arm,
                             random_fault_trace_event,
                             select_snapshot_event,
                             stable_snapshot_health,
                             validate_guest_trace_cpus,
                             validate_random_fault_mode,
                             verify_random_fault_errno_choice)
from fault_errno_catalog import errno_choices, generated_header_matches, load_catalog
from syz_workload import execution_issues
spec = importlib.util.spec_from_file_location('lifecycle_tests', pathlib.Path(__file__).with_name('test-lifecycle.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = pathlib.Path(self.tmp.name)
        (self.run / 'segments').mkdir()
        (self.run / 'checkpoint').mkdir()
        self.meta = dict(schema=1, run_id='campaign-test', boot_id='boot-a', status='complete',
                         segments_started=2, clean_shutdown=True, stop_reason='segment_limit',
                         devices=dict(zip(KINDS, ('253,16', '253,32', '253,48'))),
                         initial_states={}, final_states={}, limits={'cpus': 2}, disk={})
        for kind in KINDS:
            self.meta['initial_states'][kind] = dict(epoch=42, seq=0, active=0)
            self.meta['final_states'][kind] = dict(epoch=42, seq=2, active=0)
            atomic_json(self.run / (kind+'-disk.json'), {'valid': True,
                        'superblock':dict(free_clusters=100,free_inodes=20), 'descriptor_totals':{'dirs':2}})
            atomic_json(self.run / 'checkpoint' / (kind+'-final.json'),
                        dict(valid=True,namespace_valid=True,free_clusters=100,free_inodes=20,dirs=2))
            (self.run / (kind+'-fsck.txt')).write_text('clean\n')
            self.meta['disk'][kind] = dict(valid=True, matches_final_stable_snapshot=True, fsck_exit=0,
                 sha256=digest(self.run / (kind+'-disk.json')),
                 snapshot_sha256=digest(self.run / 'checkpoint' / (kind+'-final.json')),
                 fsck_sha256=digest(self.run / (kind+'-fsck.txt')))
        for index in range(2):
            folder = self.run / 'segments' / f'segment-{index:06d}'
            folder.mkdir()
            lines, domains = [], []
            for dev in self.meta['devices'].values():
                lines.append(f'worker-100 [000] .... 1.000001: ext4_counter_event: dev={dev} '
                             f'epoch=42 seq={index+1}\n')
                domains.append(dict(dev=dev, epoch=42, first=index+1, last=index+1,
                                    count=1, duplicates=0, holes=0))
            (folder / 'trace.txt').write_text(''.join(lines))
            atomic_json(folder / 'report.json',
                        dict(passed=True, classification_counts={'transient_mismatch': 2},
                             native_syscalls=[{'verified': True}],
                             runs=[dict(name='regular-single',
                             episodes=[dict(classification='transient_mismatch', onset_seq=1)])]))
            atomic_json(folder / 'manifest.json', dict(segment_id=index, run_id='campaign-test',
                        boot_id='boot-a', status='complete', kind='regular', mode='single',
                        trace_file='trace.txt', raw_domains=domains, lifecycle_report='report.json',
                        loss_delta={f'cpu{cpu}-stats.txt': dict.fromkeys(LOSS_KEYS,0) for cpu in range(2)},
                        evidence_sha256={'trace.txt':digest(folder/'trace.txt'), 'report.json':digest(folder/'report.json')}))
        self.save()

    def tearDown(self):
        self.tmp.cleanup()

    def save(self):
        atomic_json(self.run / 'campaign.json', self.meta)

    def test_runner_persists_post_augmentation_feedback_summary(self):
        runner = object.__new__(Runner)
        runner.meta = {}

        class Feedback:
            def summary(self):
                return dict(total_coverage=17, corpus_size=3, feedback_complete=True)

        runner.feedback = Feedback()
        runner.persist_feedback_summary()
        self.assertEqual(runner.meta['kcov_feedback'], {
            'total_coverage': 17, 'corpus_size': 3, 'feedback_complete': True})

    def test_verified_syscalls_count_is_cumulative_across_segments(self):
        runner = object.__new__(Runner)
        runner.run = self.run
        expected = 0
        for index, verified in enumerate(([True, False], [True, True, True])):
            lifecycle = self.run / 'segments' / f'segment-{index:06d}' / 'analysis' / 'lifecycle'
            lifecycle.mkdir(parents=True)
            with gzip.open(lifecycle / 'report.json.gz', 'wt') as stream:
                json.dump({'native_syscalls': [{'verified': value} for value in verified]}, stream)
            expected += sum(verified)

        last = self.run / 'segments' / 'segment-000001'
        self.assertEqual(runner.count_verified_syscalls_total(last), expected)

    def test_syscall_budget_requires_generated_and_verified_counts(self):
        from types import SimpleNamespace
        runner = object.__new__(Runner)
        runner.args = SimpleNamespace(target_syscalls=32)
        runner.generated_syscalls = 32
        runner.verified_syscalls = 31
        self.assertFalse(runner.syscall_budget_reached())
        runner.verified_syscalls = 32
        self.assertTrue(runner.syscall_budget_reached())

    def test_fault_stop_marker_accepts_only_verified_program_prefix(self):
        programs = self.run / 'programs'
        (programs / 'b1-w0').mkdir(parents=True)
        atomic_json(programs / 'b1-w0' / 'program.json', {
            'calls': [
                {'index': 0, 'nr': 1, 'name': 'mkdirat'},
                {'index': 1, 'nr': 2, 'name': 'unlinkat'},
                {'index': 2, 'nr': 3, 'name': 'fsync'},
            ]})
        calls = [
            {'id': 'segment-000000-b1-w0-1', 'program': 'b1-w0',
             'call_index': 0, 'verified': True, 'nr': 1, 'name': 'mkdirat'},
            {'id': 'segment-000000-b1-w0-2', 'program': 'b1-w0',
             'call_index': 1, 'verified': True, 'nr': 2, 'name': 'unlinkat'},
        ]
        self.assertEqual(execution_issues(programs, calls, 'segment-000000', 1, 1),
                         ['b1-w0:missing_or_extra_call_indices', 'b1-w0:call_2_not_verified'])
        self.assertEqual(execution_issues(
            programs, calls, 'segment-000000', 1, 1,
            fault_stop={'program': 'b1-w0', 'call_index': '1', 'operation': 'unlink_dirent_dirty'}), [])
        self.assertIn('b1-w0:calls_after_fault_stop', execution_issues(
            programs, calls + [{'id': 'segment-000000-b1-w0-3', 'program': 'b1-w0',
                                'call_index': 2, 'verified': True, 'nr': 3, 'name': 'fsync'}],
            'segment-000000', 1, 1,
            fault_stop={'program': 'b1-w0', 'call_index': '1'}))

    def test_record_finalized_syscalls_accumulates_and_persists(self):
        runner = object.__new__(Runner)
        runner.run = self.run
        runner.meta = {}
        runner.verified_syscalls = 3
        runner.save = lambda: atomic_json(self.run / 'campaign.json', runner.meta)
        folder = self.run / 'segments' / 'segment-000001'
        lifecycle = folder / 'analysis' / 'lifecycle'
        lifecycle.mkdir(parents=True, exist_ok=True)
        with gzip.open(lifecycle / 'report.json.gz', 'wt') as stream:
            json.dump({'native_syscalls': [{'verified': True}, {'verified': False},
                                            {'verified': True}]}, stream)
        self.assertEqual(runner.record_finalized_syscalls(folder), 2)
        self.assertEqual(runner.verified_syscalls, 5)
        self.assertEqual(runner.meta['verified_syscalls'], 5)

    def test_strict_kcov_gap_requests_one_graceful_stop(self):
        from types import SimpleNamespace
        runner = object.__new__(Runner)
        runner.args = SimpleNamespace(strict_kcov_feedback=True)
        runner.stop_requested = None
        messages = []
        runner.progress = messages.append
        runner.request_strict_kcov_stop()
        runner.request_strict_kcov_stop()
        self.assertEqual(runner.stop_requested, 'kcov_feedback_gap')
        self.assertEqual(messages, ['GRACEFUL STOP REQUEST reason=kcov_feedback_gap'])

        runner.args.strict_kcov_feedback = False
        runner.stop_requested = None
        runner.request_strict_kcov_stop()
        self.assertIsNone(runner.stop_requested)

    def change(self, index, **kwargs):
        path = self.run / 'segments' / f'segment-{index:06d}' / 'manifest.json'
        data = json.loads(path.read_text()); data.update(kwargs); atomic_json(path, data)

    def report(self):
        self.save()
        return aggregate(self.run)

    def test_complete_counts_and_dimensions(self):
        r = self.report()
        self.assertTrue(r['passed'])
        self.assertTrue(r['raw_coverage_complete'])
        self.assertEqual(r['classification_counts']['transient_mismatch'], 4)
        self.assertEqual(r['by_kind_mode']['regular/single']['transient_mismatch'], 4)

    def test_fault_hits_are_separate_from_stability_candidates(self):
        self.meta['limits']['random_fault_injection'] = True
        clear = dict(path='regular-before.json', status='clear', valid=True,
                     namespace_valid=True, relations_valid=True)
        candidate = dict(path='regular-after.json', status='candidate', valid=False,
                         observed_mismatches=1, namespace_valid=False,
                         namespace_mismatched=1, relations_valid=True, relations_mismatches=0)
        for index in range(2):
            self.change(index, fault_injection=dict(
                hit=True, seed=100+index, probability_ppm=1000000,
                status=dict(injected=1, operation='random'),
                event=dict(operation='inode_evict_final_dirty', pid=400+index),
                stable_observation=dict(before=clear, after=candidate if index == 0 else clear)))
        report = self.report()
        fault = report['fault_injection']
        self.assertEqual(fault['hit_count'], 2)
        self.assertEqual(fault['hit_counts_by_operation'], {'inode_evict_final_dirty': 2})
        self.assertEqual(fault['candidate_count'], 1)
        self.assertTrue(fault['review_required'])
        self.assertEqual(report['status'], 'candidate_review_required')
        self.assertTrue(report['passed'])
        self.assertTrue(report['counter_validation_passed'])
        self.assertTrue(all(json.loads((self.run / 'segments' / f'segment-{index:06d}' / 'manifest.json').read_text())['status'] == 'complete'
                            for index in range(2)))
        markdown = (self.run / 'campaign-report.md').read_text()
        self.assertIn('命中 2 次，候选 1 项', markdown)
        self.assertIn('hook 命中只表示该次调用按策略返回错误', markdown)

    def test_tainted_fault_epoch_is_checked_by_offline_evidence(self):
        self.meta['limits']['random_fault_injection'] = True
        self.meta['fault_tainted_kinds'] = ['regular']
        self.meta['final_states']['regular'] = dict(epoch=99, seq=0, active=0)
        manifest_path = self.run / 'segments/segment-000000/manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['fault_tainted'] = True
        manifest['fault_injection'] = dict(
            hit=True, errno_choice_verified=True,
            attribution=dict(status='verified'),
            status=dict(enabled=True, operation='random', injected=1),
            event=dict(operation='mballoc_mark', errno=-5),
            stable_observation=dict(before=dict(status='clear'), after=dict(status='unverified')),
            offline_check=dict(unmount_exit='0', fsck_exit='4', remount_exit='0'))
        atomic_json(manifest_path, manifest)
        report = self.report()
        self.assertNotIn('regular:campaign_endpoint_discontinuity', report['issues'])
        self.assertTrue(report['passed'])

    def test_fault_summary_ignores_disabled_status_residue(self):
        self.meta['limits']['random_fault_injection'] = True
        self.change(0, fault_injection=dict(
            hit=True, seed=100, probability_ppm=1000000,
            status=dict(enabled=False, operation='off', injected=1),
            stable_observation=dict(before=dict(status='candidate'),
                                    after=dict(status='candidate'))))
        report = self.report()
        fault = report['fault_injection']
        self.assertEqual(fault['hit_count'], 0)
        self.assertEqual(fault['candidate_count'], 0)
        self.assertFalse(fault['review_required'])

    def test_stable_snapshot_validation_failure_is_a_candidate(self):
        path = self.run / 'stable.json'
        atomic_json(path, dict(valid=False, namespace_valid=False, relations_valid=True,
                               observed_mismatches=0, namespace_mismatched=0,
                               relations_mismatches=0, reason='namespace_scan_invalid'))
        self.assertEqual(stable_snapshot_health(path)['status'], 'candidate')
        atomic_json(path, dict(valid=False, namespace_valid=False, relations_valid=False,
                               observed_mismatches=0, namespace_mismatched=0,
                               relations_mismatches=0,
                               reason='concurrent_or_unhealthy'))
        self.assertEqual(stable_snapshot_health(path)['status'], 'unverified')
        atomic_json(path, dict(valid=True, namespace_valid=True, relations_valid=True,
                               observed_mismatches=0, namespace_mismatched=0,
                               relations_mismatches=0))
        self.assertEqual(stable_snapshot_health(path)['status'], 'clear')
        path.write_text('{bad json')
        self.assertEqual(stable_snapshot_health(path)['status'], 'invalid_evidence')

    def test_fault_mode_still_stops_on_execution_evidence_loss(self):
        folder = self.run / 'integrity-check'
        folder.mkdir()
        atomic_json(folder / 'manifest.json', dict(issues=[
            'analysis_failed', 'b1-w0:call_2_not_verified',
            'raw_sequence_loss_or_duplicate:253,16']))
        runner = object.__new__(Runner)
        issues = runner.segment_trace_integrity_issues(folder)
        self.assertEqual(issues, ['b1-w0:call_2_not_verified',
                                  'raw_sequence_loss_or_duplicate:253,16'])

    def test_hook_only_workload_exit_is_quarantined_only_for_syscall_gaps(self):
        folder = self.run / 'hook-only'
        lifecycle = folder / 'analysis' / 'lifecycle'
        lifecycle.mkdir(parents=True)
        report = lifecycle / 'report.json.gz'
        with gzip.open(report, 'wt') as stream:
            json.dump({'passed': True, 'native_syscalls': []}, stream)
        (folder / 'workload.txt').write_text('mkdir workload tree: Cannot allocate memory\n')
        atomic_json(folder / 'manifest.json', dict(
            fault_injection=dict(
                hit=True,
                event=dict(operation='mb_buddy_load', errno=-12, trace_line=7),
                attribution=dict(status='hook_only', hook_trace_verified=True,
                                  native_envelope_count=0)),
            fault_expected_workload_exit=True,
            guest_exit=90,
            batches=1,
            syzkaller_execution_issues=[
                'b1-w0:missing_or_extra_call_indices',
                'b1-w0:call_0_not_verified',
            ],
            issues=['analysis_failed', 'b1-w0:missing_or_extra_call_indices',
                    'b1-w0:call_0_not_verified'],
            lifecycle_report='analysis/lifecycle/report.json.gz'))
        runner = object.__new__(Runner)
        gaps = ['b1-w0:missing_or_extra_call_indices', 'b1-w0:call_0_not_verified']
        self.assertTrue(runner.hook_only_workload_exit_allowed(folder, gaps))

        self.assertFalse(runner.hook_only_workload_exit_allowed(
            folder, gaps + ['raw_sequence_loss_or_duplicate:253,16']))
        manifest = json.loads((folder / 'manifest.json').read_text())
        manifest['fault_injection']['attribution']['status'] = 'verified'
        atomic_json(folder / 'manifest.json', manifest)
        self.assertFalse(runner.hook_only_workload_exit_allowed(folder, gaps))

    def test_aggregate_reports_shared_object_interleavings_and_quiescent_scans(self):
        folder=self.run/'segments/segment-000001'
        manifest=json.loads((folder/'manifest.json').read_text())
        manifest['object_sharing']='shared'
        atomic_json(folder/'manifest.json',manifest)
        report=json.loads((folder/'report.json').read_text())
        shared_scope_native_pair = {
            'left': {'syscall_id': 'call-a'}, 'right': {'syscall_id': 'call-b'},
            'shared_objects': ['directory:12'],
            'basis': ['ext4_scope_interval', 'native_syscall_interval']}
        shared_native_only_pair = {
            'left': {'syscall_id': 'call-c'}, 'right': {'syscall_id': 'call-d'},
            'shared_objects': ['inode:12'], 'basis': ['native_syscall_interval']}
        report['runs'][0].update(
            interleavings=[shared_scope_native_pair, shared_native_only_pair],
            scope_interleavings=[shared_scope_native_pair],
            native_syscall_interleavings=[shared_scope_native_pair, shared_native_only_pair],
            snapshots=[{'quiescent':True,'valid':True}, {'quiescent':False,'valid':False}])
        atomic_json(folder/'report.json',report)
        manifest=json.loads((folder/'manifest.json').read_text())
        manifest['evidence_sha256']['report.json']=digest(folder/'report.json')
        atomic_json(folder/'manifest.json',manifest)
        result=self.report()
        self.assertTrue(result['passed'])
        self.assertEqual(result['concurrency'], {
            'shared_segments':1,
            'operation_pairs':2, 'shared_object_pairs':2,
            'scope_operation_pairs':1, 'scope_shared_object_pairs':1,
            'native_syscall_pairs':2, 'native_shared_object_pairs':2,
            'quiescent_valid_snapshots':1, 'rejected_or_interrupted_snapshots':1})
        summary=json.loads((self.run/'segment-summary.jsonl').read_text().splitlines()[1])
        self.assertEqual(summary['object_sharing'],'shared')
        self.assertEqual(summary['interleavings'], {
            'operation_pairs':2, 'shared_object_pairs':2,
            'scope_operation_pairs':1, 'scope_shared_object_pairs':1,
            'native_syscall_pairs':2, 'native_shared_object_pairs':2,
            'quiescent_valid_snapshots':1, 'rejected_or_interrupted_snapshots':1})
        markdown=(self.run/'campaign-report.md').read_text()
        self.assertIn('ext4 scope 交错：1 对（共享 inode/目录对象 1 对）',markdown)
        self.assertIn('native syscall 时间窗交错：2 对（共享 inode/目录对象 2 对）',markdown)

    def test_strict_kcov_feedback_is_a_campaign_success_gate(self):
        self.meta['limits']['strict_kcov_feedback'] = True
        feedback = self.run / 'feedback'; feedback.mkdir()
        atomic_json(feedback / 'coverage.json', {'schema': 4, 'feedback_complete': True})
        report = self.report()
        self.assertTrue(report['passed'])
        self.assertTrue(report['counter_validation_passed'])
        self.assertTrue(report['kcov_feedback_complete'])

        manifest = self.run / 'segments/segment-000001/manifest.json'
        data = json.loads(manifest.read_text())
        data['kcov_feedback_errors'] = ['b1-w0:saturated']
        atomic_json(manifest, data)
        atomic_json(feedback / 'coverage.json', {'schema': 4, 'feedback_complete': False})
        report = self.report()
        self.assertFalse(report['passed'])
        self.assertTrue(report['counter_validation_passed'])
        self.assertFalse(report['kcov_feedback_complete'])
        self.assertIn('kcov_feedback_incomplete', report['issues'])

    def test_verified_syscall_budget_is_a_campaign_gate(self):
        self.meta['limits']['target_syscalls'] = 3
        report = self.report()
        self.assertFalse(report['passed'])
        self.assertIn('verified_syscall_budget_not_reached', report['issues'])
        self.meta['limits']['target_syscalls'] = 2
        report = self.report()
        self.assertTrue(report['passed'])
        self.assertEqual(report['native_verified'], 2)

    def test_single_only_run_never_selects_concurrent_and_rotates_all_images(self):
        from types import SimpleNamespace
        runner=object.__new__(Runner)
        runner.run=self.run
        runner.meta=self.meta
        runner.args=SimpleNamespace(mode='single',seed=100,workers=8,memory_mb=0)
        runner.save=lambda:None
        with tempfile.TemporaryDirectory(dir=self.run) as tmp:
            runner.run=pathlib.Path(tmp)
            manifests=[json.loads((runner.new_segment(i)/'manifest.json').read_text()) for i in range(6)]
        self.assertEqual([m['kind'] for m in manifests],list(KINDS)*2)
        self.assertTrue(all(m['mode']=='single' and m['workers']==1 for m in manifests))

    def test_default_matrix_still_covers_both_modes_for_each_image(self):
        from types import SimpleNamespace
        runner=object.__new__(Runner)
        runner.run=self.run
        runner.meta=self.meta
        runner.args=SimpleNamespace(mode='matrix',seed=100,workers=4,memory_mb=32)
        runner.save=lambda:None
        with tempfile.TemporaryDirectory(dir=self.run) as tmp:
            runner.run=pathlib.Path(tmp)
            manifests=[json.loads((runner.new_segment(i)/'manifest.json').read_text()) for i in range(6)]
        self.assertEqual([(m['kind'],m['mode']) for m in manifests],
                         [(kind,mode) for kind in KINDS for mode in ('single','concurrent')])
        self.assertEqual([m['workers'] for m in manifests],[1,4]*3)
        self.assertEqual([m['object_sharing'] for m in manifests],
                         ['private','shared']*3)
        self.assertEqual([m['synchronized_worker_start'] for m in manifests],
                         [False,True]*3)

    def test_shared_workers_all_use_the_shared_object_contention_prefix(self):
        plans=[program_input_plan('shared',ordinal,0) for ordinal in range(8)]
        self.assertEqual(plans,[('resource-valid','shared-contention')]*8)
        self.assertEqual(program_input_plan('private',0,2),('resource-valid','namespace'))
        self.assertEqual(program_input_plan('private',2,2),('resource-valid','file-state'))
        self.assertEqual(program_input_plan('private',3,2),('ordinary','auto'))

    def test_measurement_start_preserves_prepared_setup_anchors(self):
        runner = object.__new__(Runner)
        runner.run = self.run
        runner.meta = {'initial_states': {kind: {'seq': 5} for kind in KINDS}}
        runner.save = lambda: None
        folder = self.run / 'measurement-start'; folder.mkdir()
        for index, kind in enumerate(KINDS):
            atomic_json(folder / f'{kind}-start.json', {'seq': 100 + index, 'active': 0})
        runner.establish_measurement_start(folder)
        self.assertEqual(runner.meta['prepared_states'], {kind: {'seq': 5} for kind in KINDS})
        self.assertEqual(runner.meta['initial_states'], {
            kind: {'seq': 100 + index, 'active': 0} for index, kind in enumerate(KINDS)})
        self.assertGreater(runner.meta['measurement_start_wall_ns'], 0)

    def test_scenario_evidence_validates_profiles_and_pressure_threshold(self):
        from types import SimpleNamespace
        runner = object.__new__(Runner)
        runner.run = self.run
        initial = self.run / 'initial'; initial.mkdir()
        segment = self.run / 'scenario-segment'; segment.mkdir()
        for kind in KINDS:
            (initial / f'{kind}-scenario.txt').write_text(
                'scenario=all\nstateful_files=32\nlarge_dir_files=256\n'
                'pressure_bytes=109051904\navailable_kb=24000\npressure_target_free_kb=24576\n')
            (segment / f'{kind}-scenario.txt').write_text(
                'scenario=all\nstateful_files=30\nlarge_dir_files=250\n'
                'pressure_bytes=109051904\navailable_kb=24500\npressure_target_free_kb=24576\n')
        evidence, issues = runner.validate_scenario_evidence(segment, 'all')
        self.assertFalse(issues)
        self.assertEqual(set(evidence), set(KINDS))
        (segment / 'bigalloc-scenario.txt').write_text(
            'scenario=all\nstateful_files=30\nlarge_dir_files=250\n'
            'pressure_bytes=109051904\navailable_kb=30000\npressure_target_free_kb=24576\n')
        _, issues = runner.validate_scenario_evidence(segment, 'all')
        self.assertIn('scenario_pressure_target_not_reached:bigalloc', issues)

    def test_scenario_evidence_requires_long_lived_and_large_directory_fixtures(self):
        runner = object.__new__(Runner)
        runner.run = self.run
        initial = self.run / 'initial'; initial.mkdir()
        segment = self.run / 'scenario-segment'; segment.mkdir()
        for kind in KINDS:
            for parent in (initial, segment):
                (parent / f'{kind}-scenario.txt').write_text(
                    'scenario=all\nstateful_files=0\nlarge_dir_files=0\n'
                    'pressure_bytes=1\navailable_kb=1\npressure_target_free_kb=24576\n')
        _, issues = runner.validate_scenario_evidence(segment, 'all')
        self.assertIn('scenario_stateful_resources_missing:regular', issues)
        self.assertIn('scenario_large_directory_missing:regular', issues)

    def test_missing_segment(self):
        self.meta['segments_started'] = 3
        self.assertIn('missing_duplicate_or_noncontinuous_segment_ids', self.report()['issues'])

    def test_noncontinuous_segment_id(self):
        self.change(1, segment_id=3)
        self.assertFalse(self.report()['coverage_complete'])

    def test_timeout_keeps_episodes(self):
        self.change(1, status='timeout')
        r=self.report()
        self.assertFalse(r['coverage_complete'])
        self.assertEqual(r['complete_segments'],1)
        self.assertEqual(len((self.run/'episodes.jsonl').read_text().splitlines()),2)

    def test_kcov_feedback_gap_is_reported_without_failing_the_campaign(self):
        self.change(1, kcov_feedback_errors=['b1-w0:saturated'])
        r=self.report()
        self.assertTrue(r['passed'])
        self.assertTrue(r['coverage_complete'])
        self.assertEqual(r['kcov_feedback_gaps'],
                         [dict(segment_id=1, errors=['b1-w0:saturated'])])
        self.assertIn('## KCOV coverage feedback gaps',
                      (self.run/'campaign-report.md').read_text())

    def test_boot_change_rejected(self):
        self.change(1,boot_id='boot-b')
        self.assertTrue(any('boot_id_changed' in x for x in self.report()['issues']))

    def test_run_change_rejected(self):
        self.change(1,run_id='wrong')
        self.assertFalse(self.report()['raw_coverage_complete'])

    def test_loss_rejected(self):
        self.change(1, loss_delta={'cpu0-stats.txt': dict(overrun=1)})
        self.assertFalse(self.report()['coverage_complete'])

    def test_missing_or_empty_trace(self):
        for data in (None, ''):
            path=self.run/'segments/segment-000001/trace.txt'
            if data is None: path.unlink()
            else: path.write_text(data)
            self.assertFalse(self.report()['passed'])

    def test_checkpoint_finalize_accepts_already_compressed_segment(self):
        import gzip
        folder = self.run / 'segments/segment-000001'
        raw = folder / 'trace.txt'
        compressed = folder / 'trace.txt.gz'
        with gzip.open(compressed, 'wb') as stream:
            stream.write(raw.read_bytes())
        raw.unlink()
        lifecycle = folder / 'analysis/lifecycle'
        lifecycle.mkdir(parents=True)
        with gzip.open(lifecycle / 'report.json.gz', 'wt') as stream:
            json.dump({'passed': True}, stream)
        runner = object.__new__(Runner)
        runner.run = self.run
        runner.save = lambda: None
        runner.progress = lambda *args: None
        result = runner.finalize(folder, checkpoint=True)
        self.assertEqual(result['trace_file'], 'trace.txt.gz')
        self.assertNotIn('no_trace_collected', result.get('issues', []))

    def test_duplicate_and_epoch_changes_rejected(self):
        path=self.run/'segments/segment-000001/trace.txt'
        content=path.read_text()
        for altered in (content.replace('seq=2','seq=1'), content.replace('epoch=42','epoch=43')):
            path.write_text(altered)
            self.assertFalse(self.report()['raw_coverage_complete'])

    def test_manifest_raw_ranges_cannot_hide_trace_tampering(self):
        path=self.run/'segments/segment-000001/trace.txt'
        path.write_text(path.read_text().replace('seq=2','seq=20'))
        self.assertTrue(any('missing_or_changed_evidence' in x for x in self.report()['issues']))

    def test_disk_hash_and_shutdown_required(self):
        self.meta['clean_shutdown']=False
        self.assertFalse(self.report()['passed'])
        self.meta['clean_shutdown']=True
        (self.run/'regular-disk.json').write_text('{}')
        self.assertFalse(self.report()['passed'])

    def test_final_snapshot_tampering(self):
        (self.run/'checkpoint/regular-final.json').write_text('{}')
        self.assertFalse(self.report()['passed'])

    def test_recomputed_disk_comparison_ignores_cached_true(self):
        path=self.run/'regular-disk.json'
        d=json.loads(path.read_text());d['superblock']['free_clusters']+=1
        atomic_json(path,d)
        self.meta['disk']['regular']['sha256']=digest(path)
        self.assertIn('regular:disk_final_snapshot_disagrees',self.report()['issues'])

    def test_interrupted_prefix_has_no_disk_or_complete_claim(self):
        self.meta.update(status='host_interrupted', final_states={}, disk={})
        self.assertFalse(self.report()['coverage_complete'])

    def test_real_lifecycle_used_for_gaps(self):
        f=fixtures.Fixture();f.snapshot(1);f.transition(recover=False);f.snapshot(2,b=(97,97,100))
        _, report=f.run()
        self.assertGreater(report['classification_counts']['persistent_mismatch'],0)

    def test_quota_counts_logical_image_and_preserves_it(self):
        path=self.run/'sparse.raw'
        with path.open('wb') as stream:
            stream.truncate(40*1024**2)
        self.assertGreater(directory_bytes(self.run),40*1024**2)
        runner=object.__new__(Runner)
        from types import SimpleNamespace
        runner.args=SimpleNamespace(max_run_mb=32,min_free_mb=0)
        runner.run=self.run; runner.cancelled=None; runner.collector=None; runner.qemu=None
        runner.last_quota_check=0; runner.quota_graceful=False
        with self.assertRaisesRegex(CampaignError,'max_run_bytes'): runner.check()
        self.assertTrue(path.exists())

    def test_run_quota_requests_graceful_stop_with_finalization_reserve(self):
        from types import SimpleNamespace
        runner=object.__new__(Runner)
        runner.args=SimpleNamespace(max_run_mb=32,min_free_mb=0)
        runner.run=self.run; runner.cancelled=None; runner.collector=None; runner.qemu=None
        runner.last_quota_check=0; runner.quota_graceful=True; runner.stop_requested=None
        with (self.run/'large.raw').open('wb') as stream:stream.truncate(27*1024**2)
        runner.check()
        self.assertEqual(runner.stop_requested,'max_run_bytes')

    def test_finalization_allows_transport_and_disk_evidence_after_collection_deadline(self):
        from types import SimpleNamespace
        runner=object.__new__(Runner)
        runner.args=SimpleNamespace(max_run_mb=1024,min_free_mb=0)
        runner.run=self.run; runner.cancelled=None; runner.collector=None; runner.qemu=None
        runner.last_quota_check=0; runner.quota_graceful=True; runner.stop_requested=None
        runner.deadline=0
        runner.finalizing=False
        with self.assertRaisesRegex(CampaignError,'campaign_wall_watchdog'):
            runner.check()
        runner.finalizing=True
        runner.check()

    def test_quota_stop_completes_checkpoint_and_shutdown(self):
        import types
        runner=object.__new__(Runner)
        runner.args=types.SimpleNamespace(segments=2,duration_seconds=3600)
        runner.run=self.run; runner.started=0; runner.meta=dict(stop_reason=None,status='running',
            final_states={},initial_states={}); runner.current=None; runner.cancelled=None
        runner.qemu=None; runner.collector=types.SimpleNamespace(wait=lambda *a,**k:None,stop=lambda:None)
        runner.stop_requested=None; runner.carry=None; runner.previous_loss={}
        runner.discard_segment=None; runner.pending_segment=None
        runner.progress=lambda *a,**k:None; runner.save=lambda:None
        segment=self.run/'segments/segment-000000'; segment.mkdir(exist_ok=True)
        (segment/'trace.txt').write_text('segment trace\n')
        pending=self.run/'segments/segment-000001'; pending.mkdir(exist_ok=True)
        (pending/'trace.txt').write_text('checkpoint tail\n')
        runner.prepare=lambda:None
        checks={'count':0}
        def check(**kw): checks['count']+=1
        runner.check=check
        def new_segment(index):
            runner.pending_segment=(segment,pending)[index]
            runner.meta['segments_started']=index+1
            return (segment,pending)[index]
        runner.new_segment=new_segment
        runner.guest=lambda *a,**k:0
        collected=[]
        def collect_segment(folder):
            collected.append(folder)
            runner.pending_segment=None
        calls=[]
        def finalize(folder,**kw):
            checkpoint=kw.get('checkpoint',False)
            calls.append(('finalize',checkpoint))
            if not checkpoint: runner.stop_requested='max_run_bytes'
            return True
        runner.finalize=finalize
        runner.collect_segment=collect_segment
        runner.fetch=lambda *a,**k:None
        runner.finish_disk=lambda:calls.append(('finish_disk',True))
        runner.count_verified_syscalls=lambda folder:0
        def merge(target):
            from campaign_runner import Runner as RunnerClass
            RunnerClass.merge_pending_trace(runner,target)
        runner.merge_pending_trace=merge
        for kind in KINDS:
            atomic_json(self.run/'checkpoint'/(kind+'-final-state.json'),{'valid':True})
        (self.run/'checkpoint/dmesg.txt').write_text('')
        from unittest.mock import patch
        with patch('campaign_runner.aggregate',return_value={'passed':True,'segments':1,
                   'coverage_complete':True,'issues':[]}) as aggregate:
            self.assertEqual(runner.execute(),0)
        self.assertEqual(runner.meta['stop_reason'],'max_run_bytes')
        self.assertTrue(runner.meta['clean_shutdown'] if 'clean_shutdown' in runner.meta else True)
        self.assertEqual(runner.meta['segments_started'],2)
        self.assertEqual(calls,[('finalize',False),('finalize',True),('finish_disk',True)])
        self.assertEqual(collected,[segment,pending])
        aggregate.assert_called_once_with(self.run)

    def test_aggregation_respects_remaining_output_budget(self):
        with (self.run/'large.raw').open('wb') as stream:stream.truncate(40*1024**2)
        self.meta['limits']['max_run_mb']=32
        r=self.report()
        self.assertFalse(r['passed'])
        self.assertIn('aggregate_output_quota',r['issues'])
        self.assertTrue((self.run/'segments/segment-000000/report.json').exists())

    def test_live_collector_trace_cap_seals_evidence(self):
        import io
        from types import SimpleNamespace
        owner=SimpleNamespace(run=self.run,args=SimpleNamespace(max_trace_mb=0))
        collector=object.__new__(Collector)
        collector.owner=owner; collector.sealed=set(); collector.current=None; collector.stream=None
        collector.bytes=0; collector.preamble=bytearray(); collector.terminal=False; collector.error=None
        collector.lock=__import__('threading').Lock()
        collector.proc=SimpleNamespace(stdout=io.BytesIO(
            b'sh-1 [000] .... 1.0: tracing_mark_write: CT_CAMPAIGN ROTATE id=segment-000009\n'))
        collector.read()
        self.assertEqual(collector.error,'max_trace_bytes')
        self.assertIn('segment-000009',collector.sealed)
        self.assertTrue((self.run/'segments/segment-000009/trace.txt').exists())

    def test_live_rotation_retains_bridge_and_terminal(self):
        import io
        from types import SimpleNamespace
        collector=object.__new__(Collector)
        collector.owner=SimpleNamespace(run=self.run,args=SimpleNamespace(max_trace_mb=1))
        collector.sealed=set();collector.current=None;collector.stream=None
        collector.bytes=0;collector.preamble=bytearray();collector.terminal=False;collector.error=None
        collector.lock=__import__('threading').Lock()
        collector.proc=SimpleNamespace(stdout=io.BytesIO(
            b'sh-1 [000] .... 1.0: tracing_mark_write: CT_CAMPAIGN ROTATE id=segment-000008\n'
            b'background bridge event\n'
            b'sh-1 [000] .... 1.1: tracing_mark_write: CT_CAMPAIGN ROTATE id=segment-000009\n'
            b'sh-1 [000] .... 1.2: tracing_mark_write: CT_CAMPAIGN TERMINAL\n'))
        collector.read()
        self.assertIsNone(collector.error)
        self.assertTrue(collector.terminal)
        self.assertEqual(collector.sealed,{'segment-000008','segment-000009'})
        self.assertIn('background bridge event',(self.run/'segments/segment-000008/trace.txt').read_text())

    def test_first_rotation_keeps_trace_events_emitted_before_marker(self):
        import io
        from types import SimpleNamespace
        collector=object.__new__(Collector)
        collector.owner=SimpleNamespace(run=self.run,args=SimpleNamespace(max_trace_mb=1))
        collector.sealed=set();collector.current=None;collector.stream=None
        collector.bytes=0;collector.preamble=bytearray();collector.terminal=False;collector.error=None
        collector.lock=__import__('threading').Lock()
        collector.proc=SimpleNamespace(stdout=io.BytesIO(
            b'worker-1 [000] .... 1.0: ext4_counter_event: dev=253,16 seq=8\n'
            b'sh-1 [000] .... 1.1: tracing_mark_write: CT_CAMPAIGN ROTATE id=segment-000012\n'
            b'worker-1 [000] .... 1.2: ext4_counter_event: dev=253,16 seq=9\n'))
        collector.read()
        trace=(self.run/'segments/segment-000012/trace.txt').read_text()
        self.assertIn('seq=8',trace)
        self.assertIn('seq=9',trace)

    def test_cpu_commit_and_drop_loss(self):
        for key in LOSS_KEYS:
            data={f'cpu{cpu}-stats.txt':dict.fromkeys(LOSS_KEYS,0) for cpu in range(2)}
            data['cpu0-stats.txt'][key]=1
            self.change(1,loss_delta=data)
            self.assertFalse(self.report()['raw_coverage_complete'])

    def test_loss_evidence_tracks_online_trace_cpus_not_requested_smp_count(self):
        self.meta['trace_cpus'] = 1
        for index in range(2):
            self.change(index, loss_delta={'cpu0-stats.txt': dict.fromkeys(LOSS_KEYS, 0)})
        report = self.report()
        self.assertTrue(report['raw_coverage_complete'])

    def test_guest_must_bring_all_requested_vcpus_online(self):
        self.assertEqual(validate_guest_trace_cpus(2, ['cpu0-stats.txt', 'cpu1-stats.txt']), 2)
        with self.assertRaisesRegex(CampaignError,
                                   'guest_cpu_count_mismatch:requested=2:online=1'):
            validate_guest_trace_cpus(2, ['cpu0-stats.txt'])

    def test_random_fault_policy_is_seeded_and_limited_to_closed_runtime_hooks(self):
        from types import SimpleNamespace
        args = SimpleNamespace(
            random_fault_operations='mballoc_mark,inode_evict_final_dirty',
            random_fault_probability_ppm=2500,
            random_fault_errnos='EROFS')
        first = random_fault_policy(args, 20261010, 7)
        second = random_fault_policy(args, 20261010, 7)
        self.assertEqual(first, second)
        self.assertEqual(first['operations'], ['mballoc_mark', 'inode_evict_final_dirty'])
        self.assertIn('probability_ppm=2500', first['config'])
        self.assertIn('errnos=EROFS', first['config'])
        self.assertNotIn('isolate_after_hit=', first['config'])
        self.assertTrue(first['isolate_after_hit'])
        self.assertEqual(first['errnos'], ['EROFS'])
        native = random_fault_policy(SimpleNamespace(
            random_fault_operations='mballoc_mark,inode_evict_final_dirty',
            random_fault_probability_ppm=10, random_fault_errnos='native'), 1, 0)
        self.assertIn('errnos=native', native['config'])
        self.assertEqual(native['supported_errnos']['mballoc_mark'], ['EIO', 'EROFS'])
        self.assertEqual(native['supported_errnos']['inode_evict_final_dirty'],
                         ['EFSCORRUPTED', 'EROFS', 'ENOSPC'])
        event = dict(operation='mballoc_mark', errno=-74, errno_choice_index=6,
                     errno_choice_draw=13, fields={'randomized': '1'})
        event.update(errno=-30, errno_choice_index=1)
        status = dict(errno=-30, errno_choice_index=1, errno_choices='native')
        self.assertEqual(verify_random_fault_errno_choice(event, status, native),
                         (True, [-code for code in native['supported_errno_codes']['mballoc_mark']]))
        missing_choice = dict(event, errno_choice_index=-1, errno_choice_draw=-1)
        self.assertFalse(verify_random_fault_errno_choice(missing_choice, status, native)[0])
        status['errno'] = -5
        self.assertFalse(verify_random_fault_errno_choice(event, status, native)[0])
        mapped = random_fault_policy(SimpleNamespace(
            random_fault_operations='mballoc_mark,inode_evict_final_dirty',
            random_fault_probability_ppm=10,
            random_fault_errnos='mballoc_mark=EROFS;inode_evict_final_dirty=EFSCORRUPTED,ENOSPC'),
            1, 0)
        self.assertEqual(mapped['errnos'], ['per_hook'])
        self.assertEqual(mapped['errno_by_operation']['mballoc_mark'], ['EROFS'])
        mapped_event = dict(operation='mballoc_mark', errno=-30,
                            errno_choice_index=0, errno_choice_draw=8,
                            fields={'randomized': '1'})
        mapped_status = dict(errno=-30, errno_choice_index=0,
                             errno_choices='-30', errno_operation='mballoc_mark')
        self.assertTrue(verify_random_fault_errno_choice(
            mapped_event, mapped_status, mapped)[0])
        with self.assertRaisesRegex(CampaignError, 'unsupported_random_fault_errno_choices'):
            random_fault_policy(SimpleNamespace(
                random_fault_operations='mballoc_mark',
                random_fault_probability_ppm=10, random_fault_errnos='ENOSPC'), 1, 0)
        with self.assertRaisesRegex(CampaignError, 'unsupported_random_fault_operations'):
            random_fault_policy(SimpleNamespace(random_fault_operations='fast_commit_inode_claim',
                                                random_fault_probability_ppm=1),
                                1, 0)

    def test_random_fault_policy_all_expands_runtime_hooks(self):
        from types import SimpleNamespace
        policy = random_fault_policy(SimpleNamespace(
            random_fault_operations='all',
            random_fault_probability_ppm=10000,
            random_fault_errnos='native'), 20261029, 0)
        self.assertEqual(policy['operations'], list(RANDOM_FAULT_OPERATIONS))
        self.assertEqual(len(policy['operations']), 87)
        self.assertNotIn('fast_commit_inode_claim', policy['operations'])
        self.assertEqual(policy['errnos'], ['native'])
        self.assertEqual(len(policy['supported_errnos']), 87)

    def test_fault_status_must_match_current_segment_arm(self):
        from types import SimpleNamespace
        policy = random_fault_policy(SimpleNamespace(
            random_fault_operations='mballoc_mark,inode_evict_final_dirty',
            random_fault_probability_ppm=10000, random_fault_errnos='native'), 1234, 7)
        arm = dict(enabled=True, mode='random', operation='random', generation=14,
                   target_pid=0, target_pgid=4154, random_seed=policy['seed'],
                   probability_ppm=policy['probability_ppm'], max_hits=1)
        valid = dict(arm, injected=1, errno_operation='mballoc_mark')
        self.assertTrue(fault_status_matches_arm(valid, arm, policy))
        stale_off = dict(valid, enabled=False, mode='targeted', operation='off',
                          generation=13, target_pid=0, random_seed=0,
                          probability_ppm=0, max_hits=0)
        self.assertFalse(fault_status_matches_arm(stale_off, arm, policy))
        stale_seed = dict(valid, random_seed=policy['seed'] + 1)
        self.assertFalse(fault_status_matches_arm(stale_seed, arm, policy))
        wrong_hook = dict(valid, errno_operation='unrelated_hook')
        self.assertFalse(fault_status_matches_arm(wrong_hook, arm, policy))

    def test_random_fault_trace_event_keeps_one_based_trace_line(self):
        path = self.run / 'fault-trace.txt'
        path.write_text(
            'ignored line\n'
            ' worker-42 [000] ..... 12.345678: ext4_fault_inject: '
            'dev=253,16 operation=mballoc_mark errno=-5 '
            'errno_choice_index=0 errno_choice_draw=3 generation=7 '
            'injection=1 pid=42 target_pid=42 program=workload '
            'target_program=- call_index=0 match_index=0 group=0 block=1 '
            'len=1 result=-5 randomized=1 random_seed=9 '
            'probability_ppm=1000000 random_check=0 random_draw=0\n')
        event = random_fault_trace_event(path, 42)
        self.assertEqual(event['trace_line'], 2)
        self.assertEqual(event['operation'], 'mballoc_mark')
        self.assertEqual(event['errno'], -5)
        self.assertEqual(event['timestamp_ns'], 12345678000)

    def test_random_fault_trace_event_matches_process_group_and_keeps_worker_pid(self):
        path = self.run / 'fault-pgid-trace.txt'
        path.write_text(
            'worker-42 [000] ..... 12.345678: ext4_fault_inject: '
            'dev=253,16 operation=mballoc_mark errno=-5 injection=1 pid=42 '
            'target_pid=0 target_pgid=900 program=workload target_program=- '
            'call_index=0 match_index=0 group=0 block=1 len=1 result=-5 '
            'randomized=1 random_seed=9 probability_ppm=1000000 random_check=0 random_draw=0\n'
            'worker-43 [001] ..... 12.345679: ext4_fault_inject: '
            'dev=253,16 operation=mballoc_mark errno=-5 errno_choice_index=0 '
            'errno_choice_draw=3 generation=7 injection=1 pid=43 target_pid=0 '
            'target_pgid=901 program=workload target_program=- call_index=0 '
            'match_index=0 group=0 block=1 len=1 result=-5 randomized=1 '
            'random_seed=9 probability_ppm=1000000 random_check=0 random_draw=0\n')
        event = random_fault_trace_event(path, 0, 901)
        self.assertEqual(event['trace_line'], 2)
        self.assertEqual(event['pid'], 43)
        self.assertEqual(event['target_pid'], 0)
        self.assertEqual(event['target_pgid'], 901)

    def test_random_fault_mode_allows_two_worker_concurrency(self):
        from types import SimpleNamespace
        valid = SimpleNamespace(random_fault_injection=True, mode='concurrent',
                                workers=2, max_batches=1)
        self.assertIsNone(validate_random_fault_mode(valid))
        with self.assertRaisesRegex(CampaignError, 'workers >= 2'):
            validate_random_fault_mode(SimpleNamespace(
                random_fault_injection=True, mode='concurrent', workers=1, max_batches=1))
        with self.assertRaisesRegex(CampaignError, 'max-batches 1'):
            validate_random_fault_mode(SimpleNamespace(
                random_fault_injection=True, mode='single', workers=1, max_batches=2))

    def test_fault_errno_catalog_drives_generated_kernel_whitelist(self):
        self.assertTrue(generated_header_matches())
        self.assertEqual(errno_choices('mballoc_mark'), ['EIO', 'EROFS'])
        self.assertNotIn('EIO', errno_choices('inode_evict_final_dirty'))
        self.assertEqual(errno_choices('inode_evict_final_dirty'),
                         ['EFSCORRUPTED', 'EROFS', 'ENOSPC'])
        self.assertEqual(errno_choices('mkdir_append_alloc'),
                         ['EIO', 'ENOMEM', 'ENOSPC', 'EROFS',
                          'EFSCORRUPTED', 'EFSBADCRC', 'EDQUOT'])
        self.assertEqual(errno_choices('inode_evict_bitmap_read'),
                         ['EIO', 'ENOMEM', 'EFSCORRUPTED', 'EFSBADCRC'])

    def test_formula_fault_hooks_have_runtime_source_and_errno_contracts(self):
        catalog = load_catalog()['hooks']
        source = pathlib.Path(__file__).resolve().parents[1] / 'dev/linux-6.12.1'
        injector = (source / 'fs/ext4/fault_inject.c').read_text()
        dirty_errnos = ['EFSCORRUPTED', 'EROFS', 'ENOSPC']
        formula_hooks = {
            'migrate_swap_inode_data': ('fs/ext4/migrate.c', 'EXT4_FAULT_MIGRATE_SWAP_INODE_DATA'),
            'xattr_set_inode_dirty': ('fs/ext4/xattr.c', 'EXT4_FAULT_XATTR_SET_INODE_DIRTY'),
            'ea_inode_ref_dirty': ('fs/ext4/xattr.c', 'EXT4_FAULT_EA_INODE_REF_DIRTY'),
            'acl_mode_dirty': ('fs/ext4/acl.c', 'EXT4_FAULT_ACL_MODE_DIRTY'),
            'inline_dir_block_dirty': ('fs/ext4/inline.c', 'EXT4_FAULT_INLINE_DIR_BLOCK_DIRTY'),
            'inline_dir_inode_dirty': ('fs/ext4/inline.c', 'EXT4_FAULT_INLINE_DIR_INODE_DIRTY'),
            'inline_data_block_write_begin': ('fs/ext4/inline.c', 'EXT4_FAULT_INLINE_DATA_BLOCK_WRITE_BEGIN'),
            'fileattr_flags_dirty': ('fs/ext4/ioctl.c', 'EXT4_FAULT_FILEATTR_FLAGS_DIRTY'),
            'fileattr_project_dirty': ('fs/ext4/ioctl.c', 'EXT4_FAULT_FILEATTR_PROJECT_DIRTY'),
        }
        for name, (path, operation) in formula_hooks.items():
            with self.subTest(hook=name):
                definition = catalog[name]
                self.assertEqual(definition['kernel_operation'], operation)
                self.assertTrue(definition['runtime_random'])
                self.assertIn(name, RANDOM_FAULT_OPERATIONS)
                expected_errnos = ['EIO'] if name == 'inline_data_block_write_begin' else dirty_errnos
                self.assertEqual(errno_choices(name), expected_errnos)
                self.assertIn(operation, (source / path).read_text())
                self.assertIn(operation, injector)
        errno_policy = ';'.join(
            f'{name}=' + ('EIO' if name == 'inline_data_block_write_begin' else 'ENOSPC')
            for name in formula_hooks)
        policy = random_fault_policy(SimpleNamespace(
            random_fault_operations=','.join(formula_hooks),
            random_fault_probability_ppm=10000,
            random_fault_errnos=errno_policy), 20261028, 0)
        self.assertEqual(policy['operations'], list(formula_hooks))
        self.assertTrue(all(policy['errno_by_operation'][name] ==
                            (['EIO'] if name == 'inline_data_block_write_begin'
                             else ['ENOSPC'])
                            for name in formula_hooks))

    def test_fault_errno_catalog_requires_reusable_source_and_return_contract(self):
        catalog = load_catalog()
        catalog['hooks']['mballoc_mark']['source'] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'catalog.json'
            path.write_text(json.dumps(catalog))
            with self.assertRaisesRegex(ValueError, 'invalid_fault_errno_hook:mballoc_mark'):
                load_catalog(path)

    def test_random_fault_policy_accepts_per_hook_errno_sets(self):
        from fault_errno_catalog import normalize_errno_selection
        operations = ['mballoc_mark', 'inode_evict_final_dirty']
        selected = normalize_errno_selection(
            'mballoc_mark=EROFS;inode_evict_final_dirty=EFSCORRUPTED,ENOSPC', operations)
        self.assertEqual(selected['mballoc_mark'], ['EROFS'])
        self.assertEqual(selected['inode_evict_final_dirty'], ['EFSCORRUPTED', 'ENOSPC'])
        with self.assertRaisesRegex(ValueError, 'missing_random_fault_errno_policy'):
            normalize_errno_selection('mballoc_mark=EIO', operations)
        with self.assertRaisesRegex(ValueError, 'invalid_random_fault_errno_policy'):
            normalize_errno_selection('mballoc_mark=EIO;mballoc_mark=EIO', operations)

    def test_errno_name_normalizer_is_driven_by_catalog_entries(self):
        from fault_errno_catalog import normalize_errno_choices
        self.assertEqual(normalize_errno_choices('-efscorrupted'), ['EFSCORRUPTED'])
        self.assertEqual(normalize_errno_choices('EUCLEAN'), ['EFSCORRUPTED'])
        self.assertEqual(normalize_errno_choices('-117'), ['EFSCORRUPTED'])
        self.assertEqual(normalize_errno_choices('-5,-28'), ['EIO', 'ENOSPC'])
        self.assertEqual(normalize_errno_choices('EDQUOT'), ['EDQUOT'])
        self.assertEqual(normalize_errno_choices('EBADMSG'), ['EFSBADCRC'])
        self.assertEqual(normalize_errno_choices('-74'), ['EFSBADCRC'])

    def test_workload_output_is_forwarded_before_exit_and_preserves_failure_log(self):
        import contextlib
        import io
        runner=object.__new__(Runner)
        runner.run=self.run
        runner.check=lambda **kw:None
        gate=self.run/'release'
        observed=[]
        original=runner.live_line
        def receive(line):
            original(line)
            observed.append(line)
            if b'WORKLOAD_START' in line:
                gate.touch()
        runner.live_line=receive
        script=('import pathlib,time,sys\n'
                f'gate=pathlib.Path({str(gate)!r})\n'
                'print("CT_PROGRESS phase=WORKLOAD_START program=b1-w0 wall_ns=0", flush=True)\n'
                'deadline=time.monotonic()+5\n'
                'while not gate.exists() and time.monotonic()<deadline: time.sleep(.01)\n'
                'if not gate.exists(): sys.exit(88)\n'
                'print("CT_PROGRESS phase=WORKLOAD_END program=b1-w0 exit=7", flush=True)\n'
                'sys.exit(7)\n')
        terminal=io.StringIO()
        with contextlib.redirect_stdout(terminal):
            with self.assertRaisesRegex(CampaignError,'command_exit_7'):
                runner.command([sys.executable,'-c',script], live=True, log='guest.txt', timeout=8)
        self.assertEqual(len(observed),2)
        self.assertIn('WORKLOAD_START',terminal.getvalue())
        self.assertIn('+08:00',terminal.getvalue())
        self.assertIn('WORKLOAD_END',(self.run/'guest.txt').read_text())
        self.assertIn('WORKLOAD_START',(self.run/'progress.log').read_text())

    def test_live_output_keeps_timeout_and_resource_checks(self):
        runner=object.__new__(Runner)
        runner.run=self.run
        checks=[]
        runner.check=lambda **kw:checks.append(kw)
        with self.assertRaisesRegex(CampaignError,'command_timeout'):
            runner.command([sys.executable,'-c',
                            'import time; print("workload started", flush=True); time.sleep(10)'],
                           timeout=.3, live=True, log='timeout.txt')
        self.assertTrue(checks)
        self.assertIn('workload started',(self.run/'timeout.txt').read_text())

    def test_fetch_retries_transport_failure_but_not_remote_failure(self):
        from types import SimpleNamespace
        runner = object.__new__(Runner)
        runner.run = self.run
        runner.scp_args = ['scp']
        runner.progress = lambda message: None
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                raise CampaignError('command_exit_255:scp')
            return 0
        runner.command = command
        with patch('campaign_runner.time.sleep'):
            self.assertEqual(runner.fetch('/remote', self.run / 'copy'), 0)
        self.assertEqual(len(calls), 2)
        runner.command = lambda argv, **kwargs: (_ for _ in ()).throw(
            CampaignError('command_exit_90:scp'))
        with self.assertRaisesRegex(CampaignError, 'command_exit_90:scp'):
            runner.fetch('/remote', self.run / 'copy')

    def test_fetch_retries_slow_scp_with_long_timeout(self):
        from types import SimpleNamespace
        runner = object.__new__(Runner)
        runner.run = self.run
        runner.scp_args = ['scp']
        runner.progress = lambda message: None
        calls = []
        def command(argv, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise CampaignError('command_timeout:scp')
            return 0
        runner.command = command
        with patch('campaign_runner.time.sleep'):
            self.assertEqual(runner.fetch('/remote', self.run / 'copy'), 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual([call['timeout'] for call in calls], [300, 300])

    def test_reserved_transport_classification_does_not_retry_remote_status(self):
        self.assertTrue(Runner._retryable_transport('command_exit_255:ssh'))
        self.assertTrue(Runner._retryable_transport('command_exit_255:scp'))
        self.assertFalse(Runner._retryable_transport('command_exit_90:ssh'))

    def test_kernel_diagnostic_is_preserved_when_trace_reader_dies(self):
        runner = object.__new__(Runner)
        runner.run = self.run
        (self.run / 'serial.log').write_text(
            '[ 10.0 ] kernel BUG at fs/ext4/inline.c:235!\n'
            '[ 10.1 ] Oops: invalid opcode: 0000 [#1]\n')
        fault = runner.kernel_diagnostics()
        self.assertEqual(fault['kind'], 'kernel_bug')
        self.assertIn('kernel_oops', fault['kinds'])
        self.assertTrue(any('inline.c:235' in line for line in fault['evidence']))

    def test_ledger_tracks_mutations_without_scanning_sealed_history(self):
        from quota_ledger import QuotaLedger
        from campaign import compress
        ledger = QuotaLedger(self.run)
        for folder in (self.run/'segments').iterdir():
            ledger.seal(folder)
        active = self.run/'segments/segment-000002'
        active.mkdir(); ledger.activate(active)
        (active/'trace.txt').write_bytes(b'x'*10000)
        atomic_json(self.run/'new.json', {'test': 123})
        self.assertEqual(ledger.refresh(), directory_bytes(self.run))
        compress(active/'trace.txt')
        (self.run/'new.json').write_text('short')
        self.assertEqual(ledger.refresh(), directory_bytes(self.run))
        scans = ledger.full_scans
        old_scan = ledger.scan
        def scan(path, excluded=()):
            self.assertNotIn(path, ledger.frozen)
            return old_scan(path, excluded)
        ledger.scan = scan
        self.assertEqual(ledger.check(), directory_bytes(self.run))
        self.assertEqual(ledger.full_scans, scans)
        ledger.scan = old_scan
        (self.run/'segments/segment-000000/late.txt').write_text('late mutation')
        ledger.reconcile()
        self.assertTrue(ledger.corrections)
        self.assertEqual(ledger.refresh(), directory_bytes(self.run))

    def test_carry_start_keeps_stable_snapshot_begin_marker(self):
        trace = self.run / 'carry-trace.txt'
        trace.write_text(
            'sh-10 [000] .... 1.000001: tracing_mark_write: '
            'CT_PHASE phase=BEGIN source=stable_snapshot target_pid=10 id=snapshot-10\n'
            'sh-10 [000] .... 1.000002: ext4_counter_event: dev=253,16 '
            'epoch=42 seq=1 op=7 formula=BEGIN\n'
            'sh-11 [000] .... 1.000003: tracing_mark_write: '
            'CT_PHASE phase=BEGIN source=stable_snapshot target_pid=11 id=snapshot-11\n'
            'sh-11 [000] .... 1.000004: ext4_counter_event: dev=253,32 '
            'epoch=42 seq=2 op=8 formula=BEGIN\n')
        self.assertEqual(carry_start_line(trace, [(2, 10), (4, 11)]), 1)
        self.assertEqual(carry_start_line(trace, [(4, 11)]), 3)

    def test_carry_start_falls_back_for_legacy_trace(self):
        trace = self.run / 'legacy-trace.txt'
        trace.write_text('worker-10 [000] .... 1.000001: ext4_counter_event: dev=253,16\n')
        self.assertEqual(carry_start_line(trace, [(1, 10)]), 1)

    def test_snapshot_anchor_requires_epoch_after_reset_reuses_operation_id(self):
        events = [
            dict(formula='BEGIN', op=42, dev='253,32', epoch=100, seq=9, line=10),
            dict(formula='BEGIN', op=42, dev='253,32', epoch=200, seq=3, line=20),
        ]
        snapshot = {'snapshot_id': 42}
        selected = select_snapshot_event(events, snapshot, '253,32',
                                         {'epoch': 200, 'seq': 3})
        self.assertEqual(selected['line'], 20)
        self.assertIsNone(select_snapshot_event(events, snapshot, '253,32',
                                                {'epoch': 300, 'seq': 3}))
        # Legacy evidence has no persisted epoch identity; latest is the
        # current boundary in the carried trace.
        self.assertEqual(select_snapshot_event(events, snapshot, '253,32')['line'], 20)


if __name__ == '__main__':
    unittest.main()
