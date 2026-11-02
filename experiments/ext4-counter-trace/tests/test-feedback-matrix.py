#!/usr/bin/env python3
"""The long feedback matrix starts isolated campaigns and rejects bad evidence."""
import csv
import json
import os
import pathlib
import signal
import subprocess
import tempfile
import time
import unittest


PROJECT = pathlib.Path(__file__).resolve().parents[1]
MATRIX = PROJECT / 'scripts/run-feedback-long-matrix.sh'


class FeedbackMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.runs = self.root / 'fake-runs'
        self.runs.mkdir()
        self.runner = self.root / 'fake-runner.sh'
        self.runner.write_text(r'''#!/usr/bin/env bash
set -euo pipefail
mode=""; port=""; duration=""; target="0"; strict=false
while (($#)); do
  case "$1" in
    --feedback-mode) mode=$2; shift 2 ;;
    --port) port=$2; shift 2 ;;
    --duration-seconds) duration=$2; shift 2 ;;
    --target-syscalls) target=$2; shift 2 ;;
    --strict-kcov-feedback) strict=true; shift ;;
    *) shift ;;
  esac
done
python3 - "$mode" "$port" "$duration" "$target" "$strict" <<'PY'
import fcntl, json, os, pathlib, sys, tempfile, time
mode, port, duration, target, strict = sys.argv[1:]
base = pathlib.Path(os.environ['FAKE_RUNS_DIR'])
run = pathlib.Path(tempfile.mkdtemp(prefix='campaign-', dir=base))
seconds = int(duration)
bad = os.environ.get('FAKE_BAD_MODE') == mode
delays = json.loads(os.environ.get('FAKE_DELAYS', '{}'))
time.sleep(float(delays.get(mode, 0.1)))
with (base / 'active.lock').open('a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    active_path = base / 'active-count'
    active = int(active_path.read_text()) if active_path.exists() else 0
    active += 1
    active_path.write_text(str(active))
    maximum_path = base / 'maximum-active-count'
    maximum = int(maximum_path.read_text()) if maximum_path.exists() else 0
    maximum_path.write_text(str(max(maximum, active)))
time.sleep(0.1)
with (base / 'active.lock').open('a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    active_path = base / 'active-count'
    active_path.write_text(str(int(active_path.read_text()) - 1))
target = int(target)
meta = {'status': 'complete', 'limits': {'feedback_mode': mode,
        'target_syscalls': target,
        'strict_kcov_feedback': strict == 'true'}}
report = {'passed': not bad, 'kcov_feedback_complete': not bad,
          'coverage_complete': True, 'stop_reason': 'duration_limit',
          'collection_elapsed_seconds': seconds, 'segments': 3,
          'complete_segments': 3, 'kcov_feedback_gaps': [] if not bad else
          [{'segment_id': 1, 'errors': ['saturated']} ]}
if target:
    report['stop_reason'] = 'syscall_budget'
    report['generated_syscalls'] = target
    verified = int(os.environ.get('FAKE_VERIFIED_SYSCALLS', target))
    report['verified_syscalls'] = verified
    report['native_verified'] = verified
(run / 'campaign.json').write_text(json.dumps(meta))
(run / 'campaign-report.json').write_text(json.dumps(report))
(base / f'invocation-{mode}.json').write_text(json.dumps(
    {'mode': mode, 'port': int(port), 'duration': seconds,
     'strict': strict == 'true'}))
print(f'run_dir={run}', flush=True)
raise SystemExit(1 if bad else 0)
PY
''')
        self.runner.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, bad_mode=None, delays=None):
        output = self.root / 'matrix-output'
        env = os.environ.copy()
        env['FEEDBACK_MATRIX_RUNNER'] = str(self.runner)
        env['FAKE_RUNS_DIR'] = str(self.runs)
        if bad_mode:
            env['FAKE_BAD_MODE'] = bad_mode
        if delays:
            env['FAKE_DELAYS'] = json.dumps(delays)
        port_base = 34000 + (os.getpid() % 2000)
        result = subprocess.run(
            ['bash', str(MATRIX), '--duration-seconds', '30', '--output', str(output),
             '--port-base', str(port_base), '--startup-stagger-seconds', '0',
             '--min-memory-mb', '0', '--min-free-mb', '0', '--max-run-mb', '512'],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, timeout=30)
        return result, output, port_base

    def test_summary_records_actual_completion_order(self):
        result, output, _ = self.invoke(delays={
            'pc': 0.35, 'pc_edge': 0.25, 'pc_semantic': 0.05,
            'pc_edge_semantic': 0.15})
        self.assertEqual(result.returncode, 0, result.stdout)
        with (output / 'summary.tsv').open() as stream:
            rows = list(csv.DictReader(stream, delimiter='\t'))
        self.assertEqual([row['configuration'] for row in rows],
                         ['pc', 'pc_edge', 'pc_semantic', 'pc_edge_semantic'])
        self.assertEqual({row['configuration']: int(row['completion_order']) for row in rows},
                         {'pc_semantic': 1, 'pc_edge_semantic': 2, 'pc_edge': 3, 'pc': 4})

    def test_sigterm_keeps_all_campaign_rows_and_returns_after_children_stop(self):
        output = self.root / 'signal-output'
        env = os.environ.copy()
        env['FEEDBACK_MATRIX_RUNNER'] = str(self.runner)
        env['FAKE_RUNS_DIR'] = str(self.runs)
        env['FAKE_DELAYS'] = json.dumps({mode: 5 for mode in
                                         ('pc', 'pc_edge', 'pc_semantic', 'pc_edge_semantic')})
        port_base = 36000 + (os.getpid() % 1000)
        process = subprocess.Popen(
            ['bash', str(MATRIX), '--duration-seconds', '30', '--output', str(output),
             '--port-base', str(port_base), '--startup-stagger-seconds', '0',
             '--stop-grace-seconds', '2', '--min-memory-mb', '0', '--min-free-mb', '0',
             '--max-run-mb', '512'],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        time.sleep(0.25)
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 1, stdout)
        with (output / 'summary.tsv').open() as stream:
            rows = list(csv.DictReader(stream, delimiter='\t'))
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row['validation'] == 'stopped_by_signal' for row in rows), rows)

    def test_runs_four_modes_concurrently_with_distinct_ports_and_strict_feedback(self):
        result, output, port_base = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout)
        with (output / 'summary.tsv').open() as stream:
            rows = list(csv.DictReader(stream, delimiter='\t'))
        self.assertEqual([row['configuration'] for row in rows],
                         ['pc', 'pc_edge', 'pc_semantic', 'pc_edge_semantic'])
        self.assertEqual([int(row['port']) for row in rows], list(range(port_base, port_base + 4)))
        self.assertTrue(all(row['validation'] == 'passed' and row['exit_code'] == '0'
                            for row in rows))
        self.assertEqual((self.runs / 'maximum-active-count').read_text(), '4')
        for mode in ('pc', 'pc_edge', 'pc_semantic', 'pc_edge_semantic'):
            invocation = json.loads((self.runs / f'invocation-{mode}.json').read_text())
            self.assertTrue(invocation['strict'])
            self.assertEqual(invocation['duration'], 30)

    def test_saturated_feedback_marks_matrix_failed_even_when_runner_exits_zero(self):
        result, output, _ = self.invoke('pc_semantic')
        self.assertEqual(result.returncode, 1, result.stdout)
        with (output / 'summary.tsv').open() as stream:
            rows = {row['configuration']: row for row in csv.DictReader(stream, delimiter='\t')}
        self.assertEqual(rows['pc_semantic']['validation'], 'report_or_feedback_incomplete')
        self.assertEqual(rows['pc_semantic']['exit_code'], '1')
        self.assertEqual(rows['pc']['validation'], 'passed')

    def test_failed_campaign_does_not_terminate_other_modes(self):
        output = self.root / 'one-failure-output'
        env = os.environ.copy()
        env['FEEDBACK_MATRIX_RUNNER'] = str(self.runner)
        env['FAKE_RUNS_DIR'] = str(self.runs)
        env['FAKE_BAD_MODE'] = 'pc_edge'
        env['FAKE_DELAYS'] = json.dumps({
            'pc_edge': 0.05, 'pc': 0.5, 'pc_semantic': 0.5,
            'pc_edge_semantic': 0.5})
        port_base = 42000 + (os.getpid() % 1000)
        result = subprocess.run(
            ['bash', str(MATRIX), '--duration-seconds', '30', '--output', str(output),
             '--port-base', str(port_base), '--startup-stagger-seconds', '0',
             '--min-memory-mb', '0', '--min-free-mb', '0', '--max-run-mb', '512'],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout)
        with (output / 'summary.tsv').open() as stream:
            rows = {row['configuration']: row for row in csv.DictReader(stream, delimiter='\t')}
        self.assertEqual(set(rows), {'pc', 'pc_edge', 'pc_semantic', 'pc_edge_semantic'})
        self.assertEqual(rows['pc_edge']['validation'], 'report_or_feedback_incomplete')
        self.assertTrue(all(rows[mode]['validation'] == 'passed' for mode in
                            ('pc', 'pc_semantic', 'pc_edge_semantic')), rows)
        self.assertIn('continuing remaining campaigns independently', result.stdout)

    def test_fixed_syscall_budget_is_forwarded_and_validated(self):
        # Use an exact budget and a short duration; validation must use
        # syscall_budget rather than requiring the duration to elapse.
        output = self.root / 'budget-output'
        env = os.environ.copy()
        env['FEEDBACK_MATRIX_RUNNER'] = str(self.runner)
        env['FAKE_RUNS_DIR'] = str(self.runs)
        port_base = 38000 + (os.getpid() % 1000)
        result = subprocess.run(
            ['bash', str(MATRIX), '--duration-seconds', '30', '--target-syscalls', '48',
             '--output', str(output), '--port-base', str(port_base),
             '--startup-stagger-seconds', '0', '--min-memory-mb', '0', '--min-free-mb', '0',
             '--max-run-mb', '512'], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        with (output / 'summary.tsv').open() as stream:
            rows = list(csv.DictReader(stream, delimiter='\t'))
        self.assertTrue(all(row['validation'] == 'passed' for row in rows), rows)
        self.assertTrue(all(row['target_syscalls'] == '48' for row in rows), rows)

    def test_fixed_syscall_budget_rejects_verified_count_below_target(self):
        output = self.root / 'under-budget-output'
        env = os.environ.copy()
        env['FEEDBACK_MATRIX_RUNNER'] = str(self.runner)
        env['FAKE_RUNS_DIR'] = str(self.runs)
        env['FAKE_VERIFIED_SYSCALLS'] = '47'
        port_base = 40000 + (os.getpid() % 1000)
        result = subprocess.run(
            ['bash', str(MATRIX), '--duration-seconds', '30', '--target-syscalls', '48',
             '--output', str(output), '--port-base', str(port_base),
             '--startup-stagger-seconds', '0', '--min-memory-mb', '0', '--min-free-mb', '0',
             '--max-run-mb', '512'], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=env, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout)
        with (output / 'summary.tsv').open() as stream:
            rows = list(csv.DictReader(stream, delimiter='\t'))
        self.assertTrue(all(row['validation'] == 'report_or_feedback_incomplete' for row in rows), rows)

    def test_busy_ssh_port_aborts_before_starting_any_campaign(self):
        import socket
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        port_base = sock.getsockname()[1]
        output = self.root / 'busy-port-output'
        env = os.environ.copy()
        env['FEEDBACK_MATRIX_RUNNER'] = str(self.runner)
        env['FAKE_RUNS_DIR'] = str(self.runs)
        try:
            result = subprocess.run(
                ['bash', str(MATRIX), '--duration-seconds', '30', '--output', str(output),
                 '--port-base', str(port_base), '--startup-stagger-seconds', '0',
                 '--min-memory-mb', '0', '--min-free-mb', '0', '--max-run-mb', '512'],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, timeout=30)
        finally:
            sock.close()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('SSH port', result.stdout)
        self.assertFalse(list(self.runs.glob('invocation-*.json')))

    def test_matrix_default_run_quota_has_30k_headroom(self):
        source = MATRIX.read_text()
        self.assertIn('MAX_RUN_MB=9216', source)
        self.assertIn('Per-campaign logical output cap (default: 9216)', source)


if __name__ == '__main__':
    unittest.main()
