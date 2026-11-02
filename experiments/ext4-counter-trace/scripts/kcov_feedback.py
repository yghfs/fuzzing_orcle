"""Deterministic KCOV corpus admission and cumulative feedback accounting."""
from __future__ import annotations

import hashlib
import json
import math
import pathlib
import shutil
import struct
import re
from bisect import bisect_right

MAGIC = 0x43544B434F563031
HEADER = struct.Struct('<QQQQ')
WORD = struct.Struct('<Q')


SATURATED_PARSE_LIMIT = 262144
FEATURE_TABLE_LIMIT = 1048576
# Edge novelty is useful for exploration, but an edge-only input has no new
# PC evidence that it will produce useful descendants.  Keeping every such
# input as a parent makes a round-robin scheduler spend most of its mutation
# budget on low-yield parents (as the old long runs demonstrated).  Keep a
# small bounded exploration lane while giving PC-bearing parents priority.
EDGE_ONLY_PARENT_RATIO = 0.25
MIN_EDGE_ONLY_PARENTS = 8
EDGE_ONLY_SELECTION_SHARE = 0.25
# A semantic-only input is valuable as an independent exploration lane, but
# it must not grow without bound when semantic extraction becomes noisy.
SEMANTIC_ONLY_PARENT_RATIO = 0.25
MIN_SEMANTIC_ONLY_PARENTS = 8
SEMANTIC_ONLY_SELECTION_SHARE = 0.25
# Rarity is tracked per signal family.  Edge and semantic features are useful
# exploration signals, but they must not outweigh the PC target simply because
# a trace contains many more edges or semantic records.
RARITY_WEIGHTS = {
    'pc': 1.0,
    'ext4': 2.0,
    'edge': 0.15,
    'sem': 0.25,
}
RARITY_COMPONENT_CAP = 8.0
# Parents that repeatedly produce no new feature are cooled out of the normal
# weighted pool.  A periodic exploration turn keeps a temporarily exhausted
# parent from being lost permanently.
ZERO_YIELD_COOLDOWN_STREAK = 3
YIELD_EXPLORATION_INTERVAL = 16
YIELD_EMA_ALPHA = 0.25
# Cooling is tied to the optimization target (new kernel PCs), not any
# auxiliary feedback family. Keep an all-signals streak separately for
# diagnostics so edge/semantic novelty cannot mask stalled PC discovery.
# Parent selection keeps rarity as an exploration signal, then adjusts it by
# observed child yield. Ext4/JBD2 PCs are weighted more heavily because they
# are the coverage target while general PCs still indicate useful progress.
YIELD_EXT4_WEIGHT = 2.0
YIELD_PC_WEIGHT = 0.5
YIELD_EXPLORATION_FLOOR = 0.20
KCOV_FLAG_SATURATED = 1
KCOV_FLAG_CALL_RANGES = 4
KCOV_FLAG_PER_CALL_FULL = 8
KCOV_FLAG_TOTAL_LIMIT = 16
KCOV_FLAG_IO_FAILURE = 64
KCOV_FLAG_COMPACT = 128
KCOV_FLAG_FEATURE_SET = 256
KCOV_FLAG_FEATURE_TABLE_LIMIT = 512
KCOV_LEGACY_FLAGS = KCOV_FLAG_SATURATED | KCOV_FLAG_CALL_RANGES
KCOV_KNOWN_FLAGS = (KCOV_FLAG_SATURATED | KCOV_FLAG_CALL_RANGES |
                    KCOV_FLAG_PER_CALL_FULL | KCOV_FLAG_TOTAL_LIMIT |
                    KCOV_FLAG_IO_FAILURE | KCOV_FLAG_COMPACT |
                    KCOV_FLAG_FEATURE_SET | KCOV_FLAG_FEATURE_TABLE_LIMIT)
KCOV_SATURATION_REASONS = {
    KCOV_FLAG_PER_CALL_FULL: 'per_call_buffer_full',
    KCOV_FLAG_TOTAL_LIMIT: 'aggregate_pc_limit',
    KCOV_FLAG_IO_FAILURE: 'sample_spool_io_failed',
    KCOV_FLAG_FEATURE_TABLE_LIMIT: 'feature_table_limit',
}


def read_sample_diagnostics(path: pathlib.Path) -> dict:
    """Read the version and explicit saturation cause bits without unpacking PCs."""
    with pathlib.Path(path).open('rb') as stream:
        header = stream.read(HEADER.size)
    if len(header) < HEADER.size:
        raise ValueError('kcov_short_header')
    magic, version, count, flags = HEADER.unpack(header)
    if (magic != MAGIC or version not in (1, 2, 3, 4, 5) or
            (version == 2 and flags & ~KCOV_LEGACY_FLAGS) or
            (version in (3, 4, 5) and flags & ~KCOV_KNOWN_FLAGS) or
            (version == 3 and flags & KCOV_FLAG_COMPACT) or
            (version == 4 and not flags & KCOV_FLAG_COMPACT) or
            (version in (1, 2, 3, 4) and
             flags & (KCOV_FLAG_FEATURE_SET | KCOV_FLAG_FEATURE_TABLE_LIMIT)) or
            (version == 5 and not flags & KCOV_FLAG_FEATURE_SET) or
            (version == 5 and flags & KCOV_FLAG_COMPACT)):
        raise ValueError('kcov_bad_header')
    reasons = ([name for bit, name in KCOV_SATURATION_REASONS.items() if flags & bit]
               if version in (3, 4, 5) else [])
    raw_count = None
    if version in (4, 5):
        with pathlib.Path(path).open('rb') as stream:
            stream.seek(HEADER.size)
            raw = stream.read(WORD.size)
        if len(raw) != WORD.size:
            raise ValueError('kcov_short_compact_count')
        raw_count, = WORD.unpack(raw)
    return dict(version=version, flags=flags, feature_count=count,
                saturated=bool(flags & KCOV_FLAG_SATURATED),
                compact=version == 4, feature_set=version == 5,
                compact_raw_count=raw_count, feature_raw_count=raw_count,
                saturation_reasons=reasons)


def read_sample_features(path: pathlib.Path) -> dict:
    """Decode a v5 unique PC/edge sample while retaining syscall ownership.

    Feature records are ``(pc, 0)`` for PCs and ``(from_pc, to_pc)`` for
    edges.  The call table ranges over those records, so no edge is inferred
    across generated syscall boundaries.
    """
    path = pathlib.Path(path)
    with path.open('rb') as stream:
        header = stream.read(HEADER.size)
    if len(header) != HEADER.size:
        raise ValueError('kcov_short_header')
    magic, version, count, flags = HEADER.unpack(header)
    if (magic != MAGIC or version != 5 or
            flags & ~KCOV_KNOWN_FLAGS or not flags & KCOV_FLAG_FEATURE_SET or
            flags & KCOV_FLAG_COMPACT or not flags & KCOV_FLAG_CALL_RANGES):
        raise ValueError('kcov_bad_header')
    if flags & (KCOV_FLAG_PER_CALL_FULL | KCOV_FLAG_TOTAL_LIMIT |
                KCOV_FLAG_IO_FAILURE | KCOV_FLAG_FEATURE_TABLE_LIMIT) and \
            not flags & KCOV_FLAG_SATURATED:
        raise ValueError('kcov_saturation_reason_without_saturated_flag')
    if count > 16_777_216:
        raise ValueError('kcov_bad_length')
    if count > FEATURE_TABLE_LIMIT:
        raise ValueError('kcov_bad_length')
    with path.open('rb') as stream:
        stream.seek(HEADER.size)
        raw = stream.read(WORD.size)
    if len(raw) != WORD.size:
        raise ValueError('kcov_short_feature_count')
    raw_count, = WORD.unpack(raw)
    record_size = 2 * WORD.size
    feature_offset = HEADER.size + WORD.size
    table_offset = feature_offset + count * record_size
    with path.open('rb') as stream:
        stream.seek(table_offset)
        raw = stream.read(WORD.size)
    if len(raw) != WORD.size:
        raise ValueError('kcov_short_call_table')
    call_count, = WORD.unpack(raw)
    if call_count > 256:
        raise ValueError('kcov_bad_call_count')
    expected_size = table_offset + WORD.size + call_count * 2 * WORD.size
    if path.stat().st_size != expected_size:
        raise ValueError('kcov_bad_length')
    saturated = bool(flags & KCOV_FLAG_SATURATED)
    if saturated and count > SATURATED_PARSE_LIMIT:
        return dict(version=5, raw_count=raw_count, saturated=True, pcs=[],
                    edges=set(), calls=[], ranges=[])
    data = path.read_bytes()
    features = [struct.unpack_from('<QQ', data, feature_offset + i * record_size)
                for i in range(count)]
    if any(left == 0 for left, _right in features):
        raise ValueError('kcov_zero_feature_pc')
    ranges_offset = table_offset + WORD.size
    calls = []
    expected_start = 0
    pcs = []
    edges = set()
    ranges = []
    for index in range(call_count):
        start, length = struct.unpack_from('<QQ', data,
                                          ranges_offset + index * 2 * WORD.size)
        if start != expected_start or start + length > count:
            raise ValueError('kcov_invalid_call_range')
        call_features = features[int(start):int(start + length)]
        if len(set(call_features)) != len(call_features):
            raise ValueError('kcov_duplicate_feature')
        call_pcs = [left for left, right in call_features if right == 0]
        call_edges = {(left, right) for left, right in call_features if right != 0}
        call_pc_set = set(call_pcs)
        if any(left not in call_pc_set or right not in call_pc_set
               for left, right in call_edges):
            raise ValueError('kcov_edge_endpoint_missing')
        ranges.append((len(pcs), len(call_pcs)))
        pcs.extend(call_pcs)
        edges.update(call_edges)
        calls.append(dict(pcs=call_pcs, edges=call_edges,
                          feature_count=int(length)))
        expected_start = start + length
    if expected_start != count:
        raise ValueError('kcov_uncovered_feature_range')
    pc_values = set(pcs)
    if (raw_count < len(pc_values) or
            any(left not in pc_values or right not in pc_values
                for left, right in edges)):
        raise ValueError('kcov_feature_raw_count_mismatch')
    if raw_count < sum(len(call['pcs']) for call in calls):
        raise ValueError('kcov_feature_raw_count_mismatch')
    return dict(version=5, raw_count=raw_count, saturated=saturated,
                pcs=pcs, edges=edges, calls=calls, ranges=ranges)


def read_sample_details(path: pathlib.Path) -> tuple[list[int], bool, list[tuple[int, int]]]:
    path = pathlib.Path(path)
    # Saturated KCOV buffers can contain millions of PCs.  The sample is
    # intentionally ineligible for corpus admission, so validate its header
    # and exact byte length without materializing/unpacking the whole array.
    with path.open('rb') as stream:
        header = stream.read(HEADER.size)
    if len(header) < HEADER.size:
        raise ValueError('kcov_short_header')
    magic, version, count, flags = HEADER.unpack(header)
    if version == 5:
        payload = read_sample_features(path)
        return payload['pcs'], payload['saturated'], payload['ranges']
    if (magic != MAGIC or version not in (1, 2, 3, 4) or
            (version == 2 and flags & ~KCOV_LEGACY_FLAGS) or
            (version in (3, 4) and flags & ~KCOV_KNOWN_FLAGS) or
            (version == 3 and flags & KCOV_FLAG_COMPACT) or
            (version == 4 and not flags & KCOV_FLAG_COMPACT) or
            flags & (KCOV_FLAG_FEATURE_SET | KCOV_FLAG_FEATURE_TABLE_LIMIT)):
        raise ValueError('kcov_bad_header')
    if version in (3, 4) and flags & (KCOV_FLAG_PER_CALL_FULL | KCOV_FLAG_TOTAL_LIMIT |
                                 KCOV_FLAG_IO_FAILURE) and not flags & KCOV_FLAG_SATURATED:
        raise ValueError('kcov_saturation_reason_without_saturated_flag')
    if count > 16_777_216:
        raise ValueError('kcov_bad_length')
    compact_raw_count = None
    token_size = WORD.size
    expected_size = HEADER.size + count * WORD.size
    if version == 4:
        with path.open('rb') as stream:
            stream.seek(HEADER.size)
            raw = stream.read(WORD.size)
        if len(raw) != WORD.size:
            raise ValueError('kcov_short_compact_count')
        compact_raw_count, = WORD.unpack(raw)
        token_size = 2 * WORD.size
        expected_size = HEADER.size + WORD.size + count * token_size
    call_count = 0
    if version in (3, 4):
        if not flags & KCOV_FLAG_CALL_RANGES:
            raise ValueError('kcov_call_ranges_flag_missing')
        with path.open('rb') as stream:
            stream.seek(expected_size)
            raw_count = stream.read(WORD.size)
        if len(raw_count) != WORD.size:
            raise ValueError('kcov_short_call_table')
        call_count, = WORD.unpack(raw_count)
        if call_count > 256:
            raise ValueError('kcov_bad_call_count')
        expected_size += WORD.size + call_count * 2 * WORD.size
    if path.stat().st_size != expected_size:
        raise ValueError('kcov_bad_length')
    saturated = bool(flags & KCOV_FLAG_SATURATED)
    if saturated and count > SATURATED_PARSE_LIMIT:
        return [], True, []
    data = path.read_bytes()
    if version == 4:
        tokens = [struct.unpack_from('<QQ', data, HEADER.size + WORD.size + i * token_size)
                  for i in range(count)]
        if any(run == 0 for _, run in tokens):
            raise ValueError('kcov_zero_compact_run')
        if sum(run for _, run in tokens) != compact_raw_count:
            raise ValueError('kcov_compact_count_mismatch')
        # A run of two is enough to retain the self edge; extra repetitions
        # do not add PC or edge features and would defeat compaction.
        pcs = []
        for pc, run in tokens:
            pcs.append(pc)
            if run > 1:
                pcs.append(pc)
    else:
        pcs = list(struct.unpack_from(f'<{count}Q', data, HEADER.size)) if count else []
    ranges = []
    if version in (3, 4):
        offset = expected_size - call_count * 2 * WORD.size
        # v4 ranges refer to compact tokens; convert them to the minimal
        # expanded sequence used by the existing edge accounting.
        expected_start = 0
        expanded_offset = 0
        for index in range(call_count):
            start, length = struct.unpack_from('<QQ', data, offset + index * 2 * WORD.size)
            if start != expected_start or start + length > count:
                raise ValueError('kcov_invalid_call_range')
            if version == 4:
                expanded_length = sum(1 + (tokens[i][1] > 1)
                                      for i in range(int(start), int(start + length)))
                ranges.append((expanded_offset, expanded_length))
                expanded_offset += expanded_length
            else:
                ranges.append((int(start), int(length)))
            expected_start = start + length
        if expected_start != count:
            raise ValueError('kcov_uncovered_pc_range')
        if version == 4 and sum(length for _, length in ranges) != len(pcs):
            raise ValueError('kcov_compact_range_mismatch')
    return pcs, saturated, ranges


def read_sample(path: pathlib.Path) -> tuple[list[int], bool]:
    pcs, saturated, _ = read_sample_details(path)
    return pcs, saturated


def sample_count(path: pathlib.Path) -> int:
    """Read the raw KCOV PC count without unpacking a compact payload."""
    with pathlib.Path(path).open('rb') as stream:
        header = stream.read(HEADER.size)
    if len(header) < HEADER.size:
        return 0
    _magic, version, count, _flags = HEADER.unpack(header)
    if version in (4, 5):
        with pathlib.Path(path).open('rb') as stream:
            stream.seek(HEADER.size)
            raw = stream.read(WORD.size)
        if len(raw) == WORD.size:
            return WORD.unpack(raw)[0]
    return count


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


X86_CANONICAL_TEXT_BASE = 0xffffffff81000000


TARGET_SYMBOL_PREFIXES = (
    'ext4_', '__ext4_', '_ext4_', 'ext4fs',
    'jbd2_', '__jbd2_', '_jbd2_', 'jbd2',
)
TARGET_SYMBOL_EXCLUDES = (
    '__pfx_', '__traceiter_', 'perf_trace_', 'trace_', '__event_',
    '__SCT__',
)
TARGET_SYMBOL_TYPES = frozenset(('T', 't', 'W', 'w'))


def parse_symbol_ranges(path: pathlib.Path, prefixes=None,
                        canonical_text_base=X86_CANONICAL_TEXT_BASE):
    """Return symbol ranges in the address space emitted by KCOV.

    ``/proc/kallsyms`` exposes the relocated runtime address, while
    ``__sanitizer_cov_trace_pc`` subtracts ``kaslr_offset()`` before writing a
    PC.  Comparing those two address spaces made every ext4 sample appear to
    be outside ext4 when KASLR was enabled.  The x86 kernel's canonical
    ``_stext`` is 0xffffffff81000000; derive the runtime relocation from the
    kallsyms ``_stext`` entry and normalize all symbols before building ranges.
    """
    # Only text/weak-text kallsyms entries are valid executable extents.
    # Data symbols can share an ext4/jbd2 prefix and would otherwise inflate
    # target coverage by classifying unrelated KCOV PCs as filesystem code.
    prefixes = TARGET_SYMBOL_PREFIXES if prefixes is None else tuple(prefixes)
    rows = []
    raw_lines = pathlib.Path(path).read_text(errors='replace').splitlines()
    runtime_text_base = None
    for raw in raw_lines:
        fields = raw.split()
        if len(fields) >= 3 and fields[2] in ('_stext', '_text') and \
                re.fullmatch(r'[0-9a-fA-F]+', fields[0]):
            runtime_text_base = int(fields[0], 16)
            if fields[2] == '_stext':
                break
    kaslr = (runtime_text_base - canonical_text_base
             if runtime_text_base is not None and runtime_text_base >= canonical_text_base
             else 0)
    for raw in raw_lines:
        fields = raw.split()
        if (len(fields) < 3 or fields[1] not in TARGET_SYMBOL_TYPES or
                not re.fullmatch(r'[0-9a-fA-F]+', fields[0])):
            continue
        name = fields[2].split('\t', 1)[0]
        # Keep every text symbol while deriving extents. Filtering before
        # finding the next address would make a target function range span
        # unrelated code between two ext4/JBD2 symbols.
        rows.append((int(fields[0], 16) - kaslr, name, fields[1]))
    rows.sort()
    return [(addr, rows[i + 1][0] if i + 1 < len(rows) else addr + 1, name)
            for i, (addr, name, _kind) in enumerate(rows)
            if name.startswith(prefixes) and not name.startswith(TARGET_SYMBOL_EXCLUDES)]


def classify_pcs(pcs, ranges):
    # Merge symbol extents once, then locate each PC by binary search.  A
    # linear scan over all ext4 symbols for every KCOV word made large but
    # complete samples take minutes to classify.
    merged = []
    for start, end, _ in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    starts = [row[0] for row in merged]
    related = []
    for pc in pcs:
        index = bisect_right(starts, pc) - 1
        if index >= 0 and pc < merged[index][1]:
            related.append(pc)
    return dict(ext4_pc_count=len(related), ext4_unique=len(set(related)), ext4_pcs=related)


def classify_target_pcs(pcs, ranges):
    """Split executable target PCs into ext4, JBD2, and their union.

    The legacy ``classify_pcs`` API intentionally remains generic because
    older evidence and tests pass synthetic ranges without symbol names.
    New campaigns use this named split so target admission cannot be caused by
    an unrelated kernel PC.
    """
    ext4_ranges = []
    jbd2_ranges = []
    for row in ranges:
        if len(row) < 3:
            continue
        name = row[2]
        if name.startswith(('ext4_', '__ext4_', '_ext4_', 'ext4fs')):
            ext4_ranges.append(row)
        elif name.startswith(('jbd2_', '__jbd2_', '_jbd2_', 'jbd2')):
            jbd2_ranges.append(row)
    ext4 = classify_pcs(pcs, ext4_ranges)['ext4_pcs']
    jbd2 = classify_pcs(pcs, jbd2_ranges)['ext4_pcs']
    return dict(ext4_pcs=ext4, jbd2_pcs=jbd2,
                target_pcs=sorted(set(ext4).union(jbd2)),
                ext4_unique=len(set(ext4)), jbd2_unique=len(set(jbd2)),
                target_unique=len(set(ext4).union(jbd2)))


COUNTS = ('expected_samples', 'received_samples', 'complete_samples',
          'saturated_samples', 'invalid_samples', 'missing_samples', 'unexpected_samples')


class FeedbackCorpus:
    def __init__(self, run: pathlib.Path, scheduler_policy='uniform_round_robin',
                 feedback_mode='pc_edge_semantic', seed=1,
                 target_scope='all'):
        if target_scope not in ('all', 'ext4_jbd2'):
            raise ValueError('unknown target_scope:' + str(target_scope))
        self.run = pathlib.Path(run)
        self.feedback = self.run / 'feedback'
        self.corpus = self.run / 'corpus'
        self.feedback.mkdir(exist_ok=True)
        self.corpus.mkdir(exist_ok=True)
        self.coverage: set[int] = set()
        self.edges: set[tuple[int, int]] = set()
        self.parents: list[pathlib.Path] = []
        self.parent_by_hash: dict[str, pathlib.Path] = {}
        self.parent_stats: dict[str, dict] = {}
        self.parent_cursor = 0
        self.selection_count = 0
        self.counts = dict.fromkeys(COUNTS, 0)
        self.saturation_reasons: dict[str, int] = {}
        self.gaps: list[dict] = []
        self.ext4_ranges = []
        self.target_scope = target_scope
        self.ext4_coverage: set[int] = set()
        self.ext4_pc_coverage: set[int] = set()
        self.jbd2_pc_coverage: set[int] = set()
        self.target_pc_coverage: set[int] = set()
        self.corpus_pc_coverage: set[int] = set()
        self.corpus_ext4_pc_coverage: set[int] = set()
        self.corpus_jbd2_pc_coverage: set[int] = set()
        self.corpus_target_pc_coverage: set[int] = set()
        self.formula_counts: dict[str, int] = {}
        self.semantic_features: set[str] = set()
        self.mismatch_signatures: set[str] = set()
        self.unverified_signatures: set[str] = set()
        self.pending_samples: dict[str, list[dict]] = {}
        self.scheduler_policy = scheduler_policy
        self.feedback_mode = feedback_mode
        self.seed = int(seed)
        self.feature_frequency: dict[str, int] = {}
        self.parent_features: dict[str, set[str]] = {}
        self.parent_signal_kinds: dict[str, str] = {}
        self.admission_parent_counts: dict[str, int] = {
            'pc': 0, 'edge_only': 0, 'semantic_only': 0,
        }
        self.selection_parent_counts: dict[str, int] = {
            'pc': 0, 'edge_only': 0, 'semantic_only': 0, 'unknown': 0,
        }
        self.edge_only_admissions_suppressed = 0
        self.semantic_only_admissions_suppressed = 0

    @property
    def samples(self):
        return self.counts['complete_samples']  # compatibility: accepted complete samples

    @property
    def saturated_samples(self):
        return self.counts['saturated_samples']

    def summary(self):
        return dict(schema=5, **self.counts, samples=self.samples,
                    total_coverage=len(self.coverage), corpus_size=len(self.parents),
                    total_ext4_coverage=len(self.ext4_coverage),
                    # ``total_ext4_coverage`` is a compatibility alias for
                    # the old range view. New comparisons must use the
                    # explicitly named target counters below.
                    ext4_pc_coverage=len(self.ext4_pc_coverage),
                    jbd2_pc_coverage=len(self.jbd2_pc_coverage),
                    target_pc_coverage=len(self.target_pc_coverage),
                    target_scope=self.target_scope,
                    target_fraction=(len(self.target_pc_coverage) /
                                     len(self.coverage) if self.coverage else 0.0),
                    # The primary custom view includes every complete,
                    # verified sample.  The corpus view below is restricted
                    # to admitted programs and is comparable with the
                    # syz-manager corpus coverage endpoints.
                    coverage_scope='all_complete_samples',
                    corpus_coverage_scope='admitted_corpus',
                    corpus_pc_coverage=len(self.corpus_pc_coverage),
                    corpus_ext4_pc_coverage=len(self.corpus_ext4_pc_coverage),
                    corpus_jbd2_pc_coverage=len(self.corpus_jbd2_pc_coverage),
                    corpus_target_pc_coverage=len(self.corpus_target_pc_coverage),
                    corpus_target_fraction=(
                        len(self.corpus_target_pc_coverage) /
                        len(self.corpus_pc_coverage)
                        if self.corpus_pc_coverage else 0.0),
                    total_edges=len(self.edges),
                    total_semantic_features=len(self.semantic_features),
                    parent_selections=self.selection_count,
                    admission_parent_counts=dict(self.admission_parent_counts),
                    selection_parent_counts=dict(self.selection_parent_counts),
                    edge_only_selection_share=(
                        self.selection_parent_counts.get('edge_only', 0) /
                        self.selection_count if self.selection_count else 0.0),
                    edge_only_parent_limit=self.edge_only_parent_limit(),
                    semantic_only_parent_limit=self.semantic_only_parent_limit(),
                    edge_only_admissions_suppressed=self.edge_only_admissions_suppressed,
                    semantic_only_admissions_suppressed=self.semantic_only_admissions_suppressed,
                    yield_aware_scheduler=True,
                    yield_ext4_weight=YIELD_EXT4_WEIGHT,
                    yield_pc_weight=YIELD_PC_WEIGHT,
                    rarity_weights=dict(RARITY_WEIGHTS),
                    zero_yield_cooldown_streak=ZERO_YIELD_COOLDOWN_STREAK,
                    cooldown_yield_basis='new_pc_or_ext4_pc',
                    zero_any_signal_streak_diagnostic=True,
                    yield_exploration_interval=YIELD_EXPLORATION_INTERVAL,
                    scheduler_policy=self.scheduler_policy,
                    feedback_mode=self.feedback_mode,
                    saturation_reasons=dict(sorted(self.saturation_reasons.items())),
                    samples_complete=not self.gaps and self.counts['expected_samples'] > 0,
                    feedback_complete=not self.gaps and self.counts['expected_samples'] > 0,
                    gaps=list(self.gaps))

    def parent_candidate(self) -> dict | None:
        if not self.parents:
            return None
        index = self._choose_parent_index()
        path = self.parents[index]
        key = _sha(path)
        stats = self._parent_stats(key, path)
        components = self.rarity_components(key)
        return dict(path=path, sha256=key, cursor=self.parent_cursor,
                    selection_index=self.selection_count, parent_index=index,
                    rarity_score=self.rarity_score(key),
                    rarity_components=components,
                    rarity_pc=components['pc'], rarity_ext4=components['ext4'],
                    rarity_edge=components['edge'], rarity_semantic=components['sem'],
                    yield_score=self.parent_yield_score(key),
                    yield_ema_pc=float(stats.get('yield_ema_pc', 0.0) or 0.0),
                    yield_ema_ext4=float(stats.get('yield_ema_ext4', 0.0) or 0.0),
                    zero_target_yield_streak=int(stats.get(
                        'zero_target_yield_streak', stats.get('zero_yield_streak', 0)) or 0),
                    zero_any_signal_streak=int(stats.get(
                        'zero_any_signal_streak', 0) or 0),
                    # Compatibility field; zero_yield_streak now means the
                    # target-PC streak everywhere in new scheduler records.
                    zero_yield_streak=int(stats.get(
                        'zero_target_yield_streak', stats.get('zero_yield_streak', 0)) or 0),
                    yield_observations=int(stats.get('yield_observations', 0) or 0),
                    admission_kind=self.parent_signal_kinds.get(key, 'unknown'))

    def _parent_stats(self, parent_hash, path=None):
        """Return a complete, backward-compatible parent statistics record."""
        defaults = dict(path=str(path) if path is not None else None,
                        selections=0, mutations=0, own_new_pcs=0,
                        own_new_ext4_pcs=0, own_new_semantic_features=0,
                        admitted_executions=0, child_executions=0,
                        child_new_pcs=0, child_new_ext4_pcs=0,
                        child_new_edges=0, child_new_semantic_features=0,
                        yield_ema_pc=0.0, yield_ema_ext4=0.0,
                        yield_ema_edges=0.0, yield_ema_semantic=0.0,
                        zero_yield_streak=0, zero_target_yield_streak=0,
                        zero_any_signal_streak=0, yield_observations=0)
        stats = self.parent_stats.setdefault(parent_hash, defaults)
        for field, value in defaults.items():
            if field not in stats:
                if field == 'zero_target_yield_streak':
                    # Old in-memory/serialized records used a mixed-signal
                    # streak. Preserve its history when upgrading a corpus.
                    stats[field] = int(stats.get('zero_yield_streak', value) or 0)
                elif field == 'zero_yield_streak':
                    stats[field] = int(stats.get('zero_target_yield_streak', value) or 0)
                else:
                    stats[field] = value
        if path is not None and not stats.get('path'):
            stats['path'] = str(path)
        return stats

    def rarity_components(self, parent_hash):
        """Return bounded rarity components for the target and side signals.

        A mean rarity multiplied by logarithmic feature breadth preserves the
        value of rare features without allowing a trace with thousands of
        edges to dominate a parent solely by feature count.  The final cap is
        also important for deterministic long campaigns where a new signal
        family can otherwise change scheduler scale abruptly.
        """
        buckets = {name: [] for name in RARITY_WEIGHTS}
        for feature in self.parent_features.get(parent_hash, set()):
            prefix = feature.split(':', 1)[0]
            if prefix in buckets:
                buckets[prefix].append(
                    1.0 / max(1, self.feature_frequency.get(feature, 1)))
        components = {}
        for name, values in buckets.items():
            if not values:
                components[name] = 0.0
                continue
            breadth = min(4.0, math.log1p(len(values)))
            components[name] = min(RARITY_COMPONENT_CAP,
                                   (sum(values) / len(values)) * breadth)
        components['weighted'] = sum(
            RARITY_WEIGHTS[name] * components[name] for name in RARITY_WEIGHTS)
        return components

    def rarity_score(self, parent_hash):
        return self.rarity_components(parent_hash)['weighted']

    def parent_yield_score(self, parent_hash):
        """Return observed target-coverage yield per mutated child.

        New parents receive a neutral score, while repeatedly unproductive
        parents retain only the exploration floor. The logarithm prevents a
        single productive parent from monopolising mutation energy.
        """
        stats = self._parent_stats(parent_hash)
        executions = int(stats.get('yield_observations', 0) or
                         stats.get('child_executions', 0) or 0)
        if executions <= 0:
            return 1.0
        new_pcs = max(0.0, float(stats.get('yield_ema_pc', 0.0) or 0.0))
        new_ext4 = max(0.0, float(stats.get('yield_ema_ext4', 0.0) or 0.0))
        # Legacy records have cumulative counters but no EMA fields.
        if (not stats.get('yield_observations') or
                ((not stats.get('yield_ema_pc') and
                  not stats.get('yield_ema_ext4')) and
                 (stats.get('child_new_pcs', 0) or
                  stats.get('child_new_ext4_pcs', 0)))):
            new_pcs = max(0.0, float(stats.get('child_new_pcs', 0) or 0)) / executions
            new_ext4 = max(0.0, float(stats.get('child_new_ext4_pcs', 0) or 0)) / executions
        raw = YIELD_PC_WEIGHT * new_pcs + YIELD_EXT4_WEIGHT * new_ext4
        return round(math.log1p(raw), 6)

    def parent_selection_weight(self, parent_hash):
        """Combine rarity with measured child yield for weighted scheduling."""
        rarity = max(0.0, self.rarity_score(parent_hash))
        # Preserve the existing rarity pressure; yield is an additional
        # multiplier, so an unseen but rare feature still gets explored.
        rarity_weight = 1.0 + math.log1p(rarity)
        stats = self._parent_stats(parent_hash)
        executions = int(stats.get('yield_observations', 0) or
                         stats.get('child_executions', 0) or 0)
        if executions <= 0:
            yield_factor = 1.0
        else:
            yield_score = self.parent_yield_score(parent_hash)
            yield_factor = (YIELD_EXPLORATION_FLOOR if yield_score <= 0.0 else
                            1.0 + min(1.75, yield_score))
        return rarity_weight * yield_factor

    def _eligible_parent_indices(self):
        indices = list(range(len(self.parents)))
        if not indices:
            return indices
        # The prospective selection is included in the denominator. This
        # bounds side-signal mutation energy while retaining an exploration
        # lane when no PC-bearing parent is available yet.
        side_limits = {
            'edge_only': EDGE_ONLY_SELECTION_SHARE,
            'semantic_only': SEMANTIC_ONLY_SELECTION_SHARE,
        }
        over_quota = {
            kind for kind, limit in side_limits.items()
            if ((self.selection_parent_counts.get(kind, 0) + 1) /
                (self.selection_count + 1) > limit)
        }
        if over_quota:
            regular = [index for index in indices
                       if self.parent_signal_kinds.get(_sha(self.parents[index]),
                                                       'unknown') not in over_quota]
            indices = regular or indices
        # The normal rarity pool excludes parents with a repeated zero-yield
        # streak. Every fixed interval reopens the complete eligible pool so
        # a parent can recover after global coverage changes.
        if (self.scheduler_policy == 'rarity_fair' and
                self.selection_count % YIELD_EXPLORATION_INTERVAL != 0):
            active = []
            for index in indices:
                stats = self._parent_stats(_sha(self.parents[index]))
                streak = stats.get('zero_target_yield_streak',
                                   stats.get('zero_yield_streak', 0))
                if int(streak or 0) < ZERO_YIELD_COOLDOWN_STREAK:
                    active.append(index)
            indices = active or indices
        return indices

    def _choose_parent_index(self):
        eligible = self._eligible_parent_indices()
        if self.scheduler_policy == 'uniform_round_robin':
            return eligible[self.parent_cursor % len(eligible)]
        if self.scheduler_policy != 'rarity_fair':
            raise ValueError('unknown_scheduler_policy:' + self.scheduler_policy)
        max_deferral = max(8, 2 * len(eligible))
        overdue = [(self.selection_count - self.parent_stats.get(_sha(path), {}).get(
            'last_selected', -1), i) for i in eligible
                   for path in [self.parents[i]]]
        gap, overdue_index = max(overdue)
        if gap >= max_deferral:
            return overdue_index
        weights = [self.parent_selection_weight(_sha(self.parents[i]))
                   for i in eligible]
        digest = hashlib.sha256(f'{self.seed}:{self.selection_count}'.encode()).digest()
        needle = int.from_bytes(digest[:8], 'big') / 2**64 * sum(weights)
        for index, weight in zip(eligible, weights):
            needle -= weight
            if needle < 0:
                return index
        return eligible[-1]

    def mutation_budget(self, candidate, base_calls, max_calls=256):
        if not candidate or self.scheduler_policy != 'rarity_fair':
            return int(base_calls)
        score = max(0.0, float(candidate.get('rarity_score', 0.0)))
        yield_score = max(0.0, float(candidate.get('yield_score', 0.0)))
        # Rarity alone receives a modest bounded boost. Recent target yield
        # gets a similar boost, while exhausted parents are deliberately
        # reduced even when their old features remain rare.
        # The upper bound is reached only for synthetic/extreme scores; real
        # component scores are capped well below it.
        multiplier = 1.0 + min(0.50, math.log1p(score) / 12.0)
        multiplier += min(0.35, yield_score / 10.0)
        zero_streak = max(0, int(candidate.get(
            'zero_target_yield_streak', candidate.get('zero_yield_streak', 0)) or 0))
        multiplier -= min(0.45, max(0, zero_streak - 2) * 0.15)
        multiplier = max(0.50, multiplier)
        return max(1, min(int(max_calls), round(int(base_calls) * multiplier)))

    def edge_only_parent_limit(self) -> int:
        """Return the active edge-only parent quota.

        The minimum keeps edge exploration alive before any PC-bearing parent
        has been found.  Afterwards the quota grows slowly with the number of
        PC-bearing parents, so adding an edge signal cannot dilute the main
        mutation pool one-for-one.
        """
        pc_parents = self.admission_parent_counts.get('pc', 0)
        return max(MIN_EDGE_ONLY_PARENTS,
                   math.ceil(pc_parents * EDGE_ONLY_PARENT_RATIO))

    def semantic_only_parent_limit(self) -> int:
        pc_parents = self.admission_parent_counts.get('pc', 0)
        return max(MIN_SEMANTIC_ONLY_PARENTS,
                   math.ceil(pc_parents * SEMANTIC_ONLY_PARENT_RATIO))

    def _admission_kind(self, signals):
        signals = tuple(signals or ())
        if signals == ('edge',):
            return 'edge_only'
        if signals == ('semantic',):
            return 'semantic_only'
        if 'pc' in signals:
            return 'pc'
        # Keep this fallback explicit for future feedback signals.  Such a
        # signal should not silently consume the edge-only exploration lane.
        return 'semantic_only'

    def _edge_only_parent_allowed(self, program_hash):
        # Re-admitting the same program does not create another parent and is
        # therefore harmless even after the quota has been reached.
        if program_hash in self.parent_signal_kinds:
            return True
        return (self.admission_parent_counts.get('edge_only', 0) <
                self.edge_only_parent_limit())

    def _side_only_parent_allowed(self, program_hash, kind):
        if program_hash in self.parent_signal_kinds:
            return True
        if kind == 'edge_only':
            return self._edge_only_parent_allowed(program_hash)
        if kind == 'semantic_only':
            return (self.admission_parent_counts.get('semantic_only', 0) <
                    self.semantic_only_parent_limit())
        return True

    def _record_child_yield(self, parent_hash, feature_summary, path=None):
        """Record one mutated child and update its exponentially weighted yield."""
        stats = self._parent_stats(parent_hash, path)
        # Target-scoped runs use ext4/JBD2 novelty for yield and cooldown;
        # generic kernel PCs remain diagnostic only.
        new_pc = max(0, int(feature_summary.get(
            'target_new', feature_summary.get('new_pcs', 0)) or 0))
        new_ext4 = max(0, int(feature_summary.get(
            'ext4_new', feature_summary.get('new_ext4_pcs', 0)) or 0))
        new_edge = max(0, int(feature_summary.get('new_edges', 0) or 0))
        new_semantic = max(0, int(
            feature_summary.get('new_semantic_features', 0) or 0))
        stats['child_executions'] = int(stats.get('child_executions', 0) or 0) + 1
        stats['child_new_pcs'] = int(stats.get('child_new_pcs', 0) or 0) + new_pc
        stats['child_new_ext4_pcs'] = int(
            stats.get('child_new_ext4_pcs', 0) or 0) + new_ext4
        stats['child_new_edges'] = int(stats.get('child_new_edges', 0) or 0) + new_edge
        stats['child_new_semantic_features'] = int(
            stats.get('child_new_semantic_features', 0) or 0) + new_semantic
        stats['yield_observations'] = int(stats.get('yield_observations', 0) or 0) + 1
        alpha = YIELD_EMA_ALPHA
        for field, value in (('yield_ema_pc', new_pc),
                             ('yield_ema_ext4', new_ext4),
                             ('yield_ema_edges', new_edge),
                             ('yield_ema_semantic', new_semantic)):
            previous = float(stats.get(field, 0.0) or 0.0)
            stats[field] = (1.0 - alpha) * previous + alpha * value
        target_yield = bool(new_pc or new_ext4)
        any_yield = bool(target_yield or new_edge or new_semantic)
        if target_yield:
            stats['zero_target_yield_streak'] = 0
        else:
            stats['zero_target_yield_streak'] = int(
                stats.get('zero_target_yield_streak',
                          stats.get('zero_yield_streak', 0)) or 0) + 1
        if any_yield:
            stats['zero_any_signal_streak'] = 0
        else:
            stats['zero_any_signal_streak'] = int(
                stats.get('zero_any_signal_streak', 0) or 0) + 1
        # Retain the old key for consumers, with the corrected target meaning.
        stats['zero_yield_streak'] = stats['zero_target_yield_streak']

    def commit_parent(self, candidate: dict) -> None:
        """Advance the fair scheduler only after the generator made a mutation."""
        if candidate['cursor'] != self.parent_cursor:
            raise RuntimeError('stale_parent_candidate')
        path = pathlib.Path(candidate['path'])
        if path not in self.parents:
            raise RuntimeError('selected_parent_missing_from_corpus')
        key = candidate['sha256']
        stats = self._parent_stats(key, path)
        stats['selections'] += 1
        stats['mutations'] += 1
        stats['last_selected'] = self.selection_count
        kind = self.parent_signal_kinds.get(key, 'unknown')
        self.selection_parent_counts[kind] = \
            self.selection_parent_counts.get(kind, 0) + 1
        self.parent_cursor += 1
        self.selection_count += 1
        self.write_scheduler()

    def parent(self, ordinal=None) -> pathlib.Path | None:
        """Compatibility helper; production uses candidate/commit transaction."""
        candidate = self.parent_candidate()
        if candidate is None:
            return None
        self.commit_parent(candidate)
        return pathlib.Path(candidate['path'])

    def write_scheduler(self):
        parents = []
        for path in self.parents:
            key = _sha(path)
            record = dict(self.parent_stats.get(key, {}))
            record.update(sha256=key, path=str(path.relative_to(self.run)))
            record['admission_kind'] = self.parent_signal_kinds.get(key, 'unknown')
            record['rarity_score'] = round(self.rarity_score(key), 6)
            components = {
                name: round(value, 6)
                for name, value in self.rarity_components(key).items()
            }
            record['rarity_components'] = components
            record['rarity_pc'] = components['pc']
            record['rarity_ext4'] = components['ext4']
            record['rarity_edge'] = components['edge']
            record['rarity_semantic'] = components['sem']
            record['yield_score'] = self.parent_yield_score(key)
            record['zero_target_yield_streak'] = int(record.get(
                'zero_target_yield_streak', record.get('zero_yield_streak', 0)) or 0)
            record['zero_any_signal_streak'] = int(
                record.get('zero_any_signal_streak', 0) or 0)
            record['zero_yield_streak'] = record['zero_target_yield_streak']
            record['selection_weight'] = round(self.parent_selection_weight(key), 6)
            parents.append(record)
        payload = dict(schema=2, policy=self.scheduler_policy,
                       feedback_mode=self.feedback_mode,
                       parent_cursor=self.parent_cursor,
                       selection_count=self.selection_count,
                       admission_parent_counts=dict(self.admission_parent_counts),
                       selection_parent_counts=dict(self.selection_parent_counts),
                       edge_only_selection_share=(
                           self.selection_parent_counts.get('edge_only', 0) /
                           self.selection_count if self.selection_count else 0.0),
                       edge_only_parent_limit=self.edge_only_parent_limit(),
                       semantic_only_parent_limit=self.semantic_only_parent_limit(),
                       edge_only_admissions_suppressed=self.edge_only_admissions_suppressed,
                       semantic_only_admissions_suppressed=self.semantic_only_admissions_suppressed,
                       yield_aware_scheduler=True,
                       yield_ext4_weight=YIELD_EXT4_WEIGHT,
                       yield_pc_weight=YIELD_PC_WEIGHT,
                       rarity_weights=dict(RARITY_WEIGHTS),
                       zero_yield_cooldown_streak=ZERO_YIELD_COOLDOWN_STREAK,
                       cooldown_yield_basis='new_pc_or_ext4_pc',
                       zero_any_signal_streak_diagnostic=True,
                       yield_exploration_interval=YIELD_EXPLORATION_INTERVAL,
                       parents=parents)
        (self.feedback / 'scheduler.json').write_text(json.dumps(payload, indent=2) + '\n')

    def _admit_parent(self, source: pathlib.Path, features: dict) -> pathlib.Path:
        program_hash = _sha(source)
        target = self.parent_by_hash.get(program_hash)
        if target is None:
            target = self.corpus / (program_hash + '.syz')
            if not target.exists():
                shutil.copy2(source, target)
            self.parent_by_hash[program_hash] = target
            self.parents.append(target)
        stats = self._parent_stats(program_hash, target)
        stats.setdefault('admission_signals', [])
        stats['own_new_pcs'] += features.get(
            'target_new', features.get('new_pcs', 0))
        stats['own_new_ext4_pcs'] += features.get(
            'ext4_new', features.get('new_ext4_pcs', 0))
        stats['own_new_semantic_features'] += features.get('new_semantic_features', 0)
        stats['admitted_executions'] += 1
        # A program can be observed more than once.  Preserve all observed
        # signals and promote an edge-only parent when a later replay proves
        # that it also carries a new PC or semantic feature.  Without this
        # promotion the parent would consume an edge quota slot forever.
        observed_signals = set(stats.get('admission_signals', []))
        observed_signals.update(features.get('admission_signals', []))
        stats['admission_signals'] = sorted(observed_signals)
        previous_kind = self.parent_signal_kinds.get(program_hash)
        current_kind = self._admission_kind(stats['admission_signals'])
        if previous_kind is None:
            self.parent_signal_kinds[program_hash] = current_kind
            self.admission_parent_counts[current_kind] = \
                self.admission_parent_counts.get(current_kind, 0) + 1
        elif previous_kind != current_kind:
            self.admission_parent_counts[previous_kind] = max(
                0, self.admission_parent_counts.get(previous_kind, 0) - 1)
            self.admission_parent_counts[current_kind] = \
                self.admission_parent_counts.get(current_kind, 0) + 1
            self.parent_signal_kinds[program_hash] = current_kind
        self.parent_features.setdefault(program_hash, set()).update(
            features.get('feature_ids', []))
        return target

    def collect(self, folder: pathlib.Path, manifest: dict) -> dict:
        folder = pathlib.Path(folder)
        samples = {p.stem: p for p in sorted((folder / 'coverage').glob('*.bin'))}
        # Runtime reports executed batches, which can be fewer than exported
        # batches. The runner supplies their identities before collection.
        expected = manifest.get('expected_programs')
        if expected is None:
            expected = [p.parent.name for p in sorted((folder / 'syz-programs').glob('*/program.syz'))]
        expected = set(expected)
        rows, errors, segment_new, segment_new_edges = [], [], set(), set()
        segment_new_target = set()
        seen_pcs, seen_ext4, seen_target, seen_edges = set(), set(), set(), set()
        pending = []
        counts = dict.fromkeys(COUNTS, 0)
        counts.update(expected_samples=len(expected), received_samples=len(samples),
                      missing_samples=len(expected - samples.keys()),
                      unexpected_samples=len(samples.keys() - expected))
        for program in sorted(expected - samples.keys()):
            errors.append(program + ':missing')
        for program, sample in samples.items():
            row = dict(program=program, admitted=False)
            rows.append(row)
            if program not in expected:
                errors.append(program + ':unexpected')
                row.update(valid=False, reason='unexpected')
                continue
            try:
                diagnostics = read_sample_diagnostics(sample)
                pcs, saturated, ranges = read_sample_details(sample)
                feature_payload = (read_sample_features(sample)
                                   if diagnostics.get('feature_set') else None)
            except (OSError, ValueError) as exc:
                counts['invalid_samples'] += 1
                errors.append(f'{program}:{exc}')
                row.update(valid=False, reason=str(exc))
                continue
            pcs_set = set(pcs)
            fresh = pcs_set - self.coverage - seen_pcs
            pc_info = classify_pcs(pcs, self.ext4_ranges)
            target_info = classify_target_pcs(pcs, self.ext4_ranges)
            ext4_set = set(pc_info['ext4_pcs'])
            ext4_fresh = ext4_set - self.ext4_coverage - seen_ext4
            target_set = set(target_info['target_pcs'])
            target_fresh = target_set - self.target_pc_coverage - seen_target
            edges = set()
            call_rows = []
            program_seen_pcs, program_seen_edges = set(), set()
            call_inputs = (feature_payload['calls'] if feature_payload else None)
            for call_index, (start, length) in enumerate(ranges):
                sequence = pcs[start:start + length]
                call_pcs = (set(call_inputs[call_index]['pcs'])
                            if call_inputs and call_index < len(call_inputs)
                            else set(sequence))
                call_edges = (set(call_inputs[call_index]['edges'])
                              if call_inputs and call_index < len(call_inputs)
                              else set(zip(sequence, sequence[1:])))
                call_fresh_pcs = call_pcs - self.coverage - seen_pcs - program_seen_pcs
                call_fresh_edges = call_edges - self.edges - seen_edges - program_seen_edges
                edges.update(call_edges)
                call_rows.append(dict(call_index=call_index, pc_count=len(sequence),
                                      unique_pcs=len(call_pcs), new_pcs=len(call_fresh_pcs),
                                      edges=len(call_edges), new_edges=len(call_fresh_edges)))
                program_seen_pcs.update(call_pcs)
                program_seen_edges.update(call_edges)
            if feature_payload:
                edges = set(feature_payload['edges'])
            fresh_edges = edges - self.edges - seen_edges
            row.update(valid=True, count=sample_count(sample), unique=len(pcs_set), new=len(fresh),
                       ext4_pc_count=pc_info['ext4_pc_count'], ext4_unique=pc_info['ext4_unique'],
                       ext4_new=len(ext4_fresh), new_ext4_pcs=len(ext4_fresh),
                       ext4_unique_target=target_info['ext4_unique'],
                       jbd2_unique=target_info['jbd2_unique'],
                       target_unique=target_info['target_unique'],
                       target_new=len(target_fresh), new_target_pcs=len(target_fresh),
                       edge_count=len(edges), new_edges=len(fresh_edges),
                       calls=call_rows,
                       compact=bool(diagnostics.get('compact', False)),
                       feature_set=bool(diagnostics.get('feature_set', False)),
                       compact_raw_count=diagnostics.get('compact_raw_count'),
                       feature_count=diagnostics.get('feature_count'),
                       overflow=saturated, saturated=saturated,
                       saturation_reasons=(diagnostics['saturation_reasons'] or
                           (['legacy_or_unclassified_saturation'] if saturated else [])),
                       sha256=_sha(sample))
            if saturated:
                counts['saturated_samples'] += 1
                for reason in row['saturation_reasons']:
                    self.saturation_reasons[reason] = self.saturation_reasons.get(reason, 0) + 1
                errors.append(program + ':saturated')
                continue
            counts['complete_samples'] += 1
            seen_pcs.update(pcs_set)
            seen_ext4.update(ext4_set)
            seen_target.update(target_set)
            seen_edges.update(edges)
            segment_new.update(fresh)
            segment_new_target.update(target_fresh)
            segment_new_edges.update(fresh_edges)
            pending.append(dict(program=program, pcs=pcs_set,
                                ext4_pcs=set(pc_info['ext4_pcs']), sample=sample,
                                row=row, fresh=fresh, ext4_fresh=ext4_fresh,
                                target_ext4_pcs=set(target_info['ext4_pcs']),
                                target_jbd2_pcs=set(target_info['jbd2_pcs']),
                                target_pcs=target_set, target_fresh=target_fresh,
                                edges=edges, fresh_edges=fresh_edges))
        if not expected:
            errors.append('no_expected_programs')
        self.pending_samples[folder.name] = pending
        for key in COUNTS:
            self.counts[key] += counts[key]
        if errors:
            self.gaps.append(dict(segment_id=manifest.get('segment_id'), errors=errors))
        segment = dict(schema=4, segment_id=manifest.get('segment_id'), **counts,
                       expected_programs=sorted(expected), programs=rows,
                       new_coverage=len(segment_new), total_coverage=len(self.coverage),
                       new_target_pc_coverage=len(segment_new_target),
                       target_pc_coverage=len(self.target_pc_coverage),
                       target_scope=self.target_scope,
                       new_edges=len(segment_new_edges), total_edges=len(self.edges),
                       corpus_size=len(self.parents), samples_complete=not errors,
                       errors=errors, passed=not errors)
        campaign = self.summary()
        (folder / 'coverage.json').write_text(json.dumps(segment, indent=2) + '\n')
        (self.feedback / 'coverage.json').write_text(json.dumps(campaign, indent=2) + '\n')
        manifest['coverage'] = segment
        # Top-level aliases preserve existing callers; explicitly named
        # summaries prevent segment and campaign completeness being confused.
        return dict(segment, segment_summary=segment, campaign_summary=campaign)

    def augment(self, folder: pathlib.Path, report: dict, native_execution_verified=True,
                timeline_path: pathlib.Path | None = None, mode='single') -> dict:
        pending = self.pending_samples.pop(pathlib.Path(folder).name, [])
        admitted = []
        admission_signals = {'pc': 0, 'edge': 0, 'semantic': 0}
        semantic_only_admitted = []
        segment_new = set()
        program_feedback = {}
        timeline_path = pathlib.Path(timeline_path) if timeline_path else None
        semantic_eligible = bool(native_execution_verified and mode == 'single' and
            timeline_path and timeline_path.is_file() and report.get('runs') and
            report.get('event_sources', {}).get('format') == 'explicit_phases' and
            all(run.get('trajectory_integrity') for run in report.get('runs', [])))
        # Snapshot, cleanup, and asynchronous kernel events are expected to be
        # outside a generated syscall. Semantic feedback only requires events
        # inside verified generated syscall windows to be uniquely attributed;
        # whole-trace attribution remains a separate diagnostic metric.
        timeline_complete = bool(semantic_eligible and
            report.get('event_sources', {}).get(
                'generated_syscall_attribution_complete',
                report.get('event_sources', {}).get('source_attribution_complete')))
        syscall_outcomes = {}
        for call in report.get('native_syscalls', []):
            if call.get('program') is None or call.get('call_index') is None:
                continue
            syscall_outcomes[(call['program'], int(call['call_index']))] = (
                'success' if call.get('errno', 0) == 0 else 'error')
        operation_outcomes = {}
        for run in report.get('runs', []):
            # trace_lifecycle serializes native syscall exits as `boundaries`.
            # Keep the old key as a compatibility fallback for existing reports.
            for operation in run.get('boundaries', run.get('operations', [])):
                if (operation.get('kind') == 'native_syscall_return' and
                        operation.get('program') is not None and
                        operation.get('call_index') is not None):
                    operation_outcomes[(operation['program'], int(operation['call_index']))] = \
                        operation.get('classification', 'unknown')
        if semantic_eligible and timeline_path and timeline_path.is_file():
            with timeline_path.open() as timeline:
              for raw in timeline:
                try:
                    row = json.loads(raw)
                    event = row.get('event', {})
                    program = row.get('program')
                    call_index = row.get('call_index')
                    if (not program or call_index is None or
                            row.get('event_source') != 'generated_syscall' or
                            row.get('attribution') != 'same_task_native_syscall' or
                            int(event.get('status', 3)) == 3):
                        continue
                    status = int(event.get('status', 3))
                    if status not in (0, 1, 2):
                        continue
                    delta = None
                    if status in (1, 2):
                        delta = int(event['actual']) - int(event['expected'])
                    bucket = ('na' if delta is None else '0' if delta == 0 else
                              '+1' if delta == 1 else '-1' if delta == -1 else
                              '>1' if delta > 1 else '<-1')
                    key = '|'.join(str(value) for value in
                                   (event.get('formula'), event.get('point'), status,
                                    row.get('value_kind'), bucket,
                                    syscall_outcomes.get((program, int(call_index)), 'unknown'),
                                    operation_outcomes.get((program, int(call_index)), 'unknown')))
                    item = program_feedback.setdefault(program, dict(features=set(), calls=set(),
                                                                   evidence=set()))
                    item['features'].add(key)
                    item['calls'].add(int(call_index))
                    item['evidence'].add((key, int(call_index)))
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    continue
        if native_execution_verified:
            # Count observed feature frequency before calculating rarity. Each
            # program contributes at most one hit per feature per execution.
            for item in pending:
                scored_pcs = (item.get('target_pcs', set())
                              if self.target_scope == 'ext4_jbd2'
                              else item['pcs'])
                scored_ext4 = (item.get('target_ext4_pcs', set())
                               if self.target_scope == 'ext4_jbd2'
                               else item['ext4_pcs'])
                ids = {f'pc:{pc:x}' for pc in scored_pcs}
                ids.update(f'ext4:{pc:x}' for pc in scored_ext4)
                ids.update(f'edge:{left:x}:{right:x}' for left, right in item['edges'])
                for feature in ids:
                    self.feature_frequency[feature] = self.feature_frequency.get(feature, 0) + 1
            for item in pending:
                self.coverage.update(item['pcs'])
                self.ext4_coverage.update(item['ext4_pcs'])
                target_pcs = item.get('target_pcs', set())
                self.ext4_pc_coverage.update(item.get('target_ext4_pcs', set()))
                self.jbd2_pc_coverage.update(item.get('target_jbd2_pcs', set()))
                self.target_pc_coverage.update(target_pcs)
                self.edges.update(item['edges'])
                segment_new.update(item['fresh'])
                row = item['row']
                row['native_verified'] = True
                per_program = program_feedback.setdefault(item['program'],
                    dict(features=set(), calls=set(), evidence=set()))
                fresh_semantic = per_program['features'] - self.semantic_features
                feature_summary = dict(new_pcs=len(item['fresh']),
                    ext4_new=len(item['ext4_fresh']),
                    new_ext4_pcs=len(item['ext4_fresh']),
                    target_new=len(item.get('target_fresh', set())),
                    new_target_pcs=len(item.get('target_fresh', set())),
                    target_unique=len(item.get('target_pcs', set())),
                    new_edges=len(item['fresh_edges']),
                    new_semantic_features=len(fresh_semantic),
                    semantic_features=sorted(fresh_semantic),
                    semantic_evidence=[dict(feature=feature, call_index=call_index)
                                       for feature, call_index in sorted(per_program['evidence'])
                                       if feature in fresh_semantic],
                    attributed_calls=len(per_program['calls']),
                    attribution_complete=timeline_complete,
                    exact_call_events_eligible=semantic_eligible)
                per_program['new_semantic_features'] = fresh_semantic
                row.update(feature_summary)
                source = pathlib.Path(folder) / 'syz-programs' / item['program'] / 'program.syz'
                feedback_mode = self.feedback_mode
                pc_novelty = (item.get('target_fresh', set())
                              if self.target_scope == 'ext4_jbd2'
                              else item['fresh'])
                new_pc = bool(pc_novelty) and feedback_mode != 'none'
                new_edge = bool(item['fresh_edges']) and feedback_mode in ('pc_edge', 'pc_edge_semantic')
                new_semantic = bool(fresh_semantic) and feedback_mode in ('pc_semantic', 'pc_edge_semantic')
                feature_ids = []
                if feedback_mode != 'none':
                    scored_pcs = (item.get('target_pcs', set())
                                  if self.target_scope == 'ext4_jbd2'
                                  else item['pcs'])
                    scored_ext4 = (item.get('target_ext4_pcs', set())
                                   if self.target_scope == 'ext4_jbd2'
                                   else item['ext4_pcs'])
                    feature_ids.extend(f'pc:{pc:x}' for pc in scored_pcs)
                    # Ext4 IDs are a target view of PC coverage. Keeping them
                    # alongside general PC IDs lets the scheduler prioritize
                    # target novelty even in PC-only campaigns.
                    feature_ids.extend(f'ext4:{pc:x}' for pc in scored_ext4)
                    feature_ids.extend(f'target:{pc:x}'
                                       for pc in item.get('target_pcs', set()))
                if feedback_mode in ('pc_edge', 'pc_edge_semantic'):
                    feature_ids.extend(f'edge:{left:x}:{right:x}' for left, right in item['edges'])
                if feedback_mode in ('pc_semantic', 'pc_edge_semantic'):
                    feature_ids.extend('sem:' + feature for feature in per_program['features'])
                for feature in per_program['features']:
                    key = 'sem:' + feature
                    self.feature_frequency[key] = self.feature_frequency.get(key, 0) + 1
                feature_summary['feature_ids'] = feature_ids
                signals = []
                if new_pc:
                    signals.append('pc')
                if new_edge:
                    signals.append('edge')
                if new_semantic:
                    signals.append('semantic')
                feature_summary['admission_signals'] = signals
                feature_summary['admission_eligible'] = bool(signals)
                feature_summary['admitted'] = False
                row.update(feature_summary)
                if source.is_file() and signals:
                    program_hash = _sha(source)
                    kind = self._admission_kind(signals)
                    if kind in ('edge_only', 'semantic_only') and not \
                            self._side_only_parent_allowed(program_hash, kind):
                        # Keep the row and all coverage accounting, but do not
                        # turn a side-signal-only input into an active parent.
                        if kind == 'edge_only':
                            self.edge_only_admissions_suppressed += 1
                        else:
                            self.semantic_only_admissions_suppressed += 1
                        reason = kind + '_quota'
                        feature_summary['admission_suppressed'] = reason
                        row['admission_suppressed'] = reason
                    else:
                        target = self._admit_parent(source, feature_summary)
                        # Keep a corpus-only PC view separate from the
                        # all-complete-sample view above.  The admission
                        # decision is made per generated program, so this
                        # update must happen only after the program is
                        # accepted into the active corpus.
                        self.corpus_pc_coverage.update(item['pcs'])
                        self.corpus_ext4_pc_coverage.update(item['ext4_pcs'])
                        self.corpus_jbd2_pc_coverage.update(item.get('target_jbd2_pcs', set()))
                        self.corpus_target_pc_coverage.update(item.get('target_pcs', set()))
                        row.update(admitted=True, corpus=str(target.relative_to(self.run)))
                        admitted.append(row['corpus'])
                        feature_summary.update(admitted=True, corpus=row['corpus'])
                        for signal in signals:
                            admission_signals[signal] += 1
                        if signals == ['semantic']:
                            semantic_only_admitted.append(row['corpus'])
                meta_path = source.parent / 'program.json'
                meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
                parent_hash = meta.get('parent_sha256')
                if meta.get('method') == 'Mutate' and parent_hash:
                    self._record_child_yield(parent_hash, feature_summary,
                                             meta.get('mutation_input'))
                self.semantic_features.update(per_program['features'])
                program_feedback[item['program']] = feature_summary
        else:
            for item in pending:
                item['row']['native_verified'] = False
                item['row']['reason'] = 'native_execution_unverified'
            self.gaps.append(dict(segment_id=pathlib.Path(folder).name,
                                  errors=['native_execution_unverified']))
        coverage_path = pathlib.Path(folder) / 'coverage.json'
        if coverage_path.is_file():
            segment = json.loads(coverage_path.read_text())
            # collect() writes the initial rows before augment() can decide
            # admission. Merge the final per-program result back into that
            # persisted snapshot so coverage.json, manifest.json and the
            # joint feedback result describe the same admission decision.
            rows = {row.get('program'): row for row in segment.get('programs', [])
                    if row.get('program')}
            for item in pending:
                row = rows.get(item['program'])
                if row is not None:
                    row.update(item['row'])
            segment['native_execution_verified'] = bool(native_execution_verified)
            segment['new_coverage'] = len(segment_new)
            segment['total_coverage'] = len(self.coverage)
            segment['new_target_pc_coverage'] = sum(
                len(item.get('target_fresh', set())) for item in pending) \
                if native_execution_verified else 0
            segment['target_pc_coverage'] = len(self.target_pc_coverage)
            segment['target_scope'] = self.target_scope
            segment['new_edges'] = sum(len(item['fresh_edges']) for item in pending) if native_execution_verified else 0
            segment['total_edges'] = len(self.edges)
            segment['corpus_size'] = len(self.parents)
            segment['passed'] = segment.get('passed', False) and bool(native_execution_verified)
            if not native_execution_verified:
                segment.setdefault('errors', []).append('native_execution_unverified')
                segment['native_execution_verified'] = False
            segment['programs'] = list(rows.values())
            coverage_path.write_text(json.dumps(segment, indent=2) + '\n')
        mismatches, unverified = set(), set()
        if native_execution_verified:
            for run in report.get('runs', []):
                for episode in run.get('episodes', []):
                    signature = _signature(episode)
                    if episode.get('classification') in ('operation_exit_mismatch', 'persistent_mismatch'):
                        mismatches.add(signature)
                    elif episode.get('classification') in ('unsupported_or_unverified', 'concurrent_or_ambiguous'):
                        unverified.add(signature)
                for reason, count in run.get('unverified_reasons', {}).items():
                    if count:
                        unverified.add(f'{run.get("name", "")}|{reason}')
        new_mismatch = mismatches - self.mismatch_signatures
        new_unverified = unverified - self.unverified_signatures
        self.mismatch_signatures.update(mismatches)
        self.unverified_signatures.update(unverified)
        program_feedback = {name: value for name, value in program_feedback.items()
                            if isinstance(value.get('new_pcs'), int)}
        result = dict(programs=program_feedback,
                      new_semantic_features=sum(v.get('new_semantic_features', 0)
                                                for v in program_feedback.values()),
                      new_mismatch_signatures=len(new_mismatch),
                      new_unverified_signatures=len(new_unverified),
                      mismatch_is_diagnostic_only=True,
                      unverified_is_diagnostic_only=True,
                      mismatch_signatures=sorted(new_mismatch), unverified_signatures=sorted(new_unverified))
        result['admitted_repro_programs'] = admitted
        result['admission_counts'] = dict(admission_signals)
        result['admission_suppressed_counts'] = {
            'edge_only_quota': self.edge_only_admissions_suppressed,
            'semantic_only_quota': self.semantic_only_admissions_suppressed,
        }
        result['semantic_only_admitted_programs'] = semantic_only_admitted
        result['native_execution_verified'] = bool(native_execution_verified)
        result['semantic_feedback_complete'] = timeline_complete
        self.formula_counts = {feature.split('|', 2)[0] + '|' + feature.split('|', 2)[1]: 1
                               for feature in self.semantic_features}
        campaign = self.summary()
        campaign['joint_feedback'] = dict(formula_points=len(self.formula_counts),
                                          mismatch_signatures=len(self.mismatch_signatures),
                                          unverified_signatures=len(self.unverified_signatures))
        (self.feedback / 'coverage.json').write_text(json.dumps(campaign, indent=2) + '\n')
        result['campaign_summary'] = campaign
        (pathlib.Path(folder) / 'feedback.json').write_text(json.dumps(result, indent=2) + '\n')
        self.write_scheduler()
        return result




def _signature(item):
    keys = ('stream', 'classification', 'peak_gap', 'recovery_point',
            'op', 'point', 'formula', 'ino', 'group', 'block', 'slot', 'confidence')
    return hashlib.sha256('|'.join(str(item.get(k, '')) for k in keys).encode()).hexdigest()
