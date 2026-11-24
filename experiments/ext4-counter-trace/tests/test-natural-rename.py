#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


PROJECT = pathlib.Path(__file__).resolve().parents[1]
SNAPSHOT = dict(valid=True, namespace_valid=True, relations_valid=True,
                observed_mismatches=0, namespace_mismatched=0, relations_mismatches=0,
                free_clusters=123, free_inodes=456, dirs=7)


class NaturalRenameSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.binary = pathlib.Path(cls.temp.name) / 'natural-rename'
        subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-o', str(cls.binary), str(PROJECT / 'tests/natural-rename-stress.c')],
                       check=True)

    def test_snapshot_preserves_authoritative_values(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = pathlib.Path(temp) / 'source', pathlib.Path(temp) / 'snapshot'
            source.write_text(json.dumps(SNAPSHOT, separators=(',', ':')))
            result = subprocess.run(
                [str(self.binary), '--stable-snapshot', str(target), '1'],
                env=dict(os.environ, NATURAL_RENAME_SNAPSHOT=str(source)), timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_unverified_snapshot_remains_unverified(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = pathlib.Path(temp) / 'source', pathlib.Path(temp) / 'snapshot'
            source.write_text(json.dumps(dict(SNAPSHOT, valid=False)))
            result = subprocess.run(
                [str(self.binary), '--stable-snapshot', str(target), '0'],
                env=dict(os.environ, NATURAL_RENAME_SNAPSHOT=str(source)), timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(json.loads(target.read_text())['valid'])

    def test_missing_endpoint_does_not_create_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            target = pathlib.Path(temp) / 'snapshot'
            result = subprocess.run(
                [str(self.binary), '--stable-snapshot', str(target), '0'],
                env=dict(os.environ, NATURAL_RENAME_SNAPSHOT=str(target / 'missing')),
                timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())

    def test_usage_requires_mount_path(self):
        result = subprocess.run([str(self.binary)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('mount-path', result.stderr)


class NaturalRenameSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = pathlib.Path(self.temp.name)
        self.evidence = self.run / 'evidence'
        self.evidence.mkdir()
        self.result = self.evidence / 'workload-result.json'
        self.result.write_text(json.dumps(dict(completed=True, rename_calls=7)))
        (self.run / 'jbd2-enospc-grep.txt').touch()
        (self.run / 'run-status.txt').write_text(
            'guest_rc=0\nfsck_before_rc=0\nfsck_rc=0\ndisk_before_rc=0\ndisk_rc=0\n')
        (self.evidence / 'run-status.txt').write_text(
            'workload_rc=0\nafter_snapshot_rc=0\nunmount_rc=0\nremount_rc=0\n'
            'remount_umount_rc=0\ntrace_loss=0\nworkers=1\n')
        for name in ('stable-before', 'stable-oracle', 'stable-after', 'remount'):
            (self.evidence / (name + '.json')).write_text(json.dumps(SNAPSHOT))

    def summarize(self):
        runner = (PROJECT / 'scripts/run-natural-rename-stress.sh').read_text()
        script = runner.split('python3 - "$run" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
        result = subprocess.run([sys.executable, '-', str(self.run)], input=script,
                                capture_output=True, text=True, check=False)
        return result.returncode, json.loads((self.run / 'natural-summary.json').read_text())

    def test_complete_run_passes(self):
        code, report = self.summarize()
        self.assertEqual(code, 0)
        self.assertTrue(report['passed'])

    def test_missing_workload_result_fails(self):
        self.result.unlink()
        code, report = self.summarize()
        self.assertNotEqual(code, 0)
        self.assertFalse(report['passed'])

    def test_guest_failure_fails(self):
        (self.run / 'run-status.txt').write_text('guest_rc=71\n')
        code, report = self.summarize()
        self.assertNotEqual(code, 0)
        self.assertFalse(report['passed'])

    def test_missing_snapshot_fails(self):
        (self.evidence / 'remount.json').unlink()
        code, report = self.summarize()
        self.assertNotEqual(code, 0)
        self.assertFalse(report['snapshot_valid']['remount'])

    def test_observed_mismatch_fails(self):
        (self.evidence / 'stable-after.json').write_text(
            json.dumps(dict(SNAPSHOT, observed_mismatches=1)))
        code, report = self.summarize()
        self.assertNotEqual(code, 0)
        self.assertFalse(report['snapshot_valid']['stable-after'])

    def test_trace_loss_fails(self):
        path = self.evidence / 'run-status.txt'
        path.write_text(path.read_text().replace('trace_loss=0', 'trace_loss=1'))
        code, report = self.summarize()
        self.assertNotEqual(code, 0)
        self.assertFalse(report['passed'])


if __name__ == '__main__':
    unittest.main()
