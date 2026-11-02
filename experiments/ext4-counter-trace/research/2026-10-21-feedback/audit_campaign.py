#!/usr/bin/env python3
"""Summarize existing campaign evidence without executing or changing a campaign."""

import argparse
import collections
import gzip
import hashlib
import json
from pathlib import Path


def read_json(path):
    return json.loads(path.read_text())


def audit(run):
    run = run.resolve()
    methods = collections.Counter()
    modes = collections.Counter()
    parents = collections.Counter()
    coverage_rows = []
    admitted_names = set()
    joint_names = set()
    valid_segments = 0
    last_new_pc = None
    last_new_ext4 = None
    last_new_point = None
    previous_ext4 = 0
    first_weight = None
    first_segment = None
    missing = collections.Counter()
    programs = 0
    for segment in sorted((run / 'segments').glob('segment-*')):
        ordinal = int(segment.name.removeprefix('segment-'))
        for path in sorted((segment / 'syz-programs').glob('*/program.json')):
            program = read_json(path)
            programs += 1
            methods[program.get('method', 'unknown')] += 1
            modes[program.get('input_mode', 'unknown')] += 1
            if program.get('mutation_input'):
                parents[Path(program['mutation_input']).name] += 1
        coverage_path = segment / 'coverage.json'
        feedback_path = segment / 'feedback.json'
        if not coverage_path.is_file() or not feedback_path.is_file():
            missing['segment_coverage_or_feedback'] += 1
            continue
        coverage = read_json(coverage_path)
        feedback = read_json(feedback_path)
        summary = feedback.get('campaign_summary', {})
        valid_segments += int(bool(feedback.get('native_execution_verified')))
        ext4 = summary.get('total_ext4_coverage', 0)
        row = dict(segment=ordinal, total_pcs=coverage.get('total_coverage'),
                   new_pcs=coverage.get('new_coverage'), total_ext4_jbd2_pcs=ext4,
                   new_formula_points=feedback.get('new_formula_points', 0),
                   formula_points=summary.get('joint_feedback', {}).get('formula_points'),
                   corpus_size=summary.get('corpus_size'))
        coverage_rows.append(row)
        if row['new_pcs']:
            last_new_pc = ordinal
        if ext4 > previous_ext4:
            last_new_ext4 = ordinal
        previous_ext4 = ext4
        if row['new_formula_points']:
            last_new_point = ordinal
        for name in feedback.get('admitted_repro_programs', []):
            admitted_names.add(Path(name).name)
            if '-joint-' in name:
                joint_names.add(Path(name).name)
        if first_segment is None:
            first_segment = segment
            rows = coverage.get('programs', [])
            if rows:
                # This reconstructs the current admission score, not a measured reward.
                first_weight = rows[0].get('new', 0) + 4 * rows[0].get('ext4_new', 0)

    hashes = collections.Counter()
    for path in sorted((run / 'corpus').glob('*.syz')):
        hashes[hashlib.sha256(path.read_bytes()).hexdigest()] += 1
    first_report = {}
    if first_segment is not None:
        report_path = first_segment / 'analysis/lifecycle/report.json'
        if report_path.is_file():
            report = read_json(report_path)
        elif report_path.with_suffix('.json.gz').is_file():
            report = json.loads(gzip.decompress(report_path.with_suffix('.json.gz').read_bytes()))
        else:
            report = {}
        points = report.get('point_statistics', {})
        sources = report.get('event_sources', {})
        first_report = dict(segment=first_segment.name, formula_point_keys=len(points),
                            stable_snapshot_keys=[key for key in points if 'SNAPSHOT.MEMORY.STABLE' in key],
                            event_sources=sources,
                            note='Point keys and source counts are separate aggregates; no per-program causal attribution is inferred.')

    milestones = sorted(set([0, 1, 10, 100, 500, 1000, 2000, 3000, 4000,
                             coverage_rows[-1]['segment'] if coverage_rows else -1]))
    ranked = parents.most_common()
    mutations = sum(parents.values())
    summary_path = run / 'feedback/coverage.json'
    evidence_source = run / 'evidence/scripts/kcov_feedback.py'
    current_source = Path(__file__).resolve().parents[2] / 'scripts/kcov_feedback.py'
    implementation_hashes = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in [('archived', evidence_source), ('current', current_source)]
        if path.is_file()
    }
    return dict(schema=1, campaign=str(run), programs=programs,
                program_counts_scope='stored program.json records, potentially including unexecuted exports',
                feedback_implementation_sha256=implementation_hashes,
                input_methods=dict(methods), input_modes=dict(modes),
                observed_native_verified_segments=valid_segments,
                missing=dict(missing), mutation_input_records=mutations,
                selected_distinct_parent_paths=len(parents),
                top_parent_fraction=ranked[0][1] / mutations if mutations else None,
                parent_selection_counts=[dict(parent=name, selections=count) for name, count in ranked],
                first_admission_weight_reconstructed_from_current_code=first_weight,
                corpus_files=sum(hashes.values()), unique_corpus_program_hashes=len(hashes),
                duplicate_corpus_files=sum(hashes.values()) - len(hashes),
                cumulative_admitted_names=len(admitted_names), joint_admitted_names=len(joint_names),
                last_new_pc_segment=last_new_pc, last_new_ext4_jbd2_pc_segment=last_new_ext4,
                last_new_formula_point_segment=last_new_point,
                coverage_milestones=[row for row in coverage_rows if row['segment'] in milestones],
                final_feedback=read_json(summary_path) if summary_path.is_file() else None,
                first_segment_evidence=first_report,
                limits=['Historical metadata audit, not a new fuzzing experiment.',
                        'PC counts have no total-instrumentable-PC denominator.',
                        'ext4 PC labeling follows the existing ext4_/__ext4_/jbd2_ symbol-prefix filter.',
                        'No claim that a different scheduler would improve coverage without a controlled experiment.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = audit(args.campaign)
    encoded = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    if args.output:
        args.output.write_text(encoded)
        print(args.output)
    else:
        print(encoded, end='')


if __name__ == '__main__':
    main()
