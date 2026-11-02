#!/usr/bin/env python3
import argparse, json, pathlib

def main():
    p=argparse.ArgumentParser(); p.add_argument('root'); p.add_argument('--output', required=True); a=p.parse_args()
    root=pathlib.Path(a.root); rows=[]
    for label in ('ordinary','resource-valid'):
        r=json.loads((root/(label+'-campaign-report.json')).read_text())
        ex=r.get('exploration',{}); ns=ex.get('namespace_stats',{})
        verified=r.get('native_verified',0); transitions=ex.get('independent_transition_calls',0)
        row=dict(mode=label, generated_syscalls=r.get('generated_syscalls', 0),
                 target_syscalls=r.get('target_syscalls', 0), verified_syscalls=verified,
                 counter_validation_passed=r.get('counter_validation_passed', False),
                 kcov_feedback_complete=r.get('kcov_feedback_complete', False),
                 successful_syscalls=r.get('native_success',0), formula_transitions=transitions,
                 conversion_density=transitions/verified if verified else 0,
                 namespace={k:{'executions':v.get('executions',0),'successful':v.get('successful',0),
                               'success_rate':v.get('success_rate',0),'transitions':v.get('independent_transition_calls',0),
                               'conversion_density':v.get('conversion_density',0)} for k,v in ns.items()},
                 performance=r.get('performance',{}))
        rows.append(row)
    budget_ok = all(not r['target_syscalls'] or r['verified_syscalls'] >= r['target_syscalls']
                    for r in rows)
    equal_budget = len({r['verified_syscalls'] for r in rows}) <= 1
    evidence_ok = all(r['counter_validation_passed'] and r['kcov_feedback_complete'] for r in rows)
    result={'schema':1,'budget_complete':budget_ok,'equal_verified_syscalls':equal_budget,
            'evidence_complete':evidence_ok,'rows':rows}
    out=pathlib.Path(a.output); out.write_text(json.dumps(result,indent=2)+'\n')
    md=out.with_suffix('.md'); lines=['# A/B 生成策略测量','','| 模式 | 生成 syscall | 目标 | 已验证 syscall | 成功 | 公式转换 | 转换密度 |','| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for r in rows: lines.append(f'| {r["mode"]} | {r["generated_syscalls"]} | {r["target_syscalls"] or "-"} | {r["verified_syscalls"]} | {r["successful_syscalls"]} | {r["formula_transitions"]} | {r["conversion_density"]:.2%} |')
    lines += ['',f'最低预算验收：{"通过" if budget_ok else "未达到目标核验调用数"}；'
              f'两组实际核验调用数：{"相同" if equal_budget else "不同，不能作严格等量比较"}；'
              f'计数器与 KCOV 证据：{"完整" if evidence_ok else "存在不完整项"}。','',
              '## namespace 统计','','| 模式 | syscall | 执行 | 成功 | 成功率 | 转换 | 转换密度 |','| --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for r in rows:
        for name,v in r['namespace'].items(): lines.append(f'| {r["mode"]} | {name} | {v["executions"]} | {v["successful"]} | {v["success_rate"]:.2%} | {v["transitions"]} | {v["conversion_density"]:.2%} |')
    md.write_text('\n'.join(lines)+'\n')
    print(md)
    if not budget_ok or not equal_budget or not evidence_ok:
        raise SystemExit('A/B incomplete: verified budgets, counter validation, and KCOV feedback must all pass')
if __name__=='__main__': main()
