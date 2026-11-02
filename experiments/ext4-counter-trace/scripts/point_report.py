#!/usr/bin/env python3
"""Per-probe observed hits, direct comparisons and independently named derived checks."""
from __future__ import annotations

import base64
import collections
import csv
import gzip
import json
import pathlib
import sqlite3
import tempfile
import contextlib

RESULTS = ('consistent', 'inconsistent', 'ambiguous', 'unverified', 'raw_only')
LABELS = dict(consistent='一致', inconsistent='不一致', ambiguous='并发/归属不确定',
              unverified='无法验证', raw_only='仅记录，无现场比较')
MEASUREMENTS = dict(local='现场 actual / expected',
                    descriptor='重建 descriptor / 独立来源',
                    global_='重建 global 发布累计 / 独立来源',
                    group='重建本 group descriptor / 独立来源',
                    namespace='重建 nlink / 目录引用')
COUNTER_FORMULAS = ('R-BDELTA', 'R-IDELTA', 'R-UDELTA')
UNITS = {'R-BDELTA':'cluster', 'R-BG':'cluster', 'R-BSUM':'cluster',
         'R-IDELTA':'inode', 'R-IG':'inode', 'R-ISUM':'inode',
         'R-UDELTA':'directory inode', 'R-UG':'directory inode',
         'R-USUM':'directory inode', 'R-USUM.NS':'directory inode', 'R-LINK':'reference'}
# Source coordinates and comm names can differ in carried analysis input.
EVENT_FIELDS = ('dev', 'epoch', 'seq', 'op', 'parent', 'point', 'formula', 'ino', 'group',
                'actual', 'expected', 'update_delta', 'status', 'comparison_valid',
                'schema', 'step', 'ref_delta', 'nlink_before', 'nlink_after', 'generation',
                'is_dir', 'result', 'dir_ino', 'reason', 'block', 'slot')


def source_signature(event):
    return {key: event[key] for key in EVENT_FIELDS if key in event}


def read_json(path):
    path = pathlib.Path(path)
    with (gzip.open(path, 'rt') if path.suffix == '.gz' else path.open()) as stream:
        return json.load(stream)


def json_rows(path):
    path = pathlib.Path(path)
    with (gzip.open(path, 'rt') if path.suffix == '.gz' else path.open()) as stream:
        for line in stream:
            yield json.loads(line)


def comparison(name, actual=None, expected=None, confidence=None, reason=None):
    delta = actual - expected if actual is not None and expected is not None else None
    relation = None if delta is None else ('equal' if delta == 0 else 'different')
    if delta is None and not reason:
        reason = 'missing_comparable_values'
    if reason:
        result = 'unverified'
    elif confidence == 'concurrent_or_ambiguous':
        result = 'ambiguous'
    elif confidence != 'precise_single_writer_recording_order':
        result = 'unverified'
        reason = confidence or 'no_verified_derived_state'
    else:
        result = 'consistent' if delta == 0 else 'inconsistent'
    return dict(measurement=name, result=result, actual=actual, expected=expected, delta=delta,
                observed_relation=relation, confidence=confidence, reason=reason)


def probe_observation(row, context=None):
    context = context or {}
    e = row['event']
    status = e.get('status', 0)
    if status in (1, 2) and e.get('comparison_valid', 1):
        actual, expected = e['actual'], e['expected']
        reason = None
        if (status == 1) != (actual == expected):
            reason = 'status_and_comparison_values_disagree'
        local = comparison('local', actual, expected, 'precise_single_writer_recording_order', reason)
    elif status == 3 or status in (1, 2):
        local = comparison('local', reason=e.get('reason') or 'comparison_not_valid')
    else:
        local = dict(measurement='local', result='raw_only', actual=None, expected=None,
                     delta=None, observed_relation=None, confidence=None, reason='no_local_comparison')
    checks = [local]
    confidence = row.get('confidence')
    if context.get('trajectory_valid') is False:
        confidence = 'unsupported_or_unverified'
    stable = e['point'] == 'SNAPSHOT.MEMORY.STABLE'
    if not stable and e['formula'] in COUNTER_FORMULAS:
        state = row.get('derived_counters', {}).get(e['formula'])
        for lane, name in [('descriptor', 'descriptor'), ('global', 'global_')]:
            checks.append(comparison(name, state.get(lane) if state else None,
                                     state.get('independent') if state else None, confidence,
                                     None if state else 'no_independent_stable_anchor'))
        if e.get('group', 4294967295) != 4294967295:
            groups = row.get('derived_groups', {}).get(e['formula'], {})
            state = groups.get(e['group'], groups.get(str(e['group'])))
            checks.append(comparison('group', state.get('descriptor') if state else None,
                                     state.get('independent') if state else None, confidence,
                                     None if state else 'group_without_independent_anchor'))
    elif not stable and e['formula'] == 'R-LINK' and e.get('ino'):
        state = row.get('derived_inode')
        reason = (state.get('unsupported') if state else 'inode_without_baseline_or_birth')
        if state and (state.get('nlink') is None or state.get('refs') is None):
            reason = 'inode_without_comparable_state'
        checks.append(comparison('namespace', state.get('nlink') if state else None,
                                 state.get('refs') if state else None, confidence, reason))
    return dict(run_id=context.get('run_id'), boot_id=context.get('boot_id'),
                segment_id=context.get('segment_id'), kind=context.get('kind'),
                mode=context.get('mode'), interval=row.get('run'),
                dev=e['dev'], epoch=e['epoch'], seq=e['seq'], point=e['point'], formula=e['formula'],
                group=e.get('group'), ino=e.get('ino'), block=e.get('block'), slot=e.get('slot'),
                op=e.get('op'), parent=e.get('parent'),
                syscall_id=row.get('syscall_id'), timestamp_ns=e.get('timestamp_ns'),
                event_source=row.get('event_source', e.get('event_source', 'generated_syscall' if row.get('syscall_id') else 'outside_generated_call')),
                program=row.get('program'), call_index=row.get('call_index'), syscall_name=row.get('syscall_name'),
                raw_line=context.get('raw_line', e.get('line')),
                raw_trace=context.get('raw_trace'),
                analysis_segment=context.get('analysis_segment'),
                capture_complete=context.get('capture_complete', True),
                raw=dict(actual=e.get('actual'), expected=e.get('expected'), status=status,
                         comparison_valid=e.get('comparison_valid'), update_delta=e.get('update_delta')),
                checks=checks)


class PointSummary:
    def __init__(self):
        self.points = {}
        self.onsets = {}
        self.hits = 0
        self.sources = {}

    def add(self, hit):
        ident = hit['formula'] + ':' + hit['point']
        p = self.points.setdefault(ident, dict(point=hit['point'], formula=hit['formula'], hits=0,
                                               incomplete_capture_hits=0, checks={}, dimensions={}))
        p['hits'] += 1
        self.hits += 1
        source = hit['event_source']
        self.sources[source] = self.sources.get(source, 0) + 1
        p.setdefault('sources', {})[source] = p.setdefault('sources', {}).get(source, 0) + 1
        hit['hit_index'] = p['hits']
        p['incomplete_capture_hits'] += int(not hit['capture_complete'])
        dim = (hit.get('kind') or 'unspecified') + '/' + (hit.get('mode') or 'unspecified')
        d = p['dimensions'].setdefault(dim, dict(hits=0, checks={}))
        d['hits'] += 1
        for check in hit['checks']:
            for bucket, samples in ((p['checks'], True), (d['checks'], False)):
                m = bucket.setdefault(check['measurement'], dict(
                    hits=0, counts=dict.fromkeys(RESULTS, 0), observed_equal=0, observed_nonzero=0,
                    min_delta=None, max_delta=None, delta_distribution={}, distribution_complete=True,
                    reasons={}, samples=[]))
                m['hits'] += 1
                m['counts'][check['result']] += 1
                m['observed_equal'] += check['observed_relation'] == 'equal'
                m['observed_nonzero'] += check['observed_relation'] == 'different'
                delta = check['delta']
                if delta is not None:
                    m['min_delta'] = delta if m['min_delta'] is None else min(m['min_delta'], delta)
                    m['max_delta'] = delta if m['max_delta'] is None else max(m['max_delta'], delta)
                    key = str(delta)
                    if key not in m['delta_distribution'] and len(m['delta_distribution']) >= 128:
                        key = 'other'; m['distribution_complete'] = False
                    m['delta_distribution'][key] = m['delta_distribution'].get(key, 0) + 1
                if check['reason']:
                    m['reasons'][check['reason']] = m['reasons'].get(check['reason'], 0) + 1
                # Retain the first 100 hits plus sparse examples of each outcome.
                # Exact, unlimited per-hit evidence is exported separately.
                if samples and (len(m['samples']) < 100 or
                                sum(s['result'] == check['result'] for s in m['samples']) < 10):
                    m['samples'].append(dict(
                        {k: hit[k] for k in ('hit_index','segment_id','kind','mode','dev','epoch','seq','group','ino','op','syscall_id','program','call_index','syscall_name','event_source','raw_line','raw_trace')},
                        **check))

    def onset(self, episode, formula):
        ident = formula + ':' + episode['onset_point']
        p = self.onsets.setdefault(ident, dict(point=episode['onset_point'], formula=formula,
                                              counts={}, recovery_points={}, peak_abs_gap=0, samples=[]))
        classification = episode['classification']
        p['counts'][classification] = p['counts'].get(classification, 0) + 1
        point = episode.get('recovery_point')
        if point:
            p['recovery_points'][point] = p['recovery_points'].get(point, 0) + 1
        p['peak_abs_gap'] = max(p['peak_abs_gap'], episode.get('peak_abs_gap',0))
        if len(p['samples']) < 20:
            p['samples'].append({k:episode.get(k) for k in ('stream','onset_seq','peak_seq','peak_gap',
                                                          'recovered_seq','recovery_point','classification')})

    def report(self, **metadata):
        return dict(schema=1, **metadata, observed_hits=self.hits, event_sources=self.sources,
                    source_attribution_complete=bool(self.hits) and not any(self.sources.get(k) for k in
                        ('outside_generated_call', 'background_unattributed')),
                    points=dict(sorted(self.points.items())), onset_points=dict(sorted(self.onsets.items())),
                    semantics=dict(
                        unit='one_observed_counter_event_per_point_and_formula',
                        local='actual_expected_only_when_comparison_valid',
                        derived='state_after_this_event_reconstructed_from_stable_anchor_and_applied_deltas',
                        ambiguous='numeric_relation_retained_without_claiming_consistency',
                        raw_only='recorded_transition_or_boundary_without_local_expected',
                        onsets='first_difference_point_is_not_proof_of_a_bug_in_that_point',
                        global_='cumulative_publications_not_live_percpu_total',
                        evidence='raw_trace_relative_to_source_run; raw_line_is_one_based_segment_local',
                        sampling='summary_samples_are_bounded_full_hits_are_in_compressed_CSV_and_JSONL'))


CSV_FIELDS = ('point','formula','measurement','hit_index','result','actual','expected','delta',
              'observed_relation','confidence','reason','run_id','boot_id','segment_id','kind','mode',
              'dev','epoch','seq','group','ino','op','syscall_id','program','call_index','syscall_name','event_source','raw_trace','raw_line','capture_complete')


class HitWriter:
    def __init__(self, output):
        # Keep the complete per-hit exports while reducing long-run disk use.
        self.json = gzip.open(output / 'point-hits.jsonl.gz', 'wt', compresslevel=6)
        self.csv_stream = gzip.open(output / 'point-hits.csv.gz', 'wt', newline='', compresslevel=6)
        self.csv = csv.DictWriter(self.csv_stream, fieldnames=CSV_FIELDS)
        self.csv.writeheader()

    def write(self, hit):
        self.json.write(json.dumps(hit, separators=(',',':'))+'\n')
        for check in hit['checks']:
            self.csv.writerow({k:(check[k] if k in check else hit.get(k)) for k in CSV_FIELDS})

    def close(self):
        self.json.close(); self.csv_stream.close()


def render_reports(report, output):
    output = pathlib.Path(output)
    (output / 'point-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    md = ['# 插桩点逐次检查报告', '', f'已采集命中：{report["observed_hits"]:,}；'
          f'完整采集：{report.get("coverage_complete", "见整轮报告")}。', '',
          '每行按插桩点、公式和比较来源统计，命中数不是 episode 数。多种比较来源分别统计，不能相加作为命中数。',
          '仅记录表示该事件没有现场 expected；并发重建保留数值关系，归属未证明时不计为确定一致或不一致。',
          '不一致表示该步骤观察到差值，是否正常暂态、出口异常或持续异常需要结合生命周期报告。', '',
          '完整逐次文件保留 seq、actual、expected、差值、syscall 和原始 trace 路径/行号；行号从 1 开始，按原始段文件定位。', '',
          '[交互报告](point-report.html) · [完整逐次 CSV](point-hits.csv.gz) · [完整逐次 JSONL](point-hits.jsonl.gz)', '',
          '## 现场比较', '', '| 插桩点 | 公式 | 命中 | 一致 | 不一致 | 无法验证 | 仅记录 |',
          '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
    md[2:2] = [f'事件来源（互斥、总和为全部命中）：{json.dumps(report.get("event_sources", {}), ensure_ascii=False)}。',
               '采集完整、KCOV 完整、来源归属完整是三个独立结论。', '']
    for p in report['points'].values():
        c = p['checks']['local']['counts']
        md.append(f'| {p["point"]} | {p["formula"]} | {p["hits"]} | {c["consistent"]} | {c["inconsistent"]} | '
                  f'{c["unverified"]} | {c["raw_only"]} |')
    md += ['', '## 重建状态在各点的结果', '',
           '下面比较的是该事件之后的派生状态；global 为发布累计量，不能解释为现场全局采样。', '',
           '| 插桩点 | 公式 | 比较 | 命中 | 一致 | 不一致 | 并发不确定 | 无法验证 |',
           '| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for p in report['points'].values():
        for name, m in p['checks'].items():
            if name == 'local': continue
            c = m['counts']
            md.append(f'| {p["point"]} | {p["formula"]} | {MEASUREMENTS[name]} | {m["hits"]} | '
                      f'{c["consistent"]} | {c["inconsistent"]} | {c["ambiguous"]} | {c["unverified"]} |')
    md += ['', '## 差值首次出现点', '',
           '这是 episode 的首次出现位置；一个命中可能同时出现多条流的差值，因此 episode 数不等于不一致命中数。', '',
           '| 首次出现点 | 公式 | 暂态 | 出口异常 | 持续异常 | 并发不确定 | 未验证 | 最大绝对差值 |',
           '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for p in report['onset_points'].values():
        c = p['counts']
        md.append(f'| {p["point"]} | {p["formula"]} | {c.get("transient_mismatch",0)} | '
                  f'{c.get("operation_exit_mismatch",0)} | {c.get("persistent_mismatch",0)} | '
                  f'{c.get("concurrent_or_ambiguous",0)} | {c.get("unsupported_or_unverified",0)} | {p["peak_abs_gap"]} |')
    (output / 'point-report.md').write_text('\n'.join(md)+'\n')
    payload = base64.b64encode(gzip.compress(json.dumps(report, ensure_ascii=False, separators=(',',':')).encode())).decode()
    template = HTML.replace('__PAYLOAD__', payload)
    (output / 'point-report.html').write_text(template)


def lifecycle_points(timeline, intervals, output, run_id, boot_id):
    """Used by the normal analyzer. Campaign counting independently removes carry anchors."""
    output = pathlib.Path(output)
    summary = PointSummary()
    writer = HitWriter(output)
    by_name = {r['name']:r for r in intervals}
    try:
        for row in timeline:
            r = by_name[row['run']]
            valid = r['trajectory_integrity'] and not any(
                any(values.values()) for s in r['snapshots'] for values in s['reconciliation'].values())
            context = dict(run_id=run_id, boot_id=boot_id, mode=r['mode'],
                           kind=r['name'].split('-')[0], trajectory_valid=valid,
                           raw_trace='trace.txt',
                           capture_complete=r['trajectory_integrity'])
            hit = probe_observation(row, context)
            summary.add(hit); writer.write(hit)
        for r in intervals:
            for e in r['episodes']:
                formula = e['stream'].split(':')[1 if e['stream'].startswith('LOCAL:') else 0]
                summary.onset(e, formula)
    finally:
        writer.close()
    report = summary.report(run_id=run_id, boot_id=boot_id,
                            counting_scope='analysis_intervals_including_explicit_carry_anchors',
                            coverage_complete=all(r['trajectory_integrity'] for r in intervals))
    render_reports(report, output)
    return report


def campaign_points(run, output, reanalyzed=None, coverage_complete=None):
    """Join real raw hits to current/next segment timelines, using bounded per-segment SQLite.

    Raw trace is the denominator. Carried snapshots only supply state; they never
    add hits. Next-segment analysis may contain a previous segment's bridge tail.
    Missing modeled rows remain unverified, rather than silently disappearing.
    """
    from trace_lifecycle import PREFIX, fields
    run, output = pathlib.Path(run), pathlib.Path(output)
    output.mkdir(parents=True, exist_ok=True)
    meta = read_json(run / 'campaign.json')
    manifests = sorted((run / 'segments').glob('segment-*/manifest.json'))
    entries = [(p.parent, read_json(p)) for p in manifests]
    device_kinds = {v:k for k,v in meta.get('devices', {}).items()}
    summary, writer = PointSummary(), HitWriter(output)
    problems = []
    try:
        for index, (folder, manifest) in enumerate(entries):
            raw_path = folder / manifest.get('trace_file','trace.txt')
            if not raw_path.exists():
                problems.append(folder.name+':missing_raw_trace'); continue
            with tempfile.TemporaryDirectory(prefix='.point-join-', dir=output) as tmp, contextlib.closing(
                    sqlite3.connect(str(pathlib.Path(tmp)/'join.sqlite'))) as db:
                db.execute('PRAGMA cache_size=-8192')
                db.execute('CREATE TABLE hits (id INTEGER PRIMARY KEY, dev TEXT, epoch INTEGER, seq INTEGER, event TEXT, modeled TEXT)')
                db.execute('CREATE INDEX identity ON hits(dev,epoch,seq)')
                with (gzip.open(raw_path,'rt') if raw_path.suffix == '.gz' else raw_path.open()) as stream:
                    for line_number, raw in enumerate(stream,1):
                        if 'ext4_counter_event:' not in raw:continue
                        match = PREFIX.search(raw.strip())
                        if not match:
                            problems.append(folder.name+':invalid_counter_prefix');continue
                        e = fields(match['body'])
                        if e.get('dev') not in device_kinds:continue
                        if not {'point','formula','actual','expected','status','epoch','seq'} <= e.keys():
                            problems.append(folder.name+':missing_counter_fields');continue
                        sec, frac = match['time'].split('.')
                        e.update(line=line_number, timestamp_ns=int(sec)*10**9+int(frac.ljust(9,'0')))
                        db.execute('INSERT INTO hits(dev,epoch,seq,event) VALUES(?,?,?,?)',
                                   (e['dev'],e['epoch'],e['seq'],json.dumps(e,separators=(',',':'))))
                db.commit()
                # The next segment may contain a carry bridge, but in the
                # normal case the segment's own lifecycle already models all
                # of its raw events.  Do not decompress and scan the next
                # timeline unless this segment still has unmatched raw hits;
                # this keeps long campaign aggregation close to one timeline
                # pass per segment while preserving the carry fallback.
                source_index = index
                while source_index < min(index + 2, len(entries)):
                    source, sm = entries[source_index]
                    analysis = pathlib.Path(reanalyzed)/source.name if reanalyzed else source/'analysis'
                    timeline = analysis/'lifecycle/timeline.jsonl.gz'
                    if not timeline.exists():timeline = analysis/'lifecycle/timeline.jsonl'
                    rp = analysis/'lifecycle/report.json.gz'
                    if not rp.exists():rp = analysis/'lifecycle/report.json'
                    if not timeline.exists() or not rp.exists():
                        source_index += 1
                        continue
                    report = read_json(rp)
                    intervals = {r['name']:r for r in report['runs']}
                    for row in json_rows(timeline):
                        e = row['event']
                        matches = db.execute('SELECT id,event FROM hits WHERE dev=? AND epoch=? AND seq=? AND modeled IS NULL',
                                             (e['dev'],e['epoch'],e['seq'])).fetchall()
                        if not matches:continue
                        r = intervals[row['run']]
                        valid = r['trajectory_integrity'] and not any(
                            any(values.values()) for s in r['snapshots'] for values in s['reconciliation'].values())
                        for record_id, raw_event in matches:
                            original = json.loads(raw_event)
                            if source_signature(original) != source_signature(e):
                                problems.append(folder.name+':raw_modeled_event_disagrees');continue
                            context = dict(run_id=meta['run_id'],boot_id=meta.get('boot_id'),
                                segment_id=manifest['segment_id'],kind=device_kinds[e['dev']],mode=manifest.get('mode'),
                                trajectory_valid=valid,capture_complete=manifest.get('status')=='complete' and r['trajectory_integrity'],
                                raw_line=original['line'],analysis_segment=sm['segment_id'])
                            context['raw_trace'] = str(raw_path.relative_to(run))
                            hit = probe_observation(row,context)
                            db.execute('UPDATE hits SET modeled=? WHERE id=?',(json.dumps(hit,separators=(',',':')),record_id))
                    # Episode onset ownership is also assigned to real raw events.
                    for r in report['runs']:
                        for ep in r['episodes']:
                            found = db.execute('SELECT 1 FROM hits WHERE dev=? AND epoch=? AND seq=?',
                                               (r['dev'],r['epoch'],ep['onset_seq'])).fetchone()
                            if not found:continue
                            # A next segment may repeat an anchor episode; use a per-segment key.
                            key = json.dumps([r['dev'],r['epoch'],ep['stream'],ep['onset_seq']])
                            db.execute('CREATE TABLE IF NOT EXISTS onsets (key TEXT PRIMARY KEY)')
                            before = db.total_changes
                            db.execute('INSERT OR IGNORE INTO onsets VALUES(?)',(key,))
                            if db.total_changes > before:
                                formula = ep['stream'].split(':')[1 if ep['stream'].startswith('LOCAL:') else 0]
                                summary.onset(ep,formula)
                    db.commit()
                    if source_index == index:
                        pending = db.execute('SELECT 1 FROM hits WHERE modeled IS NULL LIMIT 1').fetchone()
                        if not pending:
                            break
                    source_index += 1
                for raw_event, modeled in db.execute('SELECT event,modeled FROM hits ORDER BY id'):
                    e = json.loads(raw_event)
                    if modeled:
                        hit = json.loads(modeled)
                    else:
                        hit = probe_observation(dict(event=e),dict(
                            run_id=meta['run_id'],boot_id=meta.get('boot_id'),segment_id=manifest['segment_id'],
                            kind=device_kinds[e['dev']],mode=manifest.get('mode'),trajectory_valid=False,
                            capture_complete=manifest.get('status')=='complete',raw_line=e['line']))
                        hit['raw_trace'] = str(raw_path.relative_to(run))
                        for check in hit['checks'][1:]:check['reason']='outside_available_lifecycle_interval'
                    summary.add(hit);writer.write(hit)
                db.close()
    finally:
        writer.close()
    report = summary.report(run_id=meta['run_id'],boot_id=meta.get('boot_id'),
                            counting_scope='experiment_device_events_in_raw_trace_without_carry_duplicates',
                            coverage_complete=(coverage_complete if coverage_complete is not None else meta.get('status')=='complete') and not problems,
                            issues=sorted(set(problems)))
    render_reports(report,output)
    return report


HTML = r"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>插桩点逐次检查</title>
<style>
:root{font-family:system-ui,sans-serif;color:#203040;background:#f4f7fa}body{margin:0;padding:28px;max-width:1600px;margin:auto}
h1{font-size:26px;margin:0 0 12px}p{line-height:1.7}.panel{background:white;padding:20px;border-radius:10px;margin:16px 0;border:1px solid #dbe3eb}
.controls{display:flex;flex-wrap:wrap;gap:12px}label{display:flex;gap:6px;align-items:center}input,select,button{padding:8px;border:1px solid #b4c2d1;border-radius:5px;background:white}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:9px;border-bottom:1px solid #e3e9ef}th{position:sticky;top:0;background:#ecf1f6}
tbody tr:hover{background:#edf5ff;cursor:pointer}.num{text-align:right;font-variant-numeric:tabular-nums}.bad{color:#bb2525}.good{color:#146a43}
.scroll{overflow:auto;max-height:65vh}code{font-size:12px}.subtle{color:#53667b}.bar{display:flex;width:110px;height:8px;background:#e5e9ed;border-radius:4px;overflow:hidden}
.bar span{min-width:0}.mono{font-family:ui-monospace,monospace}.selected{background:#e4efff}
</style>
<h1>插桩点逐次检查</h1>
<p id="status">正在载入报告…</p>
<div class="panel">
<p>每个点按公式单独统计：经过 100 次，就分别列出这 100 次一致、不一致、并发不确定、无法验证或仅记录各多少。
现场比较使用该事件有效的 actual / expected；重建比较使用该事件之后的派生状态。
global 是已记录发布量的累计结果。不一致是否正常暂态，需要结合下面的首次出现与恢复位置。</p>
<div class="controls">
<label>比较来源 <select id="measurement"></select></label>
<label>镜像 <select id="kind"><option value="">全部</option></select></label>
<label>模式 <select id="mode"><option value="">全部</option></select></label>
<label>查找 <input id="query" placeholder="插桩点或公式"></label>
<label><input type="checkbox" id="nonzero">只看不一致或不确定</label>
</div>
</div>
<div class="panel"><p id="total"></p><div class="scroll"><table><thead><tr>
<th>插桩点</th><th>公式</th><th class="num">命中</th><th class="num">一致</th><th class="num">不一致</th>
<th class="num">并发不确定</th><th class="num">无法验证</th><th class="num">仅记录</th><th>分布</th>
</tr></thead><tbody id="points"></tbody></table></div></div>
<div class="panel" id="detail"><p>点击一行查看命中样例的数值和证据位置；完整逐次明细在 CSV / JSONL。</p></div>
<div class="panel"><h2>差值首次出现点</h2><p class="subtle">这里是差值生命周期的数量，不是命中次数。首次出现位置也不等于该点存在 bug。</p>
<div class="scroll"><table><thead><tr><th>首次出现点</th><th>公式</th><th>分类与数量</th><th>恢复点</th><th class="num">最大绝对差值</th></tr></thead><tbody id="onsets"></tbody></table></div></div>
<p>完整逐次数据：<a href="point-hits.csv.gz">CSV</a> / <a href="point-hits.jsonl.gz">JSONL</a>；
<a href="point-report.md">Markdown 汇总</a>。页面内明细为有限样例；完整次数以汇总和导出文件为准。</p>
<script>
const PAYLOAD="__PAYLOAD__";
const names={local:"现场 actual / expected",descriptor:"重建 descriptor / 独立来源",global_:"重建 global 发布累计 / 独立来源",group:"重建本 group descriptor / 独立来源",namespace:"重建 nlink / 目录引用"};
const labels={consistent:"一致",inconsistent:"不一致",ambiguous:"并发不确定",unverified:"无法验证",raw_only:"仅记录"};
const cls={transient_mismatch:"暂态",operation_exit_mismatch:"出口异常",persistent_mismatch:"持续异常",concurrent_or_ambiguous:"并发不确定",unsupported_or_unverified:"未验证"};
const colors={consistent:"#1c865a",inconsistent:"#d33c39",ambiguous:"#d49931",unverified:"#8977b3",raw_only:"#bdc7d1"};
let data;
const el=id=>document.getElementById(id);
function cell(row,text,kind){const td=document.createElement("td");td.textContent=text??"—";if(kind)td.className=kind;row.append(td);return td}
function counts(point){const kind=el("kind").value,mode=el("mode").value,name=el("measurement").value;
 if(!kind&&!mode)return point.checks[name];
 const sum={hits:0,counts:Object.fromEntries(Object.keys(labels).map(k=>[k,0]))};
 for(const [key,d] of Object.entries(point.dimensions)){const [k,m]=key.split("/");if(kind&&kind!==k||mode&&mode!==m)continue;
 const c=d.checks[name];if(!c)continue;sum.hits+=c.hits;for(const key of Object.keys(labels))sum.counts[key]+=c.counts[key]}
 return sum.hits?sum:null;
}
function detail(point){
 const name=el("measurement").value,m=point.checks[name],c=counts(point);el("detail").replaceChildren();
 const h=document.createElement("h2");h.textContent=point.point+" · "+point.formula;el("detail").append(h);
 const p=document.createElement("p");p.textContent=names[name]+"："+c.hits+" 次；"+Object.entries(c.counts).map(([k,v])=>labels[k]+" "+v).join("，")+"。";el("detail").append(p);
 const info=document.createElement("p");info.className="subtle";info.textContent="同一命中在不同比较来源可能有不同结果。以下列出前 100 次及各结果的少量补充样例；完整明细在 CSV / JSONL。";el("detail").append(info);
 const reasons=document.createElement("p");reasons.textContent="未验证原因："+(Object.entries(m.reasons).map(([k,v])=>k+" ("+v+")").join("；")||"无");el("detail").append(reasons);
 const wrap=document.createElement("div");wrap.className="scroll";const table=document.createElement("table");wrap.append(table);
 const header=document.createElement("tr");["命中序号","镜像/模式","段","seq","结果","actual（或派生值）","expected（或独立来源）","差值","group / inode","syscall","syzkaller 程序 / call index / 调用","trace 路径:行号","理由"].forEach(t=>cell(header,t));table.append(header);
 for(const s of m.samples){if(el("kind").value&&s.kind!==el("kind").value||el("mode").value&&s.mode!==el("mode").value)continue;
 const tr=document.createElement("tr");[s.hit_index,s.kind+"/"+s.mode,s.segment_id,s.seq,labels[s.result],s.actual,s.expected,s.delta,s.group+" / "+s.ino,s.syscall_id,s.program ? s.program+" / "+s.call_index+" / "+s.syscall_name : null,(s.raw_trace||"")+":"+s.raw_line,s.reason].forEach(t=>cell(tr,t));table.append(tr)}
 el("detail").append(wrap);
}
function render(){
 const table=el("points");table.replaceChildren();let total=0,rows=0;
 const q=el("query").value.toLowerCase();
 for(const point of Object.values(data.points)){if(!(point.point+" "+point.formula).toLowerCase().includes(q))continue;
 const m=counts(point);if(!m)continue;const c=m.counts;if(el("nonzero").checked&&!c.inconsistent&&!c.ambiguous&&!c.unverified)continue;
 const tr=document.createElement("tr");cell(tr,point.point,"mono");cell(tr,point.formula);cell(tr,m.hits,"num");
 for(const k of Object.keys(labels))cell(tr,c[k],"num "+(k==="inconsistent"&&c[k]?"bad":k==="consistent"?"good":""));
 const td=cell(tr,"");const bar=document.createElement("div");bar.className="bar";
 for(const k of Object.keys(labels)){const span=document.createElement("span");span.style.width=(100*c[k]/m.hits)+"%";span.style.background=colors[k];bar.append(span)}td.append(bar);
 tr.onclick=()=>detail(point);table.append(tr);total+=m.hits;rows++}
 el("total").textContent=rows+" 个点/公式组合，当前比较来源共 "+total.toLocaleString()+" 次命中。";
}
async function boot(){
 const bytes=Uint8Array.from(atob(PAYLOAD),c=>c.charCodeAt(0));
 const decoded=await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).text();
 data=JSON.parse(decoded);el("status").textContent=data.run_id+" · "+data.observed_hits.toLocaleString()+" 次原始命中 · 完整采集："+data.coverage_complete;
 for(const [key,name] of Object.entries(names)){const opt=document.createElement("option");opt.value=key;opt.textContent=name;el("measurement").append(opt)}
 const kinds=new Set(),modes=new Set();for(const p of Object.values(data.points))for(const key of Object.keys(p.dimensions)){const [k,m]=key.split("/");kinds.add(k);modes.add(m)}
 for(const [target,values] of [["kind",kinds],["mode",modes]])for(const v of [...values].sort()){const opt=document.createElement("option");opt.value=v;opt.textContent=v;el(target).append(opt)}
 for(const id of ["measurement","kind","mode","query","nonzero"])el(id).addEventListener("input",render);
 for(const point of Object.values(data.onset_points)){const tr=document.createElement("tr");cell(tr,point.point,"mono");cell(tr,point.formula);
 cell(tr,Object.entries(point.counts).map(([k,v])=>(cls[k]||k)+": "+v).join("；"));cell(tr,Object.entries(point.recovery_points).map(([k,v])=>k+": "+v).join("；"));cell(tr,point.peak_abs_gap,"num");el("onsets").append(tr)}
 render();
}
boot().catch(e=>el("status").textContent="载入失败："+e.message+"。可查看 Markdown / CSV。");
</script></html>
"""
