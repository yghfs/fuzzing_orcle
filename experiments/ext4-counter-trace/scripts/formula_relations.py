"""Direct object checks; aggregate equality cannot discharge a failed postcondition."""
from __future__ import annotations
import collections

FORMULAS = {'R-IH', 'R-IEVICT', 'R-IDELTA-EVICT', 'O-LIFE', 'O-LIFE-DELTA',
            'O-LIFE-FREE', 'XATTR-REF', 'XATTR-REF-DELTA', 'XATTR-REF.CSUM'}
CHINESE = {'R-IH': 'inode 表初始化高水位', 'R-IEVICT': 'inode 最终释放位',
           'R-IDELTA-EVICT': 'inode 释放计数增量', 'O-LIFE': 'orphan 槽位与生命周期',
           'O-LIFE-DELTA': 'orphan 空槽计数增量', 'O-LIFE-FREE': 'orphan 空槽总量',
           'XATTR-REF': '外部扩展属性引用总量', 'XATTR-REF-DELTA': '外部扩展属性释放增量',
           'XATTR-REF.CSUM': '外部扩展属性块校验和'}
TRANSITION_POINTS = {'ORPHAN.FILE.SLOT.ADD', 'ORPHAN.FILE.SLOT.DELETE',
                     'XATTR.BLOCK.REF.INIT', 'XATTR.BLOCK.REF.INC', 'XATTR.BLOCK.REF.DEC'}


def summarize(events, capture_complete=True):
    points, findings, pending, lifetimes = {}, [], {}, collections.Counter()
    for event in events:
        e = event
        if e['point'] == 'XATTR.BLOCK.REF.INIT':
            lifetimes[e['dev'], e['epoch'], e.get('block', 0)] += 1
        formula = e['formula']
        if formula not in FORMULAS:
            continue
        point = points.setdefault((formula, e['point']), dict(formula=formula,
            name=CHINESE[formula], point=e['point'], hits=0, consistent=0,
            inconsistent=0, unverified=0, raw=0, reasons={}))
        point['hits'] += 1
        status = e['status']
        valid = status in (1, 2) and e.get('comparison_valid', 1)
        if valid and (status == 1) != (e['actual'] == e['expected']):
            valid = False
            reason = 'status_and_values_disagree'
        else:
            reason = e.get('reason', 'unspecified')
        outcome = ('consistent' if status == 1 else 'inconsistent') if valid else (
            'raw' if status == 0 else 'unverified')
        point[outcome] += 1
        if outcome == 'unverified':
            point['reasons'][reason] = point['reasons'].get(reason, 0) + 1
        if formula == 'R-IH':
            obj = (formula, e['dev'], e['epoch'], e.get('group'))
        elif formula in ('R-IEVICT', 'R-IDELTA-EVICT', 'O-LIFE-DELTA', 'XATTR-REF-DELTA'):
            # Each postcondition belongs to one operation. Inode/block reuse,
            # or a later successful release, cannot repair an earlier failure.
            obj = (formula, e['dev'], e['epoch'], e['op'], e['point'])
        elif formula.startswith('XATTR-REF'):
            block = e.get('block', 0)
            obj = (formula, e['dev'], e['epoch'], block,
                   lifetimes[e['dev'], e['epoch'], block])
        else:
            obj = (formula, e['dev'], e['epoch'], e.get('block', 0),
                   e.get('slot', 0), e.get('ino', 0))
        if outcome == 'inconsistent':
            finding = dict(formula=formula, point=e['point'], seq=e['seq'], op=e['op'],
                ino=e.get('ino'), group=e.get('group'), block=e.get('block'), slot=e.get('slot'),
                actual=e['actual'], expected=e['expected'], reason=reason,
                stage='stable_memory' if e['point'] == 'SNAPSHOT.MEMORY.STABLE' else 'operation_postcondition',
                capture_complete=capture_complete, recovered_seq=None, recovery_point=None,
                recovery_status='not_observed')
            findings.append(finding)
            pending.setdefault(obj, []).append(finding)
        elif outcome == 'consistent' and capture_complete:
            for finding in pending.pop(obj, []):
                finding.update(recovered_seq=e['seq'], recovery_point=e['point'],
                               recovery_status='direct_same_object_comparison')
    return dict(available=bool(points), capture_complete=capture_complete,
                points=list(points.values()), mismatch_count=len(findings), findings=findings,
                limitations=['unverified_is_not_consistent',
                    'counter_totals_do_not_discharge_object_postconditions',
                    'later_operation_success_does_not_repair_earlier_postcondition',
                    'xattr_scan_enumerates_inode_referenced_blocks_not_all_allocated_blocks'])


def markdown(summary):
    lines = ['# 新增公式逐点检查', '',
        f'有效不一致样本：{summary["mismatch_count"]}；轨迹完整：{summary["capture_complete"]}。', '',
        '| 公式 | 插桩点 | 命中 | 一致 | 不一致 | 无法验证 | 原始事件 |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for p in summary['points']:
        lines.append(f'| {p["formula"]}（{p["name"]}） | {p["point"]} | {p["hits"]} | '
                     f'{p["consistent"]} | {p["inconsistent"]} | {p["unverified"]} | {p["raw"]} |')
    if summary['findings']:
        lines += ['', '## 不一致与恢复证据', '']
        for f in summary['findings']:
            lines.append(f'- {f["formula"]} seq={f["seq"]}，{f["point"]}，'
                f'inode={f["ino"]} block={f["block"]} slot={f["slot"]}，'
                f'actual={f["actual"]} expected={f["expected"]}；'
                f'恢复 seq={f["recovered_seq"]}（{f["recovery_status"]}）。')
    lines += ['', '未观测到同一对象的后续有效比较时，恢复位置保持为空。'
              '读取失败、不适用与并发样本均不计为一致。', '']
    return '\n'.join(lines)
