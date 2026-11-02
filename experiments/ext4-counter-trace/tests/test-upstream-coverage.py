#!/usr/bin/env python3
import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'collect-upstream-coverage.py'
SPEC = importlib.util.spec_from_file_location('collect_upstream_coverage', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class UpstreamCoverageTests(unittest.TestCase):
    def test_symbol_ranges_use_text_boundaries_and_target_only(self):
        ranges = MODULE.parse_symbol_lines([
            '100 T generic_entry\n',
            '110 T ext4_open\n',
            '120 D ext4_data\n',
            '130 t __jbd2_commit\n',
            '140 T __traceiter_jbd2_commit\n',
            '150 T generic_exit\n',
        ])
        self.assertEqual(ranges, [(0x110, 0x130, 'ext4'), (0x130, 0x140, 'jbd2')])

    def test_cover_jsonl_uses_source_paths(self):
        data = b''.join([
            b'{"pc":1,"file_path":"fs/ext4/inode.c","hit_count":2}\n',
            b'{"pc":2,"file_path":"fs/jbd2/commit.c","hit_count":1}\n',
            b'{"pc":3,"file_path":"fs/ext4/inode.c","hit_count":4}\n',
            b'{"pc":4,"file_path":"fs/xfs/xfs.c","hit_count":1}\n',
            b'{"pc":5,"file_path":"fs/ext4/never.c","hit_count":0}\n',
            b'{"pc":6,"file_path":"fs/jbd2/never.c","hit_count":0}\n',
        ])
        all_pcs, ext4, jbd2, files = MODULE.read_records(data, 'cover-jsonl', [])
        self.assertEqual(all_pcs, {1, 2, 3, 4})
        self.assertEqual(ext4, {1, 3})
        self.assertEqual(jbd2, {2})
        self.assertNotIn('fs/ext4/never.c', files)
        self.assertNotIn('fs/jbd2/never.c', files)

    def test_rawcover_falls_back_to_symbol_ranges(self):
        ranges = MODULE.parse_symbol_lines([
            '100 T generic_entry\n',
            '110 T _ext4_open\n',
            '120 T __jbd2_commit\n',
            '130 T generic_exit\n',
        ])
        all_pcs, ext4, jbd2, _files = MODULE.read_records(
            b'0x111\n0x121\n0x129\n', 'rawcover', ranges)
        self.assertEqual(all_pcs, {0x111, 0x121, 0x129})
        self.assertEqual(ext4, {0x111})
        self.assertEqual(jbd2, {0x121, 0x129})


if __name__ == '__main__':
    unittest.main()
