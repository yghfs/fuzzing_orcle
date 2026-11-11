# 覆盖证据审计（2026-11-11）

当前结论是：自定义反馈链已实现，现有结果尚不足以证明稳定覆盖收益。
一小时三臂测量完成；两条十二小时运行的 KCOV 样本完整，但生命周期与整轮验证失败。
本文核对现存报告和已有 [history-audit.json](history-audit.json)，没有重新运行 campaign，
也没有在本次文档同步中重新生成该历史审计 JSON。审计程序为 [audit_history.py](audit_history.py)。

## 一小时三臂结果

原始目录：[target-comparison-long-20261109](../../results/target-comparison-long-20261109)。

| 目标 PC 口径 | native syz-manager | custom PC-only | custom full-feedback |
| --- | ---: | ---: | ---: |
| admitted corpus | 2467 | 1854 | 2068 |
| 全部完整执行样本并集 | 不作同口径对照 | 2637 | 2630 |

该报告 `measurement_complete=true`，两个 custom 臂的随机 fault hook 命中均为 0。
full-feedback 的 admitted-corpus 较高，而全部完整样本并集略低。
报告已有 `native_transitive_syscall_closure_differs` 警告：native 缺少逐调用/设备归因，
传递 syscall closure、corpus 准入语义以及镜像状态策略也不相同（native mount 重建镜像，
custom 持续保留镜像）。因此不能从这张表直接排序机制优劣或外推长期收益。

## 十二小时两臂历史审计

两臂均为 seed `20261024`，regular ext4、ext4+jbd2 目标范围、16 syscall，
启用随机 fault，概率 1000 ppm，调度策略 rarity_fair。详细设置、吞吐与逐小时曲线见 JSON。

| 字段 | PC-only | Full-feedback |
| --- | ---: | ---: |
| 原始运行 | [campaign-9chz7m3j](../../results/campaign-9chz7m3j) | [campaign-fyubc_lv](../../results/campaign-fyubc_lv) |
| 完整 KCOV 样本 | 1513 | 1397 |
| effect/corpus 目标 PC | 3636 | 3256 |
| raw 全部完整样本目标 PC | 3819 | 3668 |
| invalid / missing / saturated 样本 | 0 / 0 / 0 | 0 / 0 / 0 |
| 生命周期分析失败段数 | 7 | 3 |
| `kcov_feedback_complete` / `raw_coverage_complete` | true / true | true / true |
| `passed` / `coverage_complete` / `counter_validation_passed` | false / false / false | false / false / false |
| `source_attribution_complete` | false | false |
| `status` | candidate_review_required | candidate_review_required |

effect/corpus 与 raw 统计采用不同准入口径，不可相互替代。
两个臂的执行样本数也不同；这些单 seed 描述性数值不能直接解释为反馈机制的因果效果。
历史 audit 的 consistency checks 核对了样本数和增益求和；这不等于生命周期验证通过。

失败段：PC-only 为 `97,693,954,1089,1278,1298,1465`，full-feedback 为 `326,1089,1298`。
所有失败均立即跟随 fault，下一段记录文件系统 reset 完成，但带入的旧 epoch 与新 mount
epoch 不能组成有效生命周期链。报告含 `sequence_missing_duplicate_or_wrong_epoch`、
`device_identity_not_unique`，需要单独修复并验证 reset/carry 边界。
这些信息不足以认定 ext4 内核缺陷。

## 使用边界

原始结果目录位于本机，通常由 Git 忽略；只有本文和审计脚本/JSON 并不构成可移植的全量复现包。
不能把已清理的旧 campaign 摘要补进这次对照，也不能把磁盘解析器修复说成重验了历史报告。
当前离线 BIGALLOC 修复、回归结果及文档可用性见
[根目录文档审计](../../../../DOCUMENTATION-AUDIT-20261111.md)。
