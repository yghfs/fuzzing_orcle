# 第 1–3 步：冻结、正确性门槛与吞吐基线

日期：2026-11-12（Asia/Shanghai）。本记录对应第 1–3 步，不代表多 seed 消融或长期反馈收益。

## 1. 工作区快照

实验开始时的 HEAD、tracked diff、未跟踪文件、关键二进制及输入哈希已保存到：

```text
/tmp/fuzzing-orcle-step1-20261112-152327/
```

目录含 `git-head.txt`、`git-status.txt`、`worktree.patch`、`untracked-files.tar.gz`、
`artifact-sha256.txt` 和 `manifest.txt`。基线 HEAD 为
`6ab34f14f2b1e56f99c68b220d0c2fc964e63d6d`。

```text
bzImage    59fae3a9259a8c3ed081fc2c096a96c1faecdc90e98a17f6022fbfae1463df86
vmlinux    4efaf6523775dbf7e7f9fbd3cf66fa4f927cb18af3faaf79e2023967ff90d913
rootfs     fb366e68162b5ffebbf65f0dc095c99b6b2b9764f9235c680049838d6c20fc7f
SSH key    db422002819cc41dac8c464d4f2d673aa8b097fca904c22f93e4f1415bbc39f1
```

没有覆盖原有未提交改动，也没有修改内核或 rootfs 镜像。

## 2. 正确性门槛

以下全部通过：

| 检查 | 结果 |
| --- | --- |
| `test-campaign.py` | 81 passed |
| `test-kcov-feedback.py` | 56 passed |
| `test-lifecycle.py` | 31 passed |
| `test-target-coverage-comparison.py` | 8 passed |
| `test-syzkaller.py` | 22 passed |
| 新增 `test-performance-report.py` | 3 passed |
| `bash -n tests/guest-campaign.sh` | passed |
| `git diff --check` | passed |
| `source_audit.py --require-build` | `passed=true`, `issues=[]` |

PC-only、联合反馈及计时修复验证三个实际 QEMU campaign 都 `passed=true`、
`coverage_complete=true`、`counter_validation_passed=true`、无 issues；KCOV 均无缺失、
无 invalid、无 saturated 样本。每个 12 段 run 都有 192/192 个 native syscall 核验。

## 3. 短程吞吐与阶段剖析

PC-only 和联合反馈各运行 12 段、每段 16 calls，seed 均为 `20261112`，同一内核和 rootfs，
`target_scope=ext4_jbd2`、`scenario=baseline`、`rarity_fair`，随机故障注入关闭。两次顺序运行，
不是并发对照；只有一个 seed，因此数字仅作为新的性能基线。

运行目录：

- PC-only：`results/campaign-5968ukaz`
- PC+edge+semantic：`results/campaign-_re12s59`
- 当前计时逻辑的 3 段 smoke：`results/campaign-83tzmwlz`

复现命令（两个 12 段臂只改 `--feedback-mode` 与 SSH 端口）：

```sh
bash experiments/ext4-counter-trace/scripts/run-campaign.sh \
  --mode single --segments 12 --duration-seconds 900 \
  --segment-seconds 1 --max-batches 1 --batch-timeout 30 \
  --cpus 1 --guest-memory-mb 1024 --workers 1 --memory-mb 0 \
  --syzkaller-calls 16 --resource-valid-every 1 --fresh-generate-every 0 \
  --resource-seed-refresh-every 4 --target-scope ext4_jbd2 --target-kind regular \
  --scheduler-policy rarity_fair --feedback-mode pc \
  --strict-kcov-feedback --port 12473 --seed 20261112
```

第二臂把 `--feedback-mode pc` 换成 `pc_edge_semantic`，端口换为 `12474`。

| 阶段/指标 | PC-only | 联合反馈 |
| --- | ---: | ---: |
| verified syscall/s（按 segment wall time） | 0.6626 | 0.6748 |
| 目标 effect PC 并集 | 2782 | 2788 |
| 全样本目标 PC 并集 | 2828 | 2838 |
| verified 公式转移数 | 51 | 52 |
| syscall 成功数 / 192 | 139 | 136 |
| guest execution p50 / p95 | 18.19 / 26.64 s | 17.78 / 22.94 s |
| workload p50 | 9.08 s | 8.68 s |
| 6 次稳定快照耗时 p50 | 3.65 s | 3.54 s |
| upload p50 | 1.66 s | 1.81 s |
| fetch p50 | 1.89 s | 1.81 s |
| feedback p50 | 0.047 s | 0.047 s |
| analysis p50 / p95 | 2.61 / 3.07 s | 2.64 / 3.06 s |

`workload` 和 `snapshot_one` 是 `guest_execution` 的子阶段，不能与 guest 总耗时相加。
反馈计算仅约 47 ms/段；这组数据仍指向 guest 工作负载及其周边 I/O，而不是 Python 反馈
计算为主要耗时。该短测的 Full 数字略高，不足以证明联合反馈收益；目标 PC 差仅十余个，
而且没有多 seed 与等调用量统计分布。

## 计时实现修正

原 runner 在开始下一段之后才 finalize 前一段，旧代码把 analysis 时间记入当前下一段的
`segment_timings`。现已修正 `campaign_runner.py`：analysis 计入 campaign 累计时间，并写回
正在分析的 segment manifest；等待前一 trace seal 的耗时也写回其所属段。性能报告现在列出
`workload`、`snapshot_one`、fetch、feedback、analysis 以及 campaign 阶段，并标明嵌套阶段关系；
对历史 campaign 会纠正旧 analysis 计时错位。

3 段计时 smoke `campaign-83tzmwlz` 通过，manifest 中三段 analysis 分别为 2.676、2.627、
1.989 秒，和 campaign 累计 `7.293 秒 / 3 calls` 相符。对应回归覆盖位于
`tests/test-performance-report.py`。

历史 PC-only 和联合反馈 report 已用修正后的 `performance_report.py` 重建并写回各自结果目录。
这只修复报告归属；历史 run 本身仍使用原 runner 执行，不能视作当前计时代码的在线验证。
