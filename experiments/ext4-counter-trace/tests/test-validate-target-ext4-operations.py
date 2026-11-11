#!/usr/bin/env python3
import gzip
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO


SCRIPT = (pathlib.Path(__file__).resolve().parents[1] / 'scripts' /
          'validate-target-ext4-operations.py')
SPEC = importlib.util.spec_from_file_location('validate_target_ext4_operations', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TargetExt4OperationValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.campaign = self.root / 'campaign'
        self.segments = self.campaign / 'segments'
        self.segments.mkdir(parents=True)
        (self.campaign / 'campaign.json').write_text(json.dumps({
            'status': 'complete',
            'devices': {'regular': '253,16', 'bigalloc': '253,32', 'inline': '253,48'},
        }))

    def tearDown(self):
        self.temp.cleanup()

    def write_segment(self, index, calls, *, compressed=False, passed=True, syscall_issues=None):
        segment = self.segments / f'segment-{index:06d}'
        lifecycle = segment / 'analysis' / 'lifecycle'
        lifecycle.mkdir(parents=True)
        (segment / 'manifest.json').write_text(json.dumps({'segment_id': index, 'kind': 'regular'}))
        report = json.dumps({
            'passed': passed,
            'native_syscalls': calls,
            'syscall_issues': syscall_issues or [],
        }).encode()
        path = lifecycle / ('report.json.gz' if compressed else 'report.json')
        if compressed:
            with gzip.open(path, 'wb') as stream:
                stream.write(report)
        else:
            path.write_bytes(report)
        return segment

    @staticmethod
    def call(name, *, verified=True, raw_result=0, repaired=0, object_dev=None):
        return {
            'name': name,
            'verified': verified,
            'raw_result': raw_result,
            'repaired': repaired,
            'object_dev': os.makedev(253, 16) if object_dev is None else object_dev,
        }

    def test_counts_only_verified_successes_on_regular_device_without_repairs(self):
        self.write_segment(0, [
            self.call('write'),
            self.call('renameat2', raw_result=0),
            self.call('fallocate', repaired=1),
            self.call('mkdirat', object_dev=os.makedev(253, 32)),
            self.call('unlinkat', raw_result=-2),
            self.call('pwrite64', verified=False),
            self.call('openat'),  # Outside the requested data/namespace sets.
        ])

        result = MODULE.validate(self.campaign)
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['evidence_readable'])
        self.assertFalse(result['evidence_complete'])
        self.assertEqual(result['target_device'], '253:16')
        self.assertEqual(result['operations']['data']['calls_seen'], 3)
        self.assertEqual(result['operations']['data']['verified_successful_on_regular_unrepaired'], 1)
        self.assertEqual(result['operations']['data']['rejections']['repaired'], 1)
        self.assertEqual(result['operations']['data']['rejections']['unverified'], 1)
        self.assertEqual(result['operations']['namespace']['calls_seen'], 3)
        self.assertEqual(result['operations']['namespace']['verified_successful_on_regular_unrepaired'], 1)
        self.assertEqual(result['operations']['namespace']['rejections']['device_mismatch'], 1)
        self.assertEqual(result['operations']['namespace']['rejections']['failed_return'], 1)
        self.assertEqual(result['operations']['target_success_total'], 2)

    def test_reads_compressed_lifecycle_reports_and_exposes_incomplete_evidence(self):
        self.write_segment(0, [self.call('fsync'), self.call('mkdirat')], compressed=True,
                           passed=False, syscall_issues=[{'reason': 'unmatched'}])
        result = MODULE.validate(self.campaign)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['segments']['reports_read'], 1)
        self.assertEqual(result['segments']['lifecycle_failed'], 1)
        self.assertFalse(result['evidence_complete'])
        self.assertIn('segment-000000:syscall_correlation_issues', result['warnings'])

    def test_requires_successful_data_and_namespace_operations(self):
        self.write_segment(0, [self.call('write')])
        result = MODULE.validate(self.campaign)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['operations']['data']['verified_successful_on_regular_unrepaired'], 1)
        self.assertEqual(result['operations']['namespace']['verified_successful_on_regular_unrepaired'], 0)
        self.assertFalse(result['operations']['required_data_and_namespace_categories_verified'])
        self.assertIn('no_verified_successful_unrepaired_namespace_operation_on_regular_device',
                      result['errors'])

    def test_missing_report_is_incomplete_and_prevents_success_status(self):
        segment = self.segments / 'segment-000000'
        segment.mkdir()
        (segment / 'manifest.json').write_text('{}')
        result = MODULE.validate(self.campaign)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['evidence_readable'])
        self.assertFalse(result['evidence_complete'])
        self.assertEqual(result['segments']['missing_reports'], 1)

    def test_unreadable_report_is_reported(self):
        segment = self.segments / 'segment-000000'
        lifecycle = segment / 'analysis' / 'lifecycle'
        lifecycle.mkdir(parents=True)
        (segment / 'manifest.json').write_text('{}')
        (lifecycle / 'report.json').write_text('{broken')
        result = MODULE.validate(self.campaign)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['evidence_readable'])
        self.assertEqual(result['segments']['unreadable_reports'], 1)

    def test_target_success_absence_fails_even_when_reports_are_readable(self):
        self.write_segment(0, [self.call('write', object_dev=os.makedev(253, 32))])
        result = MODULE.validate(self.campaign)
        self.assertTrue(result['evidence_readable'])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('no_verified_successful_unrepaired_file_operation_on_regular_device',
                      result['errors'])
        with redirect_stdout(StringIO()):
            self.assertEqual(MODULE.main(['--campaign', str(self.campaign)]), 1)


if __name__ == '__main__':
    unittest.main()
