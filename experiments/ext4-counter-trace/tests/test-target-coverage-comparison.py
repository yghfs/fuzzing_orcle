#!/usr/bin/env python3
import importlib.util
import json
import pathlib
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'compare-target-coverage.py'
SPEC = importlib.util.spec_from_file_location('compare_target_coverage', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TargetCoverageComparisonTests(unittest.TestCase):
    def test_uses_admitted_custom_corpus_and_rawcover_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            custom = root / 'custom'
            upstream = root / 'upstream'
            custom.mkdir()
            (custom / 'dependencies').mkdir(parents=True)
            (upstream / 'coverage-checkpoints').mkdir(parents=True)
            (custom / 'campaign.json').write_text(json.dumps({
                'status': 'complete', 'stop_reason': 'duration_limit',
                'collection_elapsed_seconds': 3600,
                'limits': {'feedback_mode': 'pc', 'target_scope': 'ext4_jbd2'},
                'dependencies': {'rootfs_sha256': 'root'},
                'build_sha256': {'kernel.bzImage': 'kernel'},
            }))
            (custom / 'campaign-report.json').write_text(json.dumps({
                'passed': True, 'coverage_complete': True, 'kcov_feedback_complete': True,
                'generated_syscalls': 16, 'native_verified': 16,
                'kcov_feedback': {
                    'target_scope': 'ext4_jbd2',
                    'coverage_scope': 'all_complete_samples',
                    'corpus_coverage_scope': 'admitted_corpus',
                    'total_coverage': 100, 'target_pc_coverage': 20,
                    'corpus_pc_coverage': 40, 'corpus_target_pc_coverage': 12,
                },
            }))
            (upstream / 'run-metadata.json').write_text(json.dumps({
                'status': 'passed', 'manager_exit_code': 0,
                'stopped_after_duration': True, 'coverage_endpoint_ready': True,
                'coverage_source': 'rawcover', 'seed_applied': False,
                'requested_syscalls': 21, 'actual_enabled_syscall_count': 144,
            }))
            (upstream / 'coverage-checkpoints/final.json').write_text(json.dumps({
                'coverage_scope': 'manager_corpus', 'source': 'rawcover',
                'all_kernel_pc': 80, 'ext4_pc': 10, 'jbd2_pc': 5,
                'target_ext4_jbd2_pc': 15,
            }))
            report = MODULE.build_report(custom, upstream, 'ext4_jbd2')
            self.assertEqual(report['aligned_corpus_target_pc']['custom_admitted_corpus'], 12)
            self.assertEqual(report['aligned_corpus_target_pc']['upstream_manager_corpus'], 15)
            self.assertIn('upstream_transitive_syscall_closure_differs', report['warnings'])
            self.assertIn('requested_syscall_sets_not_aligned', report['warnings'])
            self.assertIn('target_device_scope_differs_custom_vdb_vdc_vdd_upstream_seed_mount_image',
                          report['warnings'])
            self.assertNotIn('target_device_scope_differs_custom_vdb_vdc_vdd_upstream_vda',
                             report['warnings'])
            self.assertFalse(report['fairness']['strictly_causal_comparison'])
            self.assertFalse(report['fairness']['same_syscall_set'])
            self.assertFalse(report['fairness']['same_program_seed'])
            self.assertTrue(report['measurement_complete'])

            (upstream / 'manager.log').write_text("SYZFAIL: can't reallocate\n")
            failed_report = MODULE.build_report(custom, upstream, 'ext4_jbd2')
            self.assertFalse(failed_report['measurement_complete'])
            self.assertEqual(failed_report['upstream']['executor_failure_count'], 1)
            self.assertIn('upstream_executor_failure_observed', failed_report['warnings'])

    def test_earlier_upstream_snapshot_cannot_stand_in_for_final_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            custom = root / 'custom'
            upstream = root / 'upstream'
            (custom / 'dependencies').mkdir(parents=True)
            (upstream / 'coverage-checkpoints').mkdir(parents=True)
            (custom / 'campaign.json').write_text(json.dumps({
                'status': 'complete', 'stop_reason': 'duration_limit',
                'limits': {'feedback_mode': 'pc', 'target_scope': 'ext4_jbd2'},
            }))
            (custom / 'campaign-report.json').write_text(json.dumps({
                'coverage_complete': True, 'kcov_feedback_complete': True,
                'kcov_feedback': {'corpus_coverage_scope': 'admitted_corpus',
                                  'corpus_target_pc_coverage': 2},
            }))
            (upstream / 'run-metadata.json').write_text(json.dumps({
                'status': 'passed', 'manager_exit_code': 0,
                'stopped_after_duration': True, 'coverage_endpoint_ready': True,
                'coverage_source': 'rawcover', 'seed_program': 'upstream-ext4.syz',
                'seed_program_applied': False,
                'manager_execution_activity_observed_after_seed_load': False,
            }))
            (upstream / 'coverage-checkpoints/initial.json').write_text(json.dumps({
                'coverage_scope': 'manager_corpus', 'source': 'rawcover',
                'target_ext4_jbd2_pc': 10,
            }))
            report = MODULE.build_report(custom, upstream, 'ext4_jbd2')
            self.assertFalse(report['measurement_complete'])
            self.assertTrue(report['upstream']['coverage_path'].endswith('initial.json'))
            self.assertFalse(report['upstream']['final_coverage_checkpoint'])

    def test_marks_missing_coverage_as_incomplete_not_zero_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            custom = root / 'custom'
            upstream = root / 'upstream'
            custom.mkdir()
            upstream.mkdir()
            report = MODULE.build_report(custom, upstream, 'ext4_jbd2')
            self.assertFalse(report['measurement_complete'])
            self.assertIn('custom_coverage_collection_incomplete', report['warnings'])
            self.assertIn('upstream_coverage_collection_incomplete', report['warnings'])

    def test_zero_target_pc_is_inconclusive_when_manager_has_generic_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            custom = root / 'custom'
            upstream = root / 'upstream'
            (custom / 'dependencies').mkdir(parents=True)
            (upstream / 'coverage-checkpoints').mkdir(parents=True)
            (custom / 'campaign.json').write_text(json.dumps({
                'status': 'complete', 'stop_reason': 'duration_limit',
                'limits': {'target_scope': 'ext4_jbd2'},
            }))
            (custom / 'campaign-report.json').write_text(json.dumps({
                'passed': True, 'coverage_complete': True,
                'kcov_feedback_complete': True,
                'kcov_feedback': {'target_scope': 'ext4_jbd2',
                                  'corpus_coverage_scope': 'admitted_corpus',
                                  'corpus_target_pc_coverage': 12},
            }))
            (upstream / 'run-metadata.json').write_text(json.dumps({
                'status': 'passed', 'manager_exit_code': 0,
                'stopped_after_duration': True, 'coverage_endpoint_ready': True,
                'coverage_source': 'rawcover',
            }))
            (upstream / 'coverage-checkpoints/final.json').write_text(json.dumps({
                'coverage_scope': 'manager_corpus', 'source': 'rawcover',
                'all_kernel_pc': 100, 'target_ext4_jbd2_pc': 0,
            }))
            report = MODULE.build_report(custom, upstream, 'ext4_jbd2')
            self.assertFalse(report['measurement_complete'])
            self.assertIn('inconclusive_upstream_target_execution', report['warnings'])

    def test_seed_execution_evidence_cannot_make_empty_coverage_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            custom = root / 'custom'
            upstream = root / 'upstream'
            (custom / 'dependencies').mkdir(parents=True)
            (upstream / 'coverage-checkpoints').mkdir(parents=True)
            (custom / 'campaign.json').write_text(json.dumps({
                'status': 'complete', 'stop_reason': 'duration_limit',
                'limits': {'target_scope': 'ext4_jbd2'},
            }))
            (custom / 'campaign-report.json').write_text(json.dumps({
                'coverage_complete': True, 'kcov_feedback_complete': True,
                'kcov_feedback': {'target_scope': 'ext4_jbd2',
                                  'corpus_coverage_scope': 'admitted_corpus',
                                  'corpus_target_pc_coverage': 12},
            }))
            (upstream / 'run-metadata.json').write_text(json.dumps({
                'status': 'passed', 'manager_exit_code': 0,
                'stopped_after_duration': True, 'coverage_endpoint_ready': True,
                'coverage_source': 'rawcover', 'seed_program': 'upstream-ext4.syz',
                'seed_program_applied': True, 'seed_program_loaded_count': 1,
                'manager_execution_activity_observed_after_seed_load': True,
                'target_filesystem_execution_verified': True,
                'seed_execution_evidence_status': 'passed',
            }))
            (upstream / 'coverage-checkpoints/final.json').write_text(json.dumps({
                'coverage_scope': 'manager_corpus', 'source': 'rawcover',
                'all_kernel_pc': 0, 'target_ext4_jbd2_pc': 0,
            }))
            report = MODULE.build_report(custom, upstream, 'ext4_jbd2')
            self.assertFalse(report['measurement_complete'])
            self.assertIn('upstream_coverage_data_empty', report['warnings'])

    def test_seeded_upstream_requires_seed_execution_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            custom = root / 'custom'
            upstream = root / 'upstream'
            custom.mkdir()
            (custom / 'campaign.json').write_text(json.dumps({
                'status': 'complete', 'stop_reason': 'duration_limit',
                'limits': {'target_scope': 'ext4_jbd2'},
            }))
            (custom / 'campaign-report.json').write_text(json.dumps({
                'coverage_complete': True, 'kcov_feedback_complete': True,
                'kcov_feedback': {'target_scope': 'ext4_jbd2',
                                  'corpus_coverage_scope': 'admitted_corpus',
                                  'corpus_target_pc_coverage': 12},
            }))
            (upstream / 'coverage-checkpoints').mkdir(parents=True)
            (upstream / 'run-metadata.json').write_text(json.dumps({
                'status': 'passed', 'manager_exit_code': 0,
                'stopped_after_duration': True, 'coverage_endpoint_ready': True,
                'coverage_source': 'rawcover', 'seed_program': 'upstream-ext4.syz',
                'seed_program_applied': False,
                'manager_execution_activity_observed_after_seed_load': False,
            }))
            (upstream / 'coverage-checkpoints/final.json').write_text(json.dumps({
                'coverage_scope': 'manager_corpus', 'source': 'rawcover',
                'all_kernel_pc': 100, 'target_ext4_jbd2_pc': 10,
            }))

            report = MODULE.build_report(custom, upstream, 'ext4_jbd2')
            self.assertFalse(report['measurement_complete'])
            self.assertIn('inconclusive_upstream_target_execution', report['warnings'])

    def test_three_arm_report_checks_image_identity_and_target_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            native = root / 'native'
            pc = root / 'pc-only'
            full = root / 'full'
            operations_pc = root / 'pc-operations.json'
            operations_full = root / 'full-operations.json'
            for campaign, feedback_mode, coverage in ((pc, 'pc', 12),
                                                       (full, 'pc_edge_semantic', 20)):
                (campaign / 'dependencies').mkdir(parents=True)
                (campaign / 'campaign.json').write_text(json.dumps({
                    'status': 'complete', 'stop_reason': 'duration_limit',
                    'collection_elapsed_seconds': 3600,
                    'limits': {'feedback_mode': feedback_mode, 'target_scope': 'ext4_jbd2',
                               'target_kind': 'regular', 'seed': 77, 'syz_calls': 16,
                               'resource_valid_every': 4, 'fresh_generate_every': 7,
                               'scheduler_policy': 'rarity_fair', 'mutation_budget_policy': 'fixed',
                               'target_syscalls': 0},
                    'dependencies': {'rootfs_sha256': 'root'},
                    'build_sha256': {'kernel.bzImage': 'kernel'},
                    'start_images': {'regular': {'sha256': 'shared-image'}},
                }))
                (campaign / 'campaign-report.json').write_text(json.dumps({
                    'passed': True, 'coverage_complete': True, 'kcov_feedback_complete': True,
                    'generated_syscalls': 16, 'native_verified': 16,
                    'kcov_feedback': {
                        'target_scope': 'ext4_jbd2', 'coverage_scope': 'all_complete_samples',
                        'corpus_coverage_scope': 'admitted_corpus', 'total_coverage': 100,
                        'target_pc_coverage': coverage, 'corpus_pc_coverage': 40,
                        'corpus_target_pc_coverage': coverage,
                    },
                }))
            (native / 'coverage-checkpoints').mkdir(parents=True)
            (native / 'run-metadata.json').write_text(json.dumps({
                'status': 'passed', 'manager_exit_code': 0,
                'stopped_after_duration': True, 'coverage_endpoint_ready': True,
                'coverage_source': 'rawcover', 'requested_syscalls': 24,
                'actual_enabled_syscall_count': 151, 'seed_program_applied': True,
                'seed_program_loaded_count': 1,
                'manager_execution_activity_observed_after_seed_load': True,
                'target_ext4_image_sha256': 'shared-image',
                'rootfs_backing': str(root / 'rootfs'), 'kernel': str(root / 'kernel'),
            }))
            (native / 'coverage-checkpoints/final.json').write_text(json.dumps({
                'coverage_scope': 'manager_corpus', 'source': 'rawcover',
                'all_kernel_pc': 80, 'ext4_pc': 10, 'jbd2_pc': 5,
                'target_ext4_jbd2_pc': 15,
            }))
            # The native hashes above are read from paths. Use real common files
            # so the report can assert rootfs/kernel parity.
            rootfs = root / 'rootfs'
            kernel = root / 'kernel'
            rootfs.write_text('shared rootfs')
            kernel.write_text('shared kernel')
            for campaign in (pc, full):
                data = json.loads((campaign / 'campaign.json').read_text())
                data['dependencies']['rootfs_sha256'] = MODULE.sha256(rootfs)
                data['build_sha256']['kernel.bzImage'] = MODULE.sha256(kernel)
                (campaign / 'campaign.json').write_text(json.dumps(data))
            for path in (operations_pc, operations_full):
                path.write_text(json.dumps({
                    'status': 'passed', 'evidence_readable': True,
                    'evidence_complete': True, 'target_device': '253:16',
                    'operations': {'target_success_total': 3,
                        'required_data_and_namespace_categories_verified': True, 'data': {
                        'verified_successful_on_regular_unrepaired': 2}, 'namespace': {
                        'verified_successful_on_regular_unrepaired': 1}},
                }))

            report = MODULE.build_three_arm_report(native, pc, full, 'ext4_jbd2',
                                                   operations_pc, operations_full)
            self.assertTrue(report['measurement_complete'])
            self.assertTrue(report['fairness']['same_target_ext4_image_bytes'])
            self.assertTrue(report['fairness']['feedback_ablation_settings_aligned'])
            self.assertFalse(report['fairness']['strictly_causal_native_comparison'])
            self.assertEqual(report['coverage']['feedback_delta_full_minus_pc_only'], 8)
            self.assertIn('full_feedback', report['arms'])
            self.assertNotIn('pc_edge_semantic', report['arms'])
            self.assertIn('native_transitive_syscall_closure_differs', report['warnings'])

            # The comparison wrapper also permits edge-only or semantic-only
            # feedback as the full arm; the report must validate the selected
            # mode rather than silently hard-coding pc_edge_semantic.
            full_campaign = json.loads((full / 'campaign.json').read_text())
            full_campaign['limits']['feedback_mode'] = 'pc_edge'
            (full / 'campaign.json').write_text(json.dumps(full_campaign))
            edge_report = MODULE.build_three_arm_report(native, pc, full, 'ext4_jbd2',
                                                        operations_pc, operations_full,
                                                        'pc_edge')
            self.assertTrue(edge_report['fairness']['feedback_ablation_settings_aligned'])
            self.assertNotIn('custom_feedback_mode_mismatch', edge_report['warnings'])

            full_campaign['limits']['fresh_generate_every'] = 99
            (full / 'campaign.json').write_text(json.dumps(full_campaign))
            mismatched_report = MODULE.build_three_arm_report(native, pc, full, 'ext4_jbd2',
                                                              operations_pc, operations_full,
                                                              'pc_edge')
            self.assertFalse(mismatched_report['measurement_complete'])
            self.assertFalse(mismatched_report['fairness'][
                'custom_feedback_arm_settings_aligned_except_feedback_mode'])

    def test_three_arm_zero_native_target_pc_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            native = root / 'native'
            pc = root / 'pc-only'
            full = root / 'full'
            operations_pc = root / 'pc-operations.json'
            operations_full = root / 'full-operations.json'
            for campaign, feedback_mode in ((pc, 'pc'), (full, 'pc_edge_semantic')):
                (campaign / 'dependencies').mkdir(parents=True)
                (campaign / 'campaign.json').write_text(json.dumps({
                    'status': 'complete', 'stop_reason': 'duration_limit',
                    'limits': {'feedback_mode': feedback_mode, 'target_scope': 'ext4_jbd2',
                               'target_kind': 'regular'},
                    'dependencies': {'rootfs_sha256': 'root'},
                    'build_sha256': {'kernel.bzImage': 'kernel'},
                    'start_images': {'regular': {'sha256': 'shared-image'}},
                }))
                (campaign / 'campaign-report.json').write_text(json.dumps({
                    'passed': True, 'coverage_complete': True,
                    'kcov_feedback_complete': True,
                    'kcov_feedback': {'target_scope': 'ext4_jbd2',
                                      'corpus_coverage_scope': 'admitted_corpus',
                                      'corpus_target_pc_coverage': 12},
                }))
            (native / 'coverage-checkpoints').mkdir(parents=True)
            (native / 'run-metadata.json').write_text(json.dumps({
                'status': 'passed', 'manager_exit_code': 0,
                'stopped_after_duration': True, 'coverage_endpoint_ready': True,
                'coverage_source': 'rawcover', 'seed_program_applied': True,
                'seed_program_loaded_count': 1,
                'manager_execution_activity_observed_after_seed_load': True,
                'target_filesystem_execution_verified': True,
                'seed_execution_evidence_status': 'passed',
                'target_ext4_image_sha256': 'shared-image',
                'rootfs_backing': str(root / 'rootfs'), 'kernel': str(root / 'kernel'),
            }))
            (native / 'coverage-checkpoints/final.json').write_text(json.dumps({
                'coverage_scope': 'manager_corpus', 'source': 'rawcover',
                'all_kernel_pc': 80, 'target_ext4_jbd2_pc': 0,
            }))
            rootfs = root / 'rootfs'
            kernel = root / 'kernel'
            rootfs.write_text('shared rootfs')
            kernel.write_text('shared kernel')
            for campaign in (pc, full):
                data = json.loads((campaign / 'campaign.json').read_text())
                data['dependencies']['rootfs_sha256'] = MODULE.sha256(rootfs)
                data['build_sha256']['kernel.bzImage'] = MODULE.sha256(kernel)
                (campaign / 'campaign.json').write_text(json.dumps(data))
            for path in (operations_pc, operations_full):
                path.write_text(json.dumps({
                    'status': 'passed', 'evidence_readable': True,
                    'evidence_complete': True,
                    'operations': {'target_success_total': 3,
                                   'data': {'verified_successful_on_regular_unrepaired': 2},
                                   'namespace': {'verified_successful_on_regular_unrepaired': 1}},
                }))

            report = MODULE.build_three_arm_report(native, pc, full, 'ext4_jbd2',
                                                   operations_pc, operations_full)
            self.assertFalse(report['measurement_complete'])
            self.assertIn('native_coverage_data_empty', report['warnings'])
            self.assertTrue(report['fairness']['native_seed_and_execution_gate_satisfied'])
            self.assertFalse(report['fairness']['native_target_coverage_gate_satisfied'])


if __name__ == '__main__':
    unittest.main()
