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
            (custom / 'dependencies').mkdir(parents=True)
            (upstream / 'coverage-checkpoints').mkdir(parents=True)
            (custom / 'campaign.json').write_text(json.dumps({
                'status': 'complete',
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
            self.assertFalse(report['fairness']['strictly_causal_comparison'])


if __name__ == '__main__':
    unittest.main()
