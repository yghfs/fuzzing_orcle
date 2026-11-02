#!/usr/bin/env python3
"""Prove semantic-only admission using one completed campaign segment."""
from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import shutil
import tempfile

from kcov_feedback import (FeedbackCorpus, read_sample_details,
                            read_sample_diagnostics, read_sample_features)


def read_json_maybe_gzip(path: pathlib.Path):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt') as stream:
        return json.load(stream)


def read_text_maybe_gzip(path: pathlib.Path) -> str:
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt') as stream:
        return stream.read()


def audit(campaign: pathlib.Path, segment_name: str) -> dict:
    campaign = pathlib.Path(campaign).resolve()
    segment = campaign / 'segments' / segment_name
    manifest = json.loads((segment / 'manifest.json').read_text())
    if manifest.get('status') != 'complete' or manifest.get('syzkaller_execution_issues'):
        raise ValueError('segment_does_not_have_verified_native_execution')
    sample = next(iter(sorted((segment / 'coverage').glob('b1-w0.bin*'))), None)
    program = segment / 'syz-programs' / 'b1-w0' / 'program.syz'
    report_path = segment / 'analysis' / 'lifecycle' / 'report.json.gz'
    timeline_path = segment / 'analysis' / 'lifecycle' / 'timeline.jsonl.gz'
    for path in (sample, program, report_path, timeline_path):
        if path is None or not path.is_file():
            raise ValueError('missing_semantic_admission_evidence:' + str(path))
    report = read_json_maybe_gzip(report_path)
    timeline = read_text_maybe_gzip(timeline_path)
    with tempfile.TemporaryDirectory(prefix='ct-semantic-admission-') as temporary:
        root = pathlib.Path(temporary)
        folder = root / segment_name
        (folder / 'coverage').mkdir(parents=True)
        program_dir = folder / 'syz-programs' / 'b1-w0'
        program_dir.mkdir(parents=True)
        raw_sample = folder / 'coverage' / 'b1-w0.bin'
        if sample.suffix == '.gz':
            with gzip.open(sample, 'rb') as source, raw_sample.open('wb') as target:
                shutil.copyfileobj(source, target)
        else:
            shutil.copy2(sample, raw_sample)
        shutil.copy2(program, program_dir / 'program.syz')
        replay_timeline = folder / 'timeline.jsonl'
        replay_timeline.write_text(timeline)
        diagnostics = read_sample_diagnostics(raw_sample)
        pcs, saturated, ranges = read_sample_details(raw_sample)
        if saturated:
            raise ValueError('saturated_sample_cannot_prove_admission')
        if diagnostics.get('feature_set'):
            all_edges = set(read_sample_features(raw_sample)['edges'])
        else:
            all_edges = set()
            for start, length in ranges:
                sequence = pcs[start:start + length]
                all_edges.update(zip(sequence, sequence[1:]))
        known_exits = set()
        for row in timeline.splitlines():
            try:
                item = json.loads(row)
            except json.JSONDecodeError:
                continue
            if (item.get('event_source') != 'generated_syscall' or
                    item.get('attribution') != 'same_task_native_syscall' or
                    not item.get('program') or item.get('call_index') is None):
                continue
            call = (item['program'], int(item['call_index']))
            for run in report.get('runs', []):
                for boundary in run.get('boundaries', []):
                    if ((boundary.get('program'), boundary.get('call_index')) == call and
                            boundary.get('classification') not in (None, 'unknown')):
                        known_exits.add(boundary['classification'])
        if not known_exits:
            raise ValueError('no_attributed_operation_exit_in_lifecycle_report')

        results = {}
        for mode in ('pc', 'pc_semantic'):
            replay_root = root / mode
            replay_root.mkdir()
            replay = FeedbackCorpus(replay_root, feedback_mode=mode)
            # Simulate a corpus where every code PC and per-syscall edge in this
            # exact sample is already known. Only a semantic novelty can admit.
            replay.coverage.update(pcs)
            replay.edges.update(all_edges)
            replay.collect(folder, dict(segment_id=manifest.get('segment_id'),
                                        expected_programs=['b1-w0']))
            feedback = replay.augment(folder, report,
                native_execution_verified=True, timeline_path=replay_timeline,
                mode='single')
            row = feedback['programs'].get('b1-w0', {})
            results[mode] = dict(new_pcs=row.get('new_pcs'),
                new_edges=row.get('new_edges'),
                new_semantic_features=row.get('new_semantic_features'),
                admitted=bool(feedback.get('admitted_repro_programs')))
        if results['pc']['admitted'] or not results['pc_semantic']['admitted']:
            raise AssertionError('semantic_only_admission_contract_failed')
        if any(results[mode]['new_pcs'] or results[mode]['new_edges']
               for mode in results):
            raise AssertionError('replay_did_not_hold_code_feedback_constant')
        return dict(campaign=str(campaign), segment=segment_name,
                    program_sha256=manifest['evidence_sha256'].get(
                        'syz-programs/b1-w0/program.syz'),
                    sample_sha256=manifest['coverage']['programs'][0].get('sha256'),
                    known_operation_exit_classes=sorted(known_exits),
                    known_pcs=len(set(pcs)), known_edges=len(all_edges),
                    results=results, passed=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign', type=pathlib.Path)
    parser.add_argument('--segment', default='segment-000000')
    parser.add_argument('--output', type=pathlib.Path)
    args = parser.parse_args()
    result = audit(args.campaign, args.segment)
    rendered = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end='')


if __name__ == '__main__':
    main()
