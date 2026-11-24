#!/usr/bin/env python3
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'summarize-feedback-ablation.py'
ABLATION_RUNNER = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'run-feedback-ablation.sh'
ARM_RUNNER = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'run-feedback-ablation-arm.sh'
PROJECT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('summarize_feedback_ablation', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def make_arm(root, mode):
    run = root / mode
    run.mkdir()
    limits = {
        'seed': 99, 'duration_seconds': 7200, 'target_syscalls': 0,
        'target_kind': 'regular', 'target_scope': 'ext4_jbd2',
        'syz_calls': 16, 'resource_valid_every': 1, 'fresh_generate_every': 0,
        'resource_seed_refresh_every': 4, 'resource_mutation_mode': 'combined',
        'scheduler_policy': 'rarity_fair', 'mutation_budget_policy': 'fixed',
        'scenario': 'baseline', 'segment_seconds': 1, 'max_batches': 1,
        'batch_timeout': 30, 'workers': 1, 'memory_mb': 0,
        'guest_memory_mb': 1024, 'cpus': 1, 'qemu_cpu': 'qemu64',
        'buffer_kb': 65536, 'max_trace_mb': 64, 'max_run_mb': 40960,
        'min_free_mb': 1024, 'strict_kcov_feedback': True,
        'effect_aware_admission': True, 'require_target_success': False,
        'feedback_mode': mode,
    }
    (run / 'campaign.json').write_text(json.dumps({
        'status': 'complete', 'stop_reason': 'duration_limit', 'limits': limits,
        'dependencies': {'rootfs_sha256': 'root-hash'},
        'build_sha256': {'kernel.bzImage': 'kernel-hash'},
        'start_images': {'regular': {'sha256': 'image-hash'}},
        'elapsed_seconds': 7300, 'collection_elapsed_seconds': 7200,
    }))
    (run / 'campaign-report.json').write_text(json.dumps({
        'passed': True, 'coverage_complete': True, 'raw_coverage_complete': True,
        'kcov_feedback_complete': True, 'counter_validation_passed': True,
        'issues': [], 'generated_syscalls': 1600, 'native_verified': 1600,
        'native_success': 1000, 'data_success': 20,
        'performance': {'all': {'syscalls_per_second': 0.5}},
        'exploration': {'independent_transition_calls': 20,
                        'successful_namespace_operations': 10},
        'kcov_feedback': {
            'expected_samples': 100, 'complete_samples': 100,
            'missing_samples': 0, 'invalid_samples': 0, 'saturated_samples': 0,
            'target_scope': 'ext4_jbd2', 'feedback_edge_scope': 'ext4_jbd2',
            'effect_target_pc_coverage': 123,
            'corpus_target_pc_coverage': 120, 'target_pc_coverage': 125,
            'effect_target_edge_coverage': 130, 'total_semantic_features': 20,
            'call_effect_counts': {'target_success_unmodified': 100},
        },
    }))
    return run


class FeedbackAblationSummaryTests(unittest.TestCase):
    def test_complete_four_arm_configuration_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            runs = {mode: make_arm(root, mode) for mode in MODULE.MODES}
            report = MODULE.build_report(runs, 99, 7200, 'ext4_jbd2')
            self.assertTrue(report['measurement_complete'])
            self.assertEqual(report['deltas_vs_pc']['pc_edge']['effect_target_pc_delta_vs_pc'], 0)

    def test_mismatched_image_makes_measurement_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            runs = {mode: make_arm(root, mode) for mode in MODULE.MODES}
            campaign_path = runs['pc_edge'] / 'campaign.json'
            campaign = json.loads(campaign_path.read_text())
            campaign['start_images']['regular']['sha256'] = 'different-image'
            campaign_path.write_text(json.dumps(campaign))
            report = MODULE.build_report(runs, 99, 7200, 'ext4_jbd2')
            self.assertFalse(report['measurement_complete'])
            self.assertIn('arms_regular_image_sha256_mismatch', report['warnings'])


class FeedbackAblationRunnerTests(unittest.TestCase):
    def test_help_documents_timeout_and_failure_policy(self):
        result = subprocess.run(['bash', str(ABLATION_RUNNER), '--help'],
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--batch-timeout N', result.stdout)
        self.assertIn('--fail-fast', result.stdout)

    def test_batch_timeout_validation_uses_campaign_runner_bounds(self):
        for value in ('0', '301', 'not-a-number'):
            with self.subTest(value=value):
                result = subprocess.run(['bash', str(ABLATION_RUNNER),
                                         '--batch-timeout', value],
                                        text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 2)
                self.assertIn('batch_timeout must be an integer in [1,300]',
                              result.stderr)

    def test_arm_forwards_batch_timeout_and_logs_runner_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            fake_runner = root / 'fake-runner.sh'
            fake_runner.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n',
                                   encoding='utf-8')
            fake_runner.chmod(fake_runner.stat().st_mode | 0o111)
            log = root / 'pc.log'
            result = subprocess.run([
                'bash', str(ARM_RUNNER), 'pc', '12481', '/tmp/rootfs.img',
                '/tmp/seed.raw', '99', '43200', str(log), str(fake_runner),
                'ext4_jbd2', '100000', '0', '90',
            ], text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            saved = log.read_text(encoding='utf-8')
            self.assertIn('[pc] --batch-timeout\n[pc] 90\n', saved)
            self.assertNotIn('[pc] 30\n', saved)

    def test_one_failed_arm_does_not_cancel_other_arms_by_default(self):
        rootfs = PROJECT.parents[1] / 'work' / 'debian-normal.img'
        kernel_obj = PROJECT / 'build' / 'linux-counter-trace-normal'
        ssh_key = PROJECT.parents[1] / 'work' / 'debian.id_rsa'
        tools = ('python3', 'qemu-img', 'mke2fs', 'debugfs', 'sha256sum',
                 'ssh', 'scp', 'setsid', 'awk', 'tee', 'stdbuf')
        if not (rootfs.is_file() and (kernel_obj / 'arch/x86/boot/bzImage').is_file()
                and (kernel_obj / 'vmlinux').is_file() and ssh_key.is_file()
                and all(shutil.which(tool) for tool in tools)):
            self.skipTest('project campaign prerequisites are not available')

        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            fake_runner = root / 'fake-runner.sh'
            fake_runner.write_text(
                '#!/usr/bin/env bash\n'
                'mode=\nwhile (($#)); do\n'
                '  if [[ "$1" == --feedback-mode ]]; then mode=$2; shift 2; else shift; fi\n'
                'done\n'
                'mkdir -p "$TEST_RUN_ROOT/$mode"\n'
                'printf "run_dir=%s\\n" "$TEST_RUN_ROOT/$mode"\n'
                'if [[ "$mode" == pc ]]; then exit 42; fi\n'
                'sleep 0.3\n', encoding='utf-8')
            fake_runner.chmod(fake_runner.stat().st_mode | 0o111)
            output_dir = root / 'output'
            env = dict(os.environ,
                       FEEDBACK_ABLATION_RUNNER=str(fake_runner),
                       TEST_RUN_ROOT=str(root / 'campaigns'))
            result = subprocess.run([
                'bash', str(ABLATION_RUNNER), '--duration-seconds', '1',
                '--seed', '99', '--batch-timeout', '90', '--output', str(output_dir),
            ], text=True, capture_output=True, check=False, env=env, timeout=30)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('remaining arms will continue', result.stderr)
            metadata = (output_dir / 'launch-metadata.txt').read_text(encoding='utf-8')
            self.assertIn('batch_timeout_seconds=90', metadata)
            self.assertIn('pc_exit_code=42', metadata)
            for mode in MODULE.MODES[1:]:
                self.assertIn(f'{mode}_exit_code=0', metadata)
            self.assertTrue((output_dir / 'ablation.json').is_file())


if __name__ == '__main__':
    unittest.main()
