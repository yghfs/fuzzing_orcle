#!/usr/bin/env python3
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from performance_report import analysis_timings_from_progress, build


class PerformanceReportTests(unittest.TestCase):
    def test_progress_log_attributes_analysis_to_the_named_segment(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            (run / 'progress.log').write_text(
                '[2026-11-12 10:00:00.000+00:00] ANALYSIS START segment=segment-000000 salvage=False\n'
                '[2026-11-12 10:00:02.250+00:00] ANALYSIS END segment=segment-000000 kind=regular\n'
                '[2026-11-12 10:00:03.000+00:00] ANALYSIS START segment=segment-000001 salvage=False\n'
                '[2026-11-12 10:00:04.000+00:00] ANALYSIS END segment=segment-000001 kind=regular\n')
            self.assertEqual(analysis_timings_from_progress(run), {
                'segment-000000': {'seconds': 2.25, 'calls': 1},
                'segment-000001': {'seconds': 1.0, 'calls': 1},
            })

    def test_report_includes_nested_guest_phases_and_campaign_stages(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            (run / 'campaign.json').write_text(json.dumps({
                'run_id': 'campaign-test', 'stage_timings': {
                    'prepare': {'seconds': 3.0, 'calls': 1},
                },
            }))
            (run / 'segment-summary.jsonl').write_text(json.dumps({
                'segment_id': 0,
                'segment_elapsed_seconds': 20.0,
                'native_verified': 16,
                'stage_timings': {
                    'guest_execution': {'seconds': 12.0, 'calls': 1},
                    'workload': {'seconds': 6.0, 'calls': 1},
                    'snapshot_one': {'seconds': 3.0, 'calls': 6},
                    # Deliberately wrong legacy value: progress.log wins.
                    'analysis': {'seconds': 99.0, 'calls': 1},
                },
            }) + '\n')
            (run / 'progress.log').write_text(
                '[2026-11-12 10:00:00.000+00:00] ANALYSIS START segment=segment-000000 salvage=False\n'
                '[2026-11-12 10:00:02.500+00:00] ANALYSIS END segment=segment-000000 kind=regular\n')
            report = build(run)
            self.assertEqual(report['stages']['analysis']['total'], 2.5)
            self.assertEqual(report['stages']['workload']['p50'], 6.0)
            self.assertEqual(report['stages']['snapshot_one']['p50'], 3.0)
            self.assertEqual(report['campaign_stages']['prepare']['seconds'], 3.0)
            self.assertIn('workload', report['nested_stage_groups']['guest_execution_contains'])

    def test_report_repairs_the_legacy_previous_segment_analysis_shift(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            (run / 'campaign.json').write_text(json.dumps({
                'run_id': 'legacy-shift', 'stage_timings': {
                    'analysis': {'seconds': 9.0, 'calls': 3},
                },
            }))
            rows = [
                {'segment_id': 0, 'segment_elapsed_seconds': 1, 'native_verified': 1,
                 'stage_timings': {}},
                {'segment_id': 1, 'segment_elapsed_seconds': 1, 'native_verified': 1,
                 'stage_timings': {'analysis': {'seconds': 2.0, 'calls': 1}}},
                {'segment_id': 2, 'segment_elapsed_seconds': 1, 'native_verified': 1,
                 'stage_timings': {'analysis': {'seconds': 3.0, 'calls': 1}}},
            ]
            (run / 'segment-summary.jsonl').write_text(
                ''.join(json.dumps(row) + '\n' for row in rows))
            report = build(run)
            self.assertEqual(report['stages']['analysis']['total'], 9.0)
            self.assertEqual(report['stages']['analysis']['count'], 3)
            self.assertEqual(report['stages']['analysis']['p50'], 3.0)


if __name__ == '__main__':
    unittest.main()
