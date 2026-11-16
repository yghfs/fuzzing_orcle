# 第一批反馈修复与验证

日期：2026-11-12（Asia/Shanghai）。本轮完成模板探索、目标 edge 和 reset/carry 三项修复。
机器可读证据见 [validation-summary.json](validation-summary.json)。未提交 commit，未修改内核，
未覆盖历史 campaign 报告。本轮不评价长期覆盖收益。

## 实现

1. `FeedbackCorpus.parent_candidate()` 按模板筛选资源父本；typed 资源池全冷却时返回空，
   由 runner 从同一模板重新播种。普通父本池仍保留周期探索行为。
2. runner 不再用父本模板覆盖计划模板。combined 模式新增
   `--resource-seed-refresh-every`，默认 4：每个模板独立在第 0、4、8……个实际 typed
   槽位刷新 seed。seed_only 不消费父本；parent_only 仍只在无活跃父本时产生明确的 bootstrap。
   元数据记录计划/实际模板、模板槽位和刷新原因。
3. 目标为 ext4/JBD2 时，edge 准入、rarity、频率与子代 edge 收益统一使用目标范围。
   `raw_new_edges`、`effect_new_edges` 等全内核诊断继续保留，新增 `feedback_edge_scope`。
   三臂比较也核对资源策略、刷新周期和 edge 范围，防止混合处理条件。
4. 将生命周期区间构造提取为 `lifecycle_intervals()`。只有 reset 已完成且证据匹配时，
   才取消目标设备的旧 carry，使用新 epoch 的原始 start/end；其他设备继续携带旧锚点。
   快照锚点按设备、epoch、操作身份匹配。未完成 reset、错误设备、epoch 未改变、
   缺少证据均拒绝继续。序列丢失、重复检查没有放宽。
5. 新 guest `fault-reset.json` 使用 schema 2，记录 block device、counter device，以及
   reset 前后 counter state。历史 schema 1 仅在存在前段 epoch 和匹配的完整区间时用于重分析，
   明确标记为较弱的 legacy 证据。

## 回归检查

| 检查 | 结果 |
| --- | --- |
| `python3 -B experiments/ext4-counter-trace/tests/test-campaign.py` | 81 项通过 |
| `python3 -B experiments/ext4-counter-trace/tests/test-kcov-feedback.py` | 56 项通过 |
| `python3 -B experiments/ext4-counter-trace/tests/test-lifecycle.py` | 31 项通过 |
| `python3 -B experiments/ext4-counter-trace/tests/test-target-coverage-comparison.py` | 8 项通过 |
| `python3 -B experiments/ext4-counter-trace/tests/test-syzkaller.py` | 22 项通过 |
| `scripts/source_audit.py --require-build` | `passed=true, issues=[]` |
| guest shell syntax、`git diff --check` | 通过 |

合计 198 项相关测试。新增覆盖模板持续轮换、每模板固定 seed 刷新、父本选择提交、
资源池全冷却回退、split 模式、仅通用新边禁止作为目标边入库、真实目标失败调用保留、
reset 证据校验、快照 ID 重用和下一段新 epoch carry。负例在 reset 设备和未 reset 设备上
分别删除/重复事件，仍然失败。

## 原始历史失败段重分析

[replay-reset-boundaries.py](replay-reset-boundaries.py) 将原始数据复制到新的临时目录，
重建区间并运行实际生命周期分析器；没有修改原结果。

- PC-only：`97,693,954,1089,1278,1298,1465`，7/7 通过。
- Full：`326,1089,1298`，3/3 通过。

每段的三个设备 trajectory integrity 均通过，blockers 均为空。这验证了已知 reset/carry
问题的修复；不代表重新验证全部历史段、磁盘或十二小时整轮报告。

## QEMU 普通短程

结果：[campaign-4q4qfrjm](../../results/campaign-4q4qfrjm/campaign-report.json)。

使用单 worker、regular ext4、16 calls、ext4/JBD2、rarity_fair、联合反馈和严格 KCOV 检查；
12 段、192 个生成调用全部得到 native 核验。运行、覆盖和 counter validation 均通过，issues 为空。

namespace/file-state 各 6 个程序，严格交替；每个模板槽位 0 和 4 从 seed 刷新，其余 4 个
槽位使用同模板父本。逐程序核对所有反馈 `edge:` 特征，两端地址均落在目标符号范围。

复现命令：

```sh
python3 -B experiments/ext4-counter-trace/scripts/campaign_runner.py \
  --mode single --segments 12 --duration-seconds 900 \
  --segment-seconds 1 --max-batches 1 --batch-timeout 30 \
  --cpus 1 --guest-memory-mb 1024 --workers 1 --memory-mb 0 \
  --syzkaller-calls 16 --resource-valid-every 1 --fresh-generate-every 0 \
  --resource-seed-refresh-every 4 --target-scope ext4_jbd2 --target-kind regular \
  --scheduler-policy rarity_fair --feedback-mode pc_edge_semantic \
  --strict-kcov-feedback --port 11973 --seed 20261112
```

## QEMU 故障、reset、正常工作

结果：[campaign-6bdpspyj](../../results/campaign-6bdpspyj/campaign-report.json)。

[verify-reset-qemu.py](verify-reset-qemu.py) 是有界验证驱动，仅第 0 段启用注入，之后移除
每段 fault 配置；运行、采集、隔离、reset 和分析均使用实际 runner。该驱动没有覆盖分析结果。

确认命中一次 `unlink_inode_dirty`，注入 `ENOSPC`；同一任务的 native `unlinkat`
返回 `-28`，用户态 errno 28，证据关联通过。第 1 段执行真实 mkfs/mount reset，产生
schema-2 counter state 证据；第 1、2、3 段生命周期和所有设备 trajectory integrity 均通过。

第 0 段仍有 `regular-single:single_writer_unverified_episode`，由注入后的无效稳定快照产生，
没有检测到已确认的 operation-exit/persistent mismatch。原有聚合器将该故障段的不可验证状态
保留在 `fault_injection.unverified` 中。整轮 `passed` / `counter_validation_passed` 为 true，
但 `status=candidate_review_required`，因此不能称为无告警的普通通过或自然 ext4 缺陷。

复现命令：

```sh
python3 -B experiments/ext4-counter-trace/research/2026-11-12-feedback-diagnosis/verify-reset-qemu.py \
  --mode single --segments 4 --duration-seconds 600 \
  --segment-seconds 1 --max-batches 1 --batch-timeout 30 \
  --cpus 1 --guest-memory-mb 1024 --workers 1 --memory-mb 0 \
  --syzkaller-calls 16 --resource-valid-every 1 --fresh-generate-every 0 \
  --resource-mutation-mode seed_only --target-scope ext4_jbd2 --target-kind regular \
  --scheduler-policy uniform_round_robin --feedback-mode pc_edge_semantic \
  --strict-kcov-feedback --port 11974 --seed 20261113 \
  --random-fault-injection --random-fault-probability-ppm 1000000 \
  --random-fault-operations unlink_inode_dirty --random-fault-errnos ENOSPC
```

验证范围为普通 regular ext4 的短程执行，以及其他两个设备的连续性保持；没有在本轮验证
bigalloc/inline 作为 reset 目标的长期运行。下一阶段可以开展提速测量或多 seed 受控消融，
不能将这两次冒烟的绝对覆盖数用于证明反馈收益。
