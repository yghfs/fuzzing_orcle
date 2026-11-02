#!/usr/bin/env python3
import json
import hashlib
import importlib.util
import pathlib
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from kcov_feedback import (FeedbackCorpus, HEADER, MAGIC, SATURATED_PARSE_LIMIT,
                            classify_pcs, classify_target_pcs, parse_symbol_ranges, read_sample,
                            read_sample_details, read_sample_diagnostics,
                            read_sample_features,
                            sample_count)

audit_path = pathlib.Path(__file__).resolve().parents[1] / 'scripts/audit-kcov-saturation.py'
audit_spec = importlib.util.spec_from_file_location('audit_kcov_saturation', audit_path)
audit_module = importlib.util.module_from_spec(audit_spec)
audit_spec.loader.exec_module(audit_module)


class SymbolRangeTests(unittest.TestCase):
    def test_target_ranges_keep_text_symbols_and_exclude_data_helpers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'kallsyms'
            path.write_text(
                'ffffffff81000000 T _stext\n'
                'ffffffff81001000 T ext4_file_open\n'
                'ffffffff81001020 D ext4_data_symbol\n'
                'ffffffff81002000 t __jbd2_journal_file_buffer\n'
                'ffffffff81002020 T __traceiter_jbd2_commit\n'
                'ffffffff81003000 T generic_kernel_fn\n')
            ranges = parse_symbol_ranges(path)
            names = {name for _start, _end, name in ranges}
            self.assertIn('ext4_file_open', names)
            self.assertIn('__jbd2_journal_file_buffer', names)
            self.assertNotIn('ext4_data_symbol', names)
            self.assertNotIn('__traceiter_jbd2_commit', names)
            result = classify_target_pcs(
                [0xffffffff81001010, 0xffffffff81002010, 0xffffffff81003000], ranges)
            self.assertEqual(result['ext4_unique'], 1)
            self.assertEqual(result['jbd2_unique'], 1)
            self.assertEqual(result['target_unique'], 2)

    def test_ranges_match_kcov_dekaslr_addresses(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'kallsyms'
            path.write_text(
                'ffffffff9f600000 T _stext\n'
                'ffffffff9fa69910 T ext4_free_inode\n'
                'ffffffff9fa6a540 T ext4_next\n')
            ranges = parse_symbol_ranges(path)
            self.assertIn((0xffffffff81469910, 0xffffffff8146a540, 'ext4_free_inode'), ranges)

    def test_pc_classification_handles_overlapping_symbol_extents(self):
        ranges = [(10, 20, 'a'), (15, 25, 'b'), (30, 40, 'c')]
        pcs = [9, 10, 19, 20, 24, 25, 30, 39, 40]
        result = classify_pcs(pcs, ranges)
        self.assertEqual(result['ext4_pcs'], [10, 19, 20, 24, 30, 39])
        self.assertEqual(result['ext4_unique'], 6)

    def test_pc_classification_scales_to_large_samples(self):
        ranges = [(i * 16, i * 16 + 8, f'symbol_{i}') for i in range(1000)]
        pcs = list(range(0, 16000, 2))
        result = classify_pcs(pcs, ranges)
        self.assertEqual(result['ext4_pc_count'], 4000)
        self.assertEqual(result['ext4_unique'], 4000)


class LegacyKcovSaturationAuditTests(unittest.TestCase):
    runtime = '''
size_t capacity = ct_kcov_capacity ? ct_kcov_capacity : 65536;
while (capacity < needed && capacity < 16777216) capacity *= 2;
if (count >= ct_kcov_entries) ct_kcov_saturated = 1;
if (capacity < needed) {
  ct_kcov_saturated = 1;
} else {
  uint64_t *pcs = realloc(ct_kcov_pcs, capacity * sizeof(*pcs));
  if (!pcs) ct_kcov_saturated = 1;
}
'''

    def test_non_capacity_legacy_total_does_not_identify_per_call_limit(self):
        # A truncated call can end at the old capacity, then a later allocation
        # can succeed. These are the last two retained ranges in the old sample.
        ranges = [(0, 8108743), (8108743, 279865), (8388608, 1116)]
        result = audit_module.infer_legacy_saturation_cause(
            8389724, ranges, True, self.runtime, 16777216)
        self.assertEqual(result['saturation_cause'], 'legacy_or_unclassified_saturation')
        self.assertIsNone(result['inferred_CT_KCOV_ENTRIES'])
        self.assertEqual(result['capacity_boundary_calls'],
                         [dict(index=1, start=8108743, count=279865, end=8388608)])
        self.assertIn('aggregate_allocation_failure', result['possible_causes'])

    def test_aggregate_capacity_total_remains_unclassified(self):
        ranges = [(0, 3752679), (3752679, 4635929)]
        result = audit_module.infer_legacy_saturation_cause(
            8388608, ranges, True, self.runtime, 16777216)
        self.assertEqual(result['saturation_cause'], 'legacy_or_unclassified_saturation')
        self.assertIsNone(result['inferred_CT_KCOV_ENTRIES'])


class KcovFeedbackTests(unittest.TestCase):
    def sample(self, path, pcs, overflow=0):
        path.write_bytes(struct.pack('<QQQQ', MAGIC, 1, len(pcs), overflow) +
                         (struct.pack(f'<{len(pcs)}Q', *pcs) if pcs else b''))

    def sample_v3(self, path, pcs, ranges, overflow=False, extra_flags=0):
        flags = (1 if overflow else 0) | 4 | extra_flags
        body = struct.pack('<QQQQ', MAGIC, 3, len(pcs), flags)
        body += struct.pack(f'<{len(pcs)}Q', *pcs) if pcs else b''
        body += struct.pack('<Q', len(ranges))
        body += b''.join(struct.pack('<QQ', *item) for item in ranges)
        path.write_bytes(body)

    def sample_v4(self, path, tokens, ranges, raw_count=None, overflow=False):
        """Write the compact runtime representation used by v4 samples."""
        flags = (1 if overflow else 0) | 4 | 128
        raw_count = sum(run for _, run in tokens) if raw_count is None else raw_count
        body = struct.pack('<QQQQ', MAGIC, 4, len(tokens), flags)
        body += struct.pack('<Q', raw_count)
        body += b''.join(struct.pack('<QQ', pc, run) for pc, run in tokens)
        body += struct.pack('<Q', len(ranges))
        body += b''.join(struct.pack('<QQ', *item) for item in ranges)
        path.write_bytes(body)

    def sample_v5(self, path, features, ranges, raw_count=None, flags=0):
        """Write the unique PC/edge feature-set representation."""
        flags |= 4 | 256
        raw_count = len(features) if raw_count is None else raw_count
        body = struct.pack('<QQQQ', MAGIC, 5, len(features), flags)
        body += struct.pack('<Q', raw_count)
        body += b''.join(struct.pack('<QQ', *item) for item in features)
        body += struct.pack('<Q', len(ranges))
        body += b''.join(struct.pack('<QQ', *item) for item in ranges)
        path.write_bytes(body)

    def test_parser_rejects_truncation_and_overflow_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            p = pathlib.Path(temp) / 'x.bin'
            p.write_bytes(b'bad')
            with self.assertRaises(ValueError):
                read_sample(p)
            self.sample(p, [1, 2], 1)
            self.assertEqual(read_sample(p), ([1, 2], True))
            p.write_bytes(struct.pack('<QQQQ', MAGIC, 2, 2, 1) +
                         struct.pack('<2Q', 1, 2))
            self.assertEqual(read_sample(p), ([1, 2], True))
            p.write_bytes(struct.pack('<QQQQ', MAGIC, 1, 1, 0x80) +
                          struct.pack('<Q', 7))
            self.assertEqual(read_sample(p), ([7], False))

    def test_large_saturated_sample_skips_unpacking(self):
        with tempfile.TemporaryDirectory() as temp:
            p = pathlib.Path(temp) / 'large.bin'
            count = SATURATED_PARSE_LIMIT + 1
            with p.open('wb') as stream:
                stream.write(struct.pack('<QQQQ', MAGIC, 1, count, 1))
                stream.seek(HEADER.size + count * 8 - 1)
                stream.write(b'\0')
            self.assertEqual(read_sample(p), ([], True))

    def test_saturated_sample_exposes_runtime_cause(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'limited.bin'
            self.sample_v3(path, [10], [(0, 1)], overflow=True, extra_flags=8)
            self.assertEqual(read_sample_diagnostics(path)['saturation_reasons'],
                             ['per_call_buffer_full'])
            self.assertTrue(read_sample_details(path)[1])

    def test_v4_compact_runs_preserve_pcs_self_edges_and_call_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'compact.bin'
            self.sample_v4(path, [(10, 3), (20, 1), (30, 2)], [(0, 2), (2, 1)])
            self.assertEqual(read_sample_diagnostics(path)['compact_raw_count'], 6)
            pcs, saturated, ranges = read_sample_details(path)
            self.assertFalse(saturated)
            self.assertEqual(pcs, [10, 10, 20, 30, 30])
            self.assertEqual(ranges, [(0, 3), (3, 2)])
            edges = set()
            for start, length in ranges:
                edges.update(zip(pcs[start:start + length], pcs[start + 1:start + length]))
            self.assertEqual(edges, {(10, 10), (10, 20), (30, 30)})

    def test_v4_compact_run_count_is_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'bad-compact.bin'
            self.sample_v4(path, [(10, 3)], [(0, 1)], raw_count=2)
            with self.assertRaisesRegex(ValueError, 'kcov_compact_count_mismatch'):
                read_sample_details(path)

    def test_v4_compact_raw_count_is_independent_of_token_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'large-run.bin'
            self.sample_v4(path, [(10, 16_777_217)], [(0, 1)])
            self.assertEqual(sample_count(path), 16_777_217)
            pcs, saturated, ranges = read_sample_details(path)
            self.assertFalse(saturated)
            self.assertEqual(pcs, [10, 10])
            self.assertEqual(ranges, [(0, 2)])

    def test_v5_feature_set_bounds_alternating_repeated_pc_pressure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'feature-set.bin'
            # The original sequence alternates for a very long time.  The
            # feature set stores each PC and edge once per syscall instead of
            # retaining every repeated execution record.
            features = [(10, 0), (20, 0), (10, 20), (20, 10),
                        (30, 0), (40, 0), (30, 40)]
            self.sample_v5(path, features, [(0, 4), (4, 3)], raw_count=2_000_000)
            diagnostics = read_sample_diagnostics(path)
            self.assertEqual(diagnostics['version'], 5)
            self.assertTrue(diagnostics['feature_set'])
            self.assertEqual(diagnostics['compact_raw_count'], 2_000_000)
            payload = read_sample_features(path)
            self.assertFalse(payload['saturated'])
            self.assertEqual(payload['raw_count'], 2_000_000)
            self.assertEqual(payload['pcs'], [10, 20, 30, 40])
            self.assertEqual(payload['edges'], {(10, 20), (20, 10), (30, 40)})
            self.assertEqual(payload['calls'][0]['edges'], {(10, 20), (20, 10)})
            self.assertEqual(payload['calls'][1]['edges'], {(30, 40)})
            self.assertEqual(read_sample_details(path),
                             ([10, 20, 30, 40], False, [(0, 2), (2, 2)]))

    def test_v5_feature_set_preserves_single_pc_self_edge(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'self-edge.bin'
            self.sample_v5(path, [(10, 0), (10, 10)], [(0, 2)], raw_count=2)
            payload = read_sample_features(path)
            self.assertEqual(payload['pcs'], [10])
            self.assertEqual(payload['edges'], {(10, 10)})

    def test_v5_feature_set_allows_repeated_feature_across_syscalls(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'cross-call-repeat.bin'
            self.sample_v5(path, [(10, 0), (10, 0)], [(0, 1), (1, 1)], raw_count=2)
            payload = read_sample_features(path)
            self.assertEqual([call['pcs'] for call in payload['calls']], [[10], [10]])

    def test_v5_feature_table_limit_is_explicit_and_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            folder = run / 'segment-000000'
            (folder / 'coverage').mkdir(parents=True)
            program = folder / 'syz-programs/b1-w0'
            program.mkdir(parents=True)
            (program / 'program.syz').write_text('openat\n')
            self.sample_v5(folder / 'coverage/b1-w0.bin', [(10, 0)], [(0, 1)],
                           raw_count=1_000_000, flags=1 | 512)
            diagnostics = read_sample_diagnostics(folder / 'coverage/b1-w0.bin')
            self.assertEqual(diagnostics['saturation_reasons'], ['feature_table_limit'])
            corpus = FeedbackCorpus(run)
            result = corpus.collect(folder, dict(segment_id=0,
                                                 expected_programs=['b1-w0']))
            self.assertFalse(result['feedback_complete'] if 'feedback_complete' in result
                             else result['passed'])
            self.assertEqual(result['campaign_summary']['saturation_reasons'],
                             {'feature_table_limit': 1})
            self.assertIn('b1-w0:saturated', result['errors'])

    def test_v5_edges_feed_feedback_without_cross_call_transition(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            folder = run / 'segment-000000'
            (folder / 'coverage').mkdir(parents=True)
            program = folder / 'syz-programs/b1-w0'
            program.mkdir(parents=True)
            (program / 'program.syz').write_text('openat\n')
            features = [(10, 0), (20, 0), (10, 20),
                        (30, 0), (40, 0), (30, 40)]
            self.sample_v5(folder / 'coverage/b1-w0.bin', features,
                           [(0, 3), (3, 3)], raw_count=500_000)
            corpus = FeedbackCorpus(run, feedback_mode='pc_edge')
            corpus.coverage.update({10, 20, 30, 40})
            result = corpus.collect(folder, dict(segment_id=0,
                                                 expected_programs=['b1-w0']))
            self.assertTrue(result['passed'])
            self.assertEqual(result['programs'][0]['new'], 0)
            self.assertEqual(result['programs'][0]['new_edges'], 2)
            self.assertEqual([call['edges'] for call in result['programs'][0]['calls']], [1, 1])
            self.assertNotIn((20, 30), corpus.edges)
            corpus.augment(folder, {}, native_execution_verified=True)
            self.assertEqual(corpus.edges, {(10, 20), (30, 40)})

    def test_v5_feature_set_rejects_cross_call_or_uncovered_ranges(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'bad-feature-set.bin'
            self.sample_v5(path, [(10, 0), (10, 20)], [(0, 1), (0, 1)])
            with self.assertRaisesRegex(ValueError, 'kcov_invalid_call_range'):
                read_sample_features(path)
            self.sample_v5(path, [(10, 0), (10, 20)], [(0, 1)])
            with self.assertRaisesRegex(ValueError, 'kcov_uncovered_feature_range'):
                read_sample_features(path)

    def test_v5_feature_set_rejects_duplicate_records_and_inconsistent_raw_count(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'duplicate-feature-set.bin'
            self.sample_v5(path, [(10, 0), (10, 0)], [(0, 2)], raw_count=2)
            with self.assertRaisesRegex(ValueError, 'kcov_duplicate_feature'):
                read_sample_features(path)
            self.sample_v5(path, [(10, 0), (20, 0), (10, 20)], [(0, 3)], raw_count=1)
            with self.assertRaisesRegex(ValueError, 'kcov_feature_raw_count_mismatch'):
                read_sample_features(path)

    def test_campaign_summary_counts_known_and_legacy_saturation_causes(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            folder = run / 'segment-000000'
            (folder / 'coverage').mkdir(parents=True)
            program_dir = folder / 'syz-programs/b1-w0'
            program_dir.mkdir(parents=True)
            (program_dir / 'program.syz').write_text('openat$dir\n')
            self.sample_v3(folder / 'coverage/b1-w0.bin', [10], [(0, 1)],
                           overflow=True, extra_flags=8)
            corpus = FeedbackCorpus(run)
            summary = corpus.collect(folder, dict(expected_programs=['b1-w0']))['campaign_summary']
            self.assertFalse(summary['feedback_complete'])
            self.assertEqual(summary['saturation_reasons'], {'per_call_buffer_full': 1})

    def test_only_new_coverage_is_admitted_and_parent_is_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            folder = run / 'segment-000000'
            (folder / 'coverage').mkdir(parents=True)
            (folder / 'syz-programs/b1-w0').mkdir(parents=True)
            (folder / 'syz-programs/b1-w0/program.syz').write_text('openat\n')
            self.sample(folder / 'coverage/b1-w0.bin', [10, 20])
            corpus = FeedbackCorpus(run)
            first = corpus.collect(folder, {'segment_id': 0})
            self.assertTrue(first['passed'])
            self.assertEqual(first['new_coverage'], 2)
            self.assertIsNone(corpus.parent(0))
            corpus.augment(folder, {}, native_execution_verified=True)
            self.assertIsNotNone(corpus.parent(0))
            folder2 = run / 'segment-000001'
            (folder2 / 'coverage').mkdir(parents=True)
            (folder2 / 'syz-programs/b1-w0').mkdir(parents=True)
            (folder2 / 'syz-programs/b1-w0/program.syz').write_text('openat\n')
            self.sample(folder2 / 'coverage/b1-w0.bin', [20, 30])
            second = corpus.collect(folder2, {'segment_id': 1})
            self.assertEqual(second['new_coverage'], 1)
            self.assertEqual(second['total_coverage'], 2)
            corpus.augment(folder2, {}, native_execution_verified=True)
            self.assertEqual(json.loads((folder2 / 'coverage.json').read_text())['total_coverage'], 3)
            self.assertEqual(len(list((run / 'corpus').glob('*.syz'))), 1)
            self.assertEqual(json.loads((folder2 / 'coverage.json').read_text())['programs'][0]['new'], 1)

    def test_native_unverified_sample_never_changes_coverage_or_corpus(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            folder = run / 'segment-000000'
            (folder / 'coverage').mkdir(parents=True)
            (folder / 'syz-programs/b1-w0').mkdir(parents=True)
            (folder / 'syz-programs/b1-w0/program.syz').write_text('openat\n')
            self.sample(folder / 'coverage/b1-w0.bin', [10, 20])
            corpus = FeedbackCorpus(run)
            corpus.collect(folder, {'segment_id': 0})
            result = corpus.augment(folder, {}, native_execution_verified=False)
            self.assertFalse(result['native_execution_verified'])
            self.assertEqual(corpus.coverage, set())
            self.assertEqual(corpus.parents, [])
            self.assertFalse(json.loads((folder / 'coverage.json').read_text())['passed'])


    def test_segment_level_formula_and_mismatch_do_not_reward_unattributed_programs(self):
        report = dict(point_statistics={'ALLOC.LOCAL|R-BG': {'hits': 1}},
                      runs=[dict(episodes=[dict(classification='operation_exit_mismatch',
                                                stream='R-BG', point='ALLOC.LOCAL', formula='R-BG',
                                                peak_gap=1)])])
        for native_verified in (False, True):
            with self.subTest(native_verified=native_verified), tempfile.TemporaryDirectory() as temp:
                run = pathlib.Path(temp)
                folder = run / 'segment-000000'
                (folder / 'coverage').mkdir(parents=True)
                (folder / 'syz-programs/b1-w0').mkdir(parents=True)
                (folder / 'syz-programs/b1-w0/program.syz').write_text('openat\n')
                self.sample(folder / 'coverage/b1-w0.bin', [10])
                corpus = FeedbackCorpus(run)
                corpus.coverage.add(10)
                corpus.collect(folder, {'segment_id': 0})
                result = corpus.augment(folder, report,
                                        native_execution_verified=native_verified)
                self.assertEqual(result['new_mismatch_signatures'], int(native_verified))
                self.assertEqual(result['new_semantic_features'], 0)
                self.assertFalse(result['admitted_repro_programs'])

    def test_parent_selection_is_fair_even_when_first_parent_had_large_score(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            corpus = FeedbackCorpus(run)
            high, low = run / 'high.syz', run / 'low.syz'
            high.touch(); low.touch()
            corpus.parents = [high, low]
            corpus.parent_stats["high-score"] = dict(score=16750)
            selected = []
            for _ in range(20):
                candidate = corpus.parent_candidate()
                selected.append(pathlib.Path(candidate['path']))
                corpus.commit_parent(candidate)
            self.assertEqual(selected.count(high), 10)
            self.assertEqual(selected.count(low), 10)

    def test_parent_candidate_is_not_consumed_until_mutation_commits(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            corpus = FeedbackCorpus(run)
            first, second = run / 'first.syz', run / 'second.syz'
            first.write_text('first')
            second.write_text('second')
            corpus.parents = [first, second]
            candidate = corpus.parent_candidate()
            self.assertEqual(pathlib.Path(candidate['path']), first)
            self.assertEqual(pathlib.Path(corpus.parent_candidate()['path']), first)
            corpus.commit_parent(candidate)
            self.assertEqual(pathlib.Path(corpus.parent_candidate()['path']), second)

    def test_rarity_scheduler_prefers_rare_parent_and_bounds_starvation(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            corpus = FeedbackCorpus(run, scheduler_policy='rarity_fair', seed=41)
            common, rare = run / 'common.syz', run / 'rare.syz'
            common.write_text('common')
            rare.write_text('rare')
            corpus.parents = [common, rare]
            common_hash = hashlib.sha256(common.read_bytes()).hexdigest()
            rare_hash = hashlib.sha256(rare.read_bytes()).hexdigest()
            corpus.parent_features = {common_hash: {'pc:a'}, rare_hash: {'pc:b'}}
            corpus.feature_frequency = {'pc:a': 100, 'pc:b': 1}
            selected = []
            for _ in range(100):
                candidate = corpus.parent_candidate()
                selected.append(pathlib.Path(candidate['path']))
                corpus.commit_parent(candidate)
            self.assertGreater(selected.count(rare), selected.count(common))
            self.assertGreater(selected.count(common), 0)

    def test_target_rarity_keeps_edge_breadth_from_dominating_pc_and_ext4(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            corpus = FeedbackCorpus(run, scheduler_policy='rarity_fair')
            target, edge_heavy = run / 'target.syz', run / 'edge-heavy.syz'
            target.write_text('target')
            edge_heavy.write_text('edge-heavy')
            corpus.parents = [target, edge_heavy]
            hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in corpus.parents}
            corpus.parent_features = {
                hashes[target]: {'pc:1', 'ext4:1'},
                hashes[edge_heavy]: {f'edge:{i:x}:{i + 1:x}' for i in range(256)},
            }
            corpus.feature_frequency = {
                feature: 1 for features in corpus.parent_features.values()
                for feature in features
            }
            target_components = corpus.rarity_components(hashes[target])
            edge_components = corpus.rarity_components(hashes[edge_heavy])
            self.assertGreater(target_components['weighted'], edge_components['weighted'])
            self.assertLessEqual(edge_components['weighted'], 0.15 * 4.0)

    def test_zero_yield_parent_is_cooled_but_reopened_for_exploration(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            corpus = FeedbackCorpus(run, scheduler_policy='rarity_fair')
            productive, exhausted = run / 'productive.syz', run / 'exhausted.syz'
            productive.write_text('productive')
            exhausted.write_text('exhausted')
            corpus.parents = [productive, exhausted]
            hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in corpus.parents}
            corpus.parent_stats = {
                hashes[productive]: {'zero_yield_streak': 0},
                hashes[exhausted]: {'zero_yield_streak': 3},
            }
            corpus.selection_count = 1
            self.assertEqual(corpus._eligible_parent_indices(), [0])
            corpus.selection_count = 16
            self.assertEqual(corpus._eligible_parent_indices(), [0, 1])

    def test_auxiliary_yield_does_not_mask_target_pc_cooling(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            corpus = FeedbackCorpus(run, scheduler_policy='rarity_fair')
            parent = run / 'parent.syz'
            parent.write_text('parent')
            corpus.parents = [parent]
            key = hashlib.sha256(parent.read_bytes()).hexdigest()
            # Edge/semantic novelty is retained as a diagnostic EMA, but it
            # does not count as target coverage for the cooling decision.
            for _ in range(3):
                corpus._record_child_yield(key, {
                    'new_pcs': 0, 'ext4_new': 0,
                    'new_edges': 1, 'new_semantic_features': 1})
            stats = corpus.parent_stats[key]
            self.assertEqual(stats['zero_target_yield_streak'], 3)
            self.assertEqual(stats['zero_yield_streak'], 3)
            self.assertEqual(stats['zero_any_signal_streak'], 0)
            self.assertGreater(stats['yield_ema_edges'], 0)
            self.assertGreater(stats['yield_ema_semantic'], 0)
            corpus.selection_count = 1
            self.assertEqual(corpus._eligible_parent_indices(), [0])
            corpus._record_child_yield(key, {'new_pcs': 1, 'ext4_new': 0})
            self.assertEqual(stats['zero_target_yield_streak'], 0)
            self.assertEqual(stats['zero_yield_streak'], 0)

    def test_ext4_target_yield_resets_target_cooling(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            corpus = FeedbackCorpus(run, scheduler_policy='rarity_fair')
            parent = run / 'parent.syz'
            parent.write_text('parent')
            key = hashlib.sha256(parent.read_bytes()).hexdigest()
            corpus._record_child_yield(key, {'new_pcs': 0, 'ext4_new': 0})
            corpus._record_child_yield(key, {'new_pcs': 0, 'ext4_new': 2})
            self.assertEqual(corpus.parent_stats[key]['zero_target_yield_streak'], 0)

    def test_rarity_scheduler_uses_ext4_child_yield_without_starving_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            corpus = FeedbackCorpus(run, scheduler_policy='rarity_fair', seed=7)
            productive, exhausted = run / 'productive.syz', run / 'exhausted.syz'
            productive.write_text('productive')
            exhausted.write_text('exhausted')
            corpus.parents = [productive, exhausted]
            hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in corpus.parents}
            corpus.parent_stats = {
                hashes[productive]: dict(child_executions=4, child_new_pcs=8,
                                         child_new_ext4_pcs=8, last_selected=0),
                hashes[exhausted]: dict(child_executions=4, child_new_pcs=0,
                                        child_new_ext4_pcs=0, last_selected=0),
            }
            self.assertGreater(corpus.parent_selection_weight(hashes[productive]),
                               corpus.parent_selection_weight(hashes[exhausted]))
            selected = []
            for _ in range(40):
                candidate = corpus.parent_candidate()
                selected.append(pathlib.Path(candidate['path']))
                corpus.commit_parent(candidate)
            self.assertGreater(selected.count(productive), selected.count(exhausted))
            self.assertGreater(selected.count(exhausted), 0)

    def test_scheduler_persists_child_ext4_yield_and_candidate_score(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            corpus = FeedbackCorpus(run, scheduler_policy='rarity_fair')
            source = run / 'parent.syz'
            source.write_text('parent')
            target = corpus._admit_parent(source, dict(admission_signals=['pc'],
                                                       feature_ids=['pc:1']))
            key = hashlib.sha256(source.read_bytes()).hexdigest()
            corpus.parent_stats[key].update(child_executions=2,
                                            child_new_pcs=5,
                                            child_new_ext4_pcs=3)
            candidate = corpus.parent_candidate()
            self.assertGreater(candidate['yield_score'], 0)
            corpus.write_scheduler()
            payload = json.loads((run / 'feedback/scheduler.json').read_text())
            record = payload['parents'][0]
            self.assertEqual(record['child_new_ext4_pcs'], 3)
            self.assertIn('yield_score', record)

    def test_edge_only_selection_share_is_bounded_when_pc_parent_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            regular_a, regular_b, edge = (run / name for name in
                                          ('regular-a.syz', 'regular-b.syz', 'edge.syz'))
            regular_a.write_text('regular-a')
            regular_b.write_text('regular-b')
            edge.write_text('edge')
            corpus = FeedbackCorpus(run, scheduler_policy='uniform_round_robin')
            corpus.parents = [regular_a, regular_b, edge]
            hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in corpus.parents}
            corpus.parent_signal_kinds = {
                hashes[regular_a]: 'pc', hashes[regular_b]: 'pc',
                hashes[edge]: 'edge_only'}
            for _ in range(100):
                candidate = corpus.parent_candidate()
                corpus.commit_parent(candidate)
            edge_selections = corpus.selection_parent_counts['edge_only']
            self.assertLessEqual(edge_selections,
                                 corpus.selection_count // 4 + 1)
            self.assertGreater(edge_selections, 0)
            self.assertLessEqual(corpus.summary()['edge_only_selection_share'],
                                 0.25 + 1 / corpus.selection_count)

    def test_dynamic_mutation_budget_scales_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            corpus = FeedbackCorpus(pathlib.Path(temp), scheduler_policy='rarity_fair')
            low = corpus.mutation_budget({'rarity_score': 1}, 16, max_calls=256)
            high = corpus.mutation_budget({'rarity_score': 10**12}, 16, max_calls=24)
            self.assertGreater(low, 16)
            self.assertGreaterEqual(high, low)
            self.assertEqual(high, 24)
            self.assertEqual(corpus.mutation_budget(None, 16), 16)

    def test_edge_only_parent_admission_is_bounded_but_feedback_is_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            corpus = FeedbackCorpus(run, feedback_mode='pc_edge')
            # Before any PC-bearing input exists, the minimum exploration lane
            # admits eight edge-only parents.  The ninth is still a valid
            # feedback event, but must not dilute the active parent pool.
            for index in range(8):
                source = run / f'edge-{index}.syz'
                source.write_text(f'openat${index}\n')
                corpus._admit_parent(source, dict(
                    admission_signals=['edge'], feature_ids=[f'edge:{index}']))
            self.assertEqual(corpus.edge_only_parent_limit(), 8)

            folder = run / 'segment-000000'
            program = folder / 'syz-programs/b1-w0/program.syz'
            (folder / 'coverage').mkdir(parents=True)
            program.parent.mkdir(parents=True)
            program.write_text('openat$quota\n')
            # All PCs are known, while (20, 10) is a genuinely new edge.
            self.sample_v3(folder / 'coverage/b1-w0.bin', [10, 20, 10], [(0, 3)])
            corpus.coverage.update({10, 20})
            corpus.edges.add((10, 20))
            corpus.collect(folder, {'segment_id': 0, 'expected_programs': ['b1-w0']})
            result = corpus.augment(folder, {}, native_execution_verified=True)

            feedback = result['programs']['b1-w0']
            self.assertEqual(feedback['new_pcs'], 0)
            self.assertEqual(feedback['new_edges'], 1)
            self.assertTrue(feedback['admission_eligible'])
            self.assertFalse(feedback['admitted'])
            self.assertEqual(feedback['admission_suppressed'], 'edge_only_quota')
            self.assertEqual(len(corpus.parents), 8)
            self.assertEqual(corpus.edge_only_admissions_suppressed, 1)
            self.assertEqual(result['campaign_summary']['total_edges'], 2)

    def test_semantic_only_parent_admission_has_bounded_exploration_lane(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            corpus = FeedbackCorpus(run, feedback_mode='pc_semantic')
            for index in range(8):
                source = run / f'sem-{index}.syz'
                source.write_text(f'semantic${index}\n')
                corpus._admit_parent(source, dict(
                    admission_signals=['semantic'], feature_ids=[f'sem:key-{index}']))
            self.assertEqual(corpus.semantic_only_parent_limit(), 8)
            source = run / 'sem-quota.syz'
            source.write_text('semantic$quota\n')
            key = hashlib.sha256(source.read_bytes()).hexdigest()
            self.assertFalse(corpus._side_only_parent_allowed(key, 'semantic_only'))

    def test_replayed_edge_only_parent_promotes_when_pc_is_observed(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            source = run / 'replayed.syz'
            source.write_text('openat$replayed\n')
            corpus = FeedbackCorpus(run, feedback_mode='pc_edge')
            corpus._admit_parent(source, dict(
                admission_signals=['edge'], feature_ids=['edge:1:2']))
            self.assertEqual(corpus.admission_parent_counts['edge_only'], 1)
            corpus._admit_parent(source, dict(
                admission_signals=['pc'], feature_ids=['pc:3']))
            self.assertEqual(corpus.admission_parent_counts['edge_only'], 0)
            self.assertEqual(corpus.admission_parent_counts['pc'], 1)
            self.assertEqual(corpus.parent_signal_kinds[
                hashlib.sha256(source.read_bytes()).hexdigest()], 'pc')
            self.assertEqual(len(corpus.parents), 1)

    def test_feedback_ablation_controls_corpus_admission(self):
        for mode, expected_admission in (('pc', False), ('pc_edge', True)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                run = pathlib.Path(temp)
                folder = run / 'segment-000000'
                (folder / 'coverage').mkdir(parents=True)
                program = folder / 'syz-programs/b1-w0/program.syz'
                program.parent.mkdir(parents=True)
                program.write_text('openat\n')
                self.sample_v3(folder / 'coverage/b1-w0.bin', [10, 20], [(0, 2)])
                corpus = FeedbackCorpus(run, feedback_mode=mode)
                corpus.coverage.update((10, 20))
                corpus.collect(folder, {'segment_id': 0, 'expected_programs': ['b1-w0']})
                corpus.augment(folder, {}, native_execution_verified=True)
                self.assertEqual(bool(corpus.parents), expected_admission)

    def test_target_scope_rejects_generic_pc_and_accepts_target_pc(self):
        for pcs, expected in (([10], False), ([20], True)):
            with self.subTest(pcs=pcs), tempfile.TemporaryDirectory() as temp:
                run = pathlib.Path(temp)
                folder = run / 'segment-000000'
                program = folder / 'syz-programs/b1-w0/program.syz'
                (folder / 'coverage').mkdir(parents=True)
                program.parent.mkdir(parents=True)
                program.write_text('openat\n')
                self.sample_v3(folder / 'coverage/b1-w0.bin', pcs, [(0, len(pcs))])
                corpus = FeedbackCorpus(run, feedback_mode='pc',
                                        target_scope='ext4_jbd2')
                corpus.ext4_ranges = [
                    (0, 15, 'generic_kernel_fn'),
                    (16, 25, 'ext4_file_open'),
                ]
                corpus.collect(folder, {'segment_id': 0,
                                        'expected_programs': ['b1-w0']})
                corpus.augment(folder, {}, native_execution_verified=True)
                self.assertEqual(bool(corpus.parents), expected)
                row = json.loads((folder / 'coverage.json').read_text())['programs'][0]
                self.assertEqual(row['target_new'], int(expected))

    def test_kcov_call_ranges_never_create_edges_across_syscalls(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'calls.bin'
            self.sample_v3(path, [10, 20, 30, 40], [(0, 2), (2, 2)])
            pcs, saturated, ranges = read_sample_details(path)
            self.assertFalse(saturated)
            self.assertEqual(pcs, [10, 20, 30, 40])
            edges = set()
            for start, length in ranges:
                edges.update(zip(pcs[start:start + length], pcs[start + 1:start + length]))
            self.assertEqual(edges, {(10, 20), (30, 40)})

    def test_semantic_features_are_attributed_to_verified_call_only(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            folder = run / 'segment-000000'
            program_dir = folder / 'syz-programs/b1-w0'
            (folder / 'coverage').mkdir(parents=True)
            program_dir.mkdir(parents=True)
            (program_dir / 'program.syz').write_text('openat\n')
            self.sample_v3(folder / 'coverage/b1-w0.bin', [10, 20], [(0, 1), (1, 1)])
            timeline = folder / 'timeline.jsonl'
            rows = [dict(program='b1-w0', call_index=2, event_source='generated_syscall',
                         attribution='same_task_native_syscall', value_kind='counter',
                         event=dict(formula='R-BG', point='ALLOC.LOCAL', status=1,
                                    actual=4, expected=4)),
                    dict(program='b1-w0', call_index=3, event_source='background_unattributed',
                         attribution='no_cross_thread_cause', value_kind='counter',
                         event=dict(formula='R-BG', point='ALLOC.BACKGROUND', status=1,
                                    actual=4, expected=4))]
            timeline.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            report = dict(event_sources=dict(format='explicit_phases',
                                             source_attribution_complete=True),
                          runs=[dict(trajectory_integrity=True)])
            corpus = FeedbackCorpus(run)
            corpus.coverage.update({10, 20})
            corpus.collect(folder, dict(segment_id=0))
            result = corpus.augment(folder, report, timeline_path=timeline, mode='single')
            feedback = result['programs']['b1-w0']
            self.assertTrue(result['semantic_feedback_complete'])
            self.assertEqual(feedback['new_semantic_features'], 1)
            self.assertEqual(feedback['attributed_calls'], 1)
            self.assertEqual(feedback['new_edges'], 0)
            self.assertEqual(result['admitted_repro_programs'], ['corpus/' +
                             next((run / 'corpus').glob('*.syz')).name])

    def test_real_lifecycle_boundary_enables_semantic_only_corpus_admission(self):
        # All PC and edge features are already known, so semantic feedback is
        # the sole possible reason to admit this program.
        for mode, expected in (('pc', False), ('pc_edge', False),
                               ('pc_semantic', True), ('pc_edge_semantic', True)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                run = pathlib.Path(temp)
                folder = run / 'segment-000000'
                program_dir = folder / 'syz-programs/b1-w0'
                (folder / 'coverage').mkdir(parents=True)
                program_dir.mkdir(parents=True)
                (program_dir / 'program.syz').write_text('openat$dir\n')
                self.sample_v3(folder / 'coverage/b1-w0.bin', [10, 20], [(0, 2)])
                timeline = folder / 'timeline.jsonl'
                timeline.write_text(json.dumps(dict(
                    program='b1-w0', call_index=0,
                    event_source='generated_syscall',
                    attribution='same_task_native_syscall', value_kind='counter',
                    event=dict(formula='R-BG', point='ALLOC.LOCAL', status=1,
                               actual=4, expected=4))) + '\n')
                report = dict(
                    native_syscalls=[dict(program='b1-w0', call_index=0, errno=0)],
                    event_sources=dict(format='explicit_phases',
                                       source_attribution_complete=True),
                    runs=[dict(trajectory_integrity=True, boundaries=[dict(
                        kind='native_syscall_return', program='b1-w0', call_index=0,
                        classification='operation_exit_mismatch')])])
                corpus = FeedbackCorpus(run, feedback_mode=mode)
                corpus.coverage.update({10, 20})
                corpus.edges.add((10, 20))
                corpus.collect(folder, dict(segment_id=0, expected_programs=['b1-w0']))
                result = corpus.augment(folder, report, timeline_path=timeline, mode='single')
                feedback = result['programs']['b1-w0']
                self.assertEqual(feedback['new_pcs'], 0)
                self.assertEqual(feedback['new_edges'], 0)
                self.assertEqual(feedback['new_semantic_features'], 1)
                self.assertTrue(feedback['semantic_features'][0].endswith(
                    '|success|operation_exit_mismatch'))
                persisted = json.loads((folder / 'coverage.json').read_text())
                persisted_row = persisted['programs'][0]
                self.assertEqual(persisted_row['admitted'], expected)
                self.assertEqual(persisted_row['admission_signals'],
                                 ['semantic'] if expected else [])
                self.assertEqual(persisted_row['new_semantic_features'], 1)
                self.assertEqual(feedback['admitted'], expected)
                self.assertEqual(feedback['admission_signals'],
                                 ['semantic'] if expected else [])
                self.assertEqual(result['semantic_only_admitted_programs'],
                                 (result['admitted_repro_programs'] if expected else []))
                self.assertEqual(bool(result['admitted_repro_programs']), expected)

    def test_duplicate_semantic_feature_does_not_trigger_new_admission(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            corpus = FeedbackCorpus(run, feedback_mode='pc_semantic')
            corpus.coverage.update({10, 20})
            corpus.edges.add((10, 20))
            report = dict(
                native_syscalls=[dict(program='b1-w0', call_index=0, errno=0)],
                event_sources=dict(format='explicit_phases',
                                   source_attribution_complete=True),
                runs=[dict(trajectory_integrity=True, boundaries=[dict(
                    kind='native_syscall_return', program='b1-w0', call_index=0,
                    classification='consistent')])])
            for segment in range(2):
                folder = run / f'segment-{segment:06d}'
                program_dir = folder / 'syz-programs/b1-w0'
                (folder / 'coverage').mkdir(parents=True)
                program_dir.mkdir(parents=True)
                (program_dir / 'program.syz').write_text(f'openat${segment}\n')
                self.sample_v3(folder / 'coverage/b1-w0.bin', [10, 20], [(0, 2)])
                timeline = folder / 'timeline.jsonl'
                timeline.write_text(json.dumps(dict(
                    program='b1-w0', call_index=0,
                    event_source='generated_syscall',
                    attribution='same_task_native_syscall', value_kind='counter',
                    event=dict(formula='R-BG', point='ALLOC.LOCAL', status=1,
                               actual=4, expected=4))) + '\n')
                corpus.collect(folder, dict(segment_id=segment,
                                            expected_programs=['b1-w0']))
                result = corpus.augment(folder, report, timeline_path=timeline,
                                        mode='single')
                if segment == 0:
                    self.assertEqual(len(result['admitted_repro_programs']), 1)
                else:
                    self.assertEqual(result['programs']['b1-w0']['new_semantic_features'], 0)
                    self.assertEqual(result['admitted_repro_programs'], [])

    def test_v3_parser_rejects_invalid_call_range(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'bad-range.bin'
            self.sample_v3(path, [10, 20], [(0, 1), (0, 1)])
            with self.assertRaisesRegex(ValueError, 'kcov_invalid_call_range'):
                read_sample_details(path)

    def test_cumulative_gaps_survive_good_samples(self):
        for problem in ('saturated', 'truncated', 'missing'):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as temp:
                run = pathlib.Path(temp)
                corpus = FeedbackCorpus(run)
                for i in range(3):
                    folder = run / f'segment-{i}'
                    (folder / 'coverage').mkdir(parents=True)
                    (folder / 'syz-programs/b1-w0').mkdir(parents=True)
                    (folder / 'syz-programs/b1-w0/program.syz').write_text('openat\n')
                    path = folder / 'coverage/b1-w0.bin'
                    if i != 1 or problem != 'missing':
                        self.sample(path, [i+1], int(i == 1 and problem == 'saturated'))
                        if i == 1 and problem == 'truncated':
                            path.write_bytes(b'bad')
                    result = corpus.collect(folder, dict(segment_id=i, expected_programs=['b1-w0']))
                    corpus.augment(folder, {}, native_execution_verified=True)
                    result['campaign_summary'] = json.loads((run/'feedback/coverage.json').read_text())
                cumulative = result['campaign_summary']
                self.assertEqual(cumulative['expected_samples'], 3)
                self.assertEqual(cumulative['complete_samples'], 2)
                self.assertFalse(cumulative['samples_complete'])
                self.assertEqual(cumulative['gaps'][0]['segment_id'], 1)
                self.assertEqual(len(corpus.parents), 1)
                self.assertEqual(cumulative, json.loads((run/'feedback/coverage.json').read_text()))

    def test_empty_coverage_is_valid_only_with_expected_sample_file(self):
        with tempfile.TemporaryDirectory() as temp:
            run = pathlib.Path(temp)
            folder = run/'segment'; (folder/'coverage').mkdir(parents=True)
            corpus = FeedbackCorpus(run)
            self.sample(folder/'coverage/a.bin', [])
            result = corpus.collect(folder, dict(expected_programs=['a', 'b']))
            self.assertEqual(result['complete_samples'], 1)
            self.assertEqual(result['missing_samples'], 1)
            self.assertFalse(result['passed'])


if __name__ == '__main__':
    unittest.main()
