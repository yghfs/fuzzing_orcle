#!/usr/bin/env python3
"""Run fixed-seed replay and feedback ablation campaigns."""
import argparse
import gzip
import json
import statistics
import pathlib
import re
import subprocess
import sys
import tempfile

PROJECT = pathlib.Path(__file__).resolve().parents[1]
ROOT = PROJECT.parents[1]
RUNNER = PROJECT / 'scripts/run-campaign.sh'


def launch(study, label, seed, args, resource_every, feedback_mode):
    command = [
        'bash', str(RUNNER), '--rootfs-backing', args.rootfs,
        '--mode', 'single', '--segments', str(args.segments),
        '--duration-seconds', str(args.duration_seconds),
        '--target-syscalls', str(args.segments * args.calls),
        '--segment-seconds', '1', '--max-batches', '1',
        '--batch-timeout', str(args.batch_timeout), '--boot-timeout', '180',
        '--analysis-timeout', '180', '--workers', '1', '--memory-mb', '0',
        '--guest-memory-mb', '1024', '--cpus', '1', '--buffer-kb', '8192',
        '--max-trace-mb', '32', '--max-run-mb', '8192', '--min-free-mb', '512',
        '--syzkaller-calls', str(args.calls), '--resource-valid-every', str(resource_every),
        '--fresh-generate-every', '0', '--scheduler-policy', args.scheduler_policy,
        '--feedback-mode', feedback_mode, '--mutation-budget-policy', args.mutation_budget,
        '--target-scope', args.target_scope,
        '--max-mutation-calls', str(args.max_calls), '--scenario', 'baseline',
        '--seed', str(seed),
    ]
    output = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    log = study / f'{label}.log'
    log.write_text(output.stdout)
    match = re.search(r'^run_dir=(.+)$', output.stdout, re.MULTILINE)
    if output.returncode or not match:
        raise RuntimeError(f'{label} failed with exit {output.returncode}; see {log}')
    run = pathlib.Path(match.group(1))
    report = json.loads((run / 'campaign-report.json').read_text())
    campaign = json.loads((run / 'campaign.json').read_text())
    feedback = report.get('kcov_feedback', {})
    return dict(label=label, run_dir=str(run), run_id=campaign['run_id'],
                seed=seed, passed=report['passed'], coverage_complete=report['coverage_complete'],
                total_coverage=feedback.get('total_coverage', 0),
                total_edges=feedback.get('total_edges', 0),
                total_ext4_coverage=feedback.get('total_ext4_coverage', 0),
                ext4_pc_coverage=feedback.get('ext4_pc_coverage', 0),
                jbd2_pc_coverage=feedback.get('jbd2_pc_coverage', 0),
                target_pc_coverage=feedback.get('target_pc_coverage', 0),
                target_fraction=feedback.get('target_fraction', 0.0),
                target_scope=feedback.get('target_scope', args.target_scope),
                semantic_features=feedback.get('total_semantic_features', 0),
                corpus_size=feedback.get('corpus_size', 0),
                parent_selections=feedback.get('parent_selections', 0),
                native_verified=report.get('native_verified', 0),
                native_syscalls=report.get('native_syscalls', 0),
                independent_transition_calls=report.get('exploration', {}).get(
                    'independent_transition_calls', 0),
                rootfs_sha256=campaign.get('dependencies', {}).get('rootfs_sha256'),
                kernel_sha256=campaign.get('build_sha256', {}).get('kernel.bzImage'),
                start_images=campaign.get('start_images', {}))


def replay_report(study, rows):
    programs, pcs, semantic = [], [], []
    for row in rows:
        run = pathlib.Path(row['run_dir'])
        program_dir = run / 'segments/segment-000000/syz-programs/b1-w0'
        meta = json.loads((program_dir / 'program.json').read_text())
        sample = program_dir.parent.parent / 'coverage/b1-w0.bin.gz'
        sys.path.insert(0, str(PROJECT / 'scripts'))
        from kcov_feedback import read_sample_details
        with gzip.open(sample, 'rb') as source, tempfile.NamedTemporaryFile() as decoded:
            decoded.write(source.read())
            decoded.flush()
            trace_pcs, saturated, _ = read_sample_details(pathlib.Path(decoded.name))
        if saturated:
            raise RuntimeError(f'{row["label"]} KCOV sample saturated')
        feedback = json.loads((run / 'segments/segment-000000/feedback.json').read_text())
        programs.append(meta['sha256']['program.syz'])
        pcs.append(set(trace_pcs))
        semantic.append({feature for item in feedback.get('programs', {}).values()
                         for feature in item.get('semantic_features', [])})
    if len(set(programs)) != 1:
        raise RuntimeError('replay_program_content_drift')
    pc_intersection = set.intersection(*pcs) if pcs else set()
    pc_union = set.union(*pcs) if pcs else set()
    semantic_intersection = set.intersection(*semantic) if semantic else set()
    semantic_union = set.union(*semantic) if semantic else set()
    result = dict(schema=1, repetitions=len(rows), identical_program=True,
                  program_sha256=programs[0], same_start_images=all(
                      row['start_images'] == rows[0]['start_images'] for row in rows),
                  same_environment=all(
                      (row['rootfs_sha256'], row['kernel_sha256']) ==
                      (rows[0]['rootfs_sha256'], rows[0]['kernel_sha256']) for row in rows),
                  pc_intersection=len(pc_intersection), pc_union=len(pc_union),
                  pc_stability=(len(pc_intersection) / len(pc_union) if pc_union else 1.0),
                  semantic_intersection=len(semantic_intersection), semantic_union=len(semantic_union),
                  semantic_stability=(len(semantic_intersection) / len(semantic_union)
                                      if semantic_union else 1.0), runs=rows)
    if not all(row['passed'] and row['coverage_complete'] for row in rows):
        raise RuntimeError('replay_campaign_incomplete')
    if not result['same_start_images']:
        raise RuntimeError('replay_start_image_drift')
    if not result['same_environment']:
        raise RuntimeError('replay_environment_drift')
    if result['pc_stability'] < 0.95 or result['semantic_stability'] < 0.95:
        raise RuntimeError('replay_feature_instability')
    (study / 'replay-report.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('replay', 'ablation'))
    parser.add_argument('--rootfs', default=str(ROOT / 'work/debian-clean-smalljournal.img'))
    parser.add_argument('--seed', type=int, default=20261022)
    parser.add_argument('--repetitions', type=int, default=2)
    parser.add_argument('--segments', type=int, default=6)
    parser.add_argument('--calls', type=int, default=16)
    parser.add_argument('--duration-seconds', type=int, default=1800)
    parser.add_argument('--batch-timeout', type=int, default=30)
    parser.add_argument('--scheduler-policy', choices=('uniform_round_robin', 'rarity_fair'),
                        default='uniform_round_robin')
    parser.add_argument('--mutation-budget', choices=('fixed', 'dynamic'), default='fixed')
    parser.add_argument('--max-calls', type=int, default=256)
    parser.add_argument('--target-scope', choices=('all', 'ext4_jbd2'),
                        default='ext4_jbd2',
                        help='PC feedback scope used by replay and ablation runs')
    parser.add_argument('--output', help='study directory; created if absent')
    args = parser.parse_args()
    if not pathlib.Path(args.rootfs).is_file():
        parser.error('rootfs image does not exist: ' + args.rootfs)
    if args.repetitions < 2 or args.segments < 1 or args.calls < 16:
        parser.error('repetitions >= 2, segments >= 1 and calls >= 16 are required')
    study = pathlib.Path(args.output) if args.output else pathlib.Path(
        tempfile.mkdtemp(prefix='feedback-study-', dir=PROJECT / 'results'))
    study.mkdir(parents=True, exist_ok=True)
    rows = []
    if args.mode == 'replay':
        args.segments = 1
        args.scheduler_policy = 'uniform_round_robin'
        args.mutation_budget = 'fixed'
        for repeat in range(args.repetitions):
            rows.append(launch(study, f'replay-{repeat:02d}', args.seed, args, 0,
                               'pc_edge_semantic'))
        replay_report(study, rows)
    else:
        if args.scheduler_policy != 'uniform_round_robin' or args.mutation_budget != 'fixed':
            parser.error('ablation mode fixes uniform selection and fixed mutation budgets')
        variants = (
            ('ordinary_pc', 0, 'pc'),
            ('resource_valid_pc', 2, 'pc'),
            ('resource_valid_edge', 2, 'pc_edge'),
            ('resource_valid_semantic', 2, 'pc_semantic'),
            ('resource_valid_joint', 2, 'pc_edge_semantic'),
        )
        for repeat in range(args.repetitions):
            seed = args.seed + repeat
            for label, resource_every, feedback_mode in variants:
                rows.append(launch(study, f'{label}-{repeat:02d}', seed, args,
                                   resource_every, feedback_mode))
        if not all(row['passed'] and row['coverage_complete'] for row in rows):
            raise RuntimeError('ablation_campaign_incomplete')
        for seed in sorted({row['seed'] for row in rows}):
            pair = [row for row in rows if row['seed'] == seed]
            identities = {(json.dumps(row['start_images'], sort_keys=True),
                           row['rootfs_sha256'], row['kernel_sha256']) for row in pair}
            if len(identities) != 1:
                raise RuntimeError(f'ablation_start_drift:{seed}')
        groups = {}
        metrics = ('total_coverage', 'total_edges', 'target_pc_coverage',
                   'semantic_features', 'corpus_size', 'independent_transition_calls')
        for label, _, _ in variants:
            group = [row for row in rows if row['label'].startswith(label + '-')]
            groups[label] = {metric: statistics.mean(row[metric] for row in group)
                             for metric in metrics}
        baseline = groups['resource_valid_pc']
        deltas = {label: {metric: round(values[metric] - baseline[metric], 3)
                          for metric in metrics} for label, values in groups.items()}
        (study / 'ablation-report.json').write_text(json.dumps(
            dict(schema=1, repetitions=args.repetitions, groups=groups,
                 delta_vs_resource_valid_pc=deltas, variants=rows), indent=2) + '\n')
    print(study)
    return 0


if __name__ == '__main__':
    sys.exit(main())
