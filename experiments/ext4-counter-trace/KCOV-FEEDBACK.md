# KCOV 反馈闭环路线

## 2026-10-21 调研与审计补充

外部工具方法对照、覆盖指标定义及历史调度审计见
[反馈策略调研](research/2026-10-21-feedback/README.md)。复核 `campaign-0mwk51uk` 的
4,619 段记录发现：225 个 corpus 文件中，1,847 次变异全部使用首个父程序；
当前 `ordinal % sum(weights)` 选择没有在该预算内走出首种子的权重区间。
因此本文“加权轮转”不能解释为已经保证种子探索公平，也不能把 corpus 大小视作实际参与变异的种子数。

现有等量 A/B 支持资源有效前缀提高短测中的公式转换密度，尚未独立验证联合反馈系数或父种子调度收益。
段级公式点统计包含快照、准备和清理事件，后续应补充程序级归因、稳定状态特征和与均匀抽样的消融对照。
当时本次补充没有改变运行时策略；历史原始证据随后按保留清单清理。

这份文档记录 ext4 counter-trace 子项目如何把 syzkaller 程序、KCOV 覆盖、插桩点差值和持久化结果接成一个可审计的闭环。第一至第四批反馈代码已落地，并完成离线测试及受控 VM 短测；2026-10-23 的四组旧长跑审计发现语义操作出口字段未接通，所以那组结果不能作为完整语义反馈对照。修复后的短 VM 证明语义出口可用且能独立准入；联合反馈是否提高长期覆盖仍需重跑对照确认。

## 目标覆盖口径与外部基线

文件系统实验的主指标是同一 counter-trace 内核上的 ext4/JBD2 目标 PC 覆盖。`feedback/coverage.json` 现在同时保存 `ext4_pc_coverage`、`jbd2_pc_coverage`、`target_pc_coverage`、`target_fraction` 和 `target_scope`；`total_coverage` 仍是全内核 PC 诊断值。为兼容旧结果，`total_ext4_coverage` 保留为旧范围视图的别名，不能把它与新字段混作跨版本基线。

目标范围只接受 kallsyms 的 `T/t/W/w` 文本符号，范围终点由下一个文本符号确定；覆盖 `ext4_`、`_ext4_`、`__ext4_`、`ext4fs` 以及 `jbd2_`、`_jbd2_`、`__jbd2_`，并排除 trace/perf/prefix/event 辅助符号和数据符号。新矩阵默认传 `--target-scope ext4_jbd2`，因此 `PC-only` 的准确含义是“只启用目标 ext4/JBD2 PC 信号的内部消融”，不是 upstream syzkaller，也不是旧版全内核 PC-only。需要复现旧内部口径时显式使用 `--target-scope all`，结果必须单独标记。

外部基线由 upstream `syz-manager` 独立运行，使用同一 bzImage、vmlinux、rootfs backing、TCG/QEMU 参数和 24 个请求 syscall（包括 `syz_mount_image$ext4` 及其 `getegid`/`geteuid` 资源创建依赖）；manager 会按资源依赖扩展为更大的传递闭包，结果中的 `actual_enabled_syscall_count` 才是实际集合大小。每次运行使用独立 qcow2 rootfs overlay，不会修改共享 backing。upstream 默认 seed 是 `syzgen/seeds/upstream-ext4.syz`：它嵌入带 `has_journal` 的 ext4 镜像，通过 `syz_mount_image$ext4` 后执行创建、写入、分配、截断、同步、重命名和删除，避免普通 `./` 路径落入 executor 临时文件系统。自定义 runner 的四组反馈仍是内部消融。只比较目标 PC 时，应使用下面的统一并行入口：

```sh
bash experiments/ext4-counter-trace/scripts/run-target-coverage-comparison.sh \
  --duration-seconds 3600 --seed 20261024 --feedback-mode pc \
  --target-scope ext4_jbd2 \
  --output experiments/ext4-counter-trace/results/target-comparison-$(date +%Y%m%d-%H%M%S)
```

它同时启动一个自定义 VM 和一个 upstream manager，等待同一时间预算结束，再生成 `comparison.json`；任一 runner 异常退出时会立即停止另一侧，避免失败的一侧留下无人值守的 VM。正式对齐指标是自定义 `corpus_target_pc_coverage`（已准入程序的 ext4/JBD2 PC）与 upstream `/rawcover` 的 `target_ext4_jbd2_pc`；自定义 `target_pc_coverage` 仍包含所有完整样本，只作为诊断。比较报告会保存执行量、实际 syscall 闭包和公平性警告。

当前两者的目标设备仍不同：自定义 runner 在 vdb/vdc/vdd 三块私有 ext4 盘上执行，upstream manager 在 guest 内通过 seed 挂载独立的带 journal ext4 镜像；syzkaller 也不能应用自定义 seed。因此 `comparison.json` 将 `strictly_causal_comparison` 标记为 false，这个基线是同内核下的外部参考，不能单独证明反馈策略收益。要做严格策略对照，后续必须让两边使用相同目标设备和相同实际 syscall 集。

单独运行 upstream 基线的入口为：

```sh
bash experiments/ext4-counter-trace/scripts/run-upstream-syzkaller-baseline.sh \
  --mode fuzzing --duration-seconds 3600 --http-port 12900 \
  --seed 20261024 \
  --output experiments/ext4-counter-trace/results/upstream-baseline-$(date +%Y%m%d-%H%M%S)
```

该脚本保存 manager 配置、日志、输入哈希、corpus 和周期性覆盖快照。正式 baseline 使用 `/rawcover`，因为它只返回 manager corpus 中实际覆盖的 PC。调试用 `/cover?jsonl=1` 时，采集器只计入 `hit_count > 0` 的记录；零命中 frame 是插桩全集，不能算覆盖。`scripts/collect-upstream-coverage.py` 计算全内核 PC、ext4 PC、JBD2 PC、联合目标 PC 及目标比例；只有相同内核、过滤器和 corpus 范围下的目标字段才可比较。旧 smoke 结果中的 `647433/19726` 是零命中 frame 污染后的无效数字，必须重跑，不能作为 baseline。`smoke-test` 只验证 manager/QEMU 启动，若 manager 尚未建立 corpus，覆盖端点返回 500 属于“尚未可采集”，不应当被解释为零覆盖。

## 当前闭环：单 VM、单 worker、按程序反馈

一次 campaign 的反馈循环如下：

```text
Generate(seed) 或 Mutate(corpus parent)
        │
        ├─ 保存 program.syz、原始 C、插桩 C、二进制和调用元数据
        │
        ├─ guest 进程打开 /sys/kernel/debug/kcov
        │       KCOV_INIT_TRACE → mmap → KCOV_ENABLE(TRACE_PC)
        │       清零 setup 覆盖 → 执行生成 syscall 序列
        │       每个 syscall 保存 PC 起止位置
        │       KCOV_DISABLE → 写 coverage/<program>.bin
        │
        ├─ native syscall BEGIN/END、ext4 trace 和稳定快照照常采集
        │
        ├─ host 校验 coverage 文件、overflow、syscall 配对和生命周期分析
        │       只在一次 syscall 的 PC 范围内提取 edge
        │       只将精确归因到 program/call_index 的公式事件作为状态特征
        │
        └─ 按反馈消融配置合并 PC/edge/语义集合；产生启用信号增益的 program 才进入 corpus
```

KCOV v3 样本保存 magic、版本、PC 数、flags、PC 数组、调用数，以及每个 syscall 的 PC 起点和长度。host 从单次 syscall 内相邻 PC 构造边，绝不把两个 syscall 的首尾相连。旧 v1/v2 样本继续解析 PC，但没有调用边界，因此不产生 edge。当前 guest 默认使用 v5 feature-set 样本：header 后保存 `raw_pc_count`、唯一 feature 记录和调用范围表；记录 `(pc, 0)` 表示 PC，记录 `(from_pc, to_pc)` 表示 edge。唯一表在每个 syscall 开始时清空，因此重复执行只保留该 syscall 的一个 PC 或 edge，同时调用范围仍能把收益归因到具体调用。flags bit 256 表示 feature-set，bit 512 表示唯一表容量不足；后者是明确的 `feature_table_limit` 饱和原因。v4 RLE 和 v1-v3 样本继续兼容。KCOV 初始化失败、文件截断、非法调用范围、overflow、超时或 native syscall 未核验时，样本不得更新覆盖或进入 corpus。已标记 overflow 且超过解析阈值的样本只校验头部和长度，不解包全部 PC；这类样本记录为 saturated。每个 guest worker 只有自己的 KCOV fd 和 buffer；语义反馈目前只在 single 模式启用。

host 维护四类证据：

- `segments/<id>/coverage.json`：本段每个 program 的命中数、唯一数、新增数、overflow、哈希和是否入 corpus。
- `feedback/coverage.json`：全样本 PC、edge、语义特征数量，已准入 corpus 的 PC 视图，样本完整性及 corpus 大小。`campaign.json.kcov_feedback` 保存同一累计摘要；不能用最后一段的状态覆盖整轮状态。`coverage_scope=all_complete_samples`，`corpus_coverage_scope=admitted_corpus`；对接 upstream 时使用 `corpus_target_pc_coverage`。
- `feedback/scheduler.json`：父程序内容哈希、选择/变异次数及其子代发现的新 PC、edge 和语义特征。
- `corpus/<program-sha256>.syz`：按程序内容去重；只有发现当前 `feedback_mode` 启用的新 PC、edge 或可信语义特征才准入。

输入任务按固定周期选择 resource-valid、fresh generate 或 mutate。默认 mutate 使用 corpus 均匀轮转；启用 `rarity_fair` 时按特征观测频率做逆频率加权，并以有界未选间隔避免种子饥饿。只有生成器确实返回 `Mutate` 后才推进游标；`GenerateFallback` 不消耗轮转位置。任务先于父程序选择决定，segment 序号不直接进入父程序权重。父程序元数据记录 `parent_sha256`、选择序号、任务类型和调度策略。动态变异预算依据该父程序分数提高调用数，最高为基础预算两倍且受显式上限约束；固定预算仍可用于对照。

语义状态按 `formula/point/status/value_kind/delta_bucket/syscall_outcome/operation_exit` 归一化；不包含 inode 编号、地址或原始计数。事件必须有完整 trace 轨迹、明确 phase 格式，并精确带有 program、call_index 和 same-task syscall 归因。setup、snapshot、cleanup 和 background 事件不获得程序奖励。整段 mismatch 与 unverified 只保留为诊断，不作为覆盖分数或 corpus 准入理由。

`run-feedback-study.py replay` 会在同一 seed 生成并重放首个程序，验证起始盘、rootfs、kernel、程序 hash 和反馈特征稳定性；它还没有对长跑中每个准入 corpus 程序自动做 deflake。因此“完整样本”仍表示本次执行的 KCOV/trace/native 证据通过校验，不代表每个语料特征均经重复验证。manager、多 VM corpus 合并、KCOV remote/CMP、程序最小化和并发语义调度也仍未实现。

## 验证范围与后续边界

四批的实现、离线测试与 VM 证据列在下方“当前状态”。覆盖样本校验、调用边界、程序归因、corpus 去重和可信语义准入都有自动化检查；VM 短测同时经过 native syscall 核验及现有 trace、稳定快照、fsck 和离线磁盘检查。通过这些检查说明该单 VM 实验闭环可用于受控长跑。

本次不包含长期统计显著性结论，也没有实现多 VM syzkaller manager、跨 worker corpus 合并、KCOV remote/CMP、程序最小化、checkpoint 恢复或自动 VM 重启。长跑仍需操作者启动和监控。长期报告应继续跟踪有效样本率、KCOV overflow/loss、coverage 增益曲线、corpus 大小、插桩点命中率、native syscall 核验率、操作出口/稳定态不一致，以及 fsck/P-DISK/R-UG/R-LINK 交叉验证结果。

## 2026-10-23 四组各 3 小时实测

当时的完整分析、逐段重算脚本、结构化统计和增长 CSV 位于 `feedback-long-20261022-235615/ANALYSIS.md`；该历史目录已按结果保留清单删除。四组均以 duration_limit 停止，native syscall 全部核验，计数与磁盘检查通过；PC+语义有一个 saturated 样本，故该组 `kcov_feedback_complete=false`，不能仅看 shell exit=0 或 `passed=true`。

| 配置 | 全内核 PC | ext4/JBD2 PC | edge | 语义特征 | corpus |
| --- | ---: | ---: | ---: | ---: | ---: |
| PC-only | 11103 | 3784 | 14199 | 118 | 147 |
| PC+edge | 10440 | 3595 | 13196 | 115 | 278 |
| PC+语义 | 10922 | 3714 | 13938 | 120 | 139 |
| 联合反馈 | 10838 | 3631 | 13776 | 115 | 292 |

`total_ext4_coverage` 包含 `jbd2_` 符号，故此处标为 ext4/JBD2。PC-only 在本轮的最终代码覆盖最高；对齐每组最先执行的 18,784 次 syscall 后排序不变。每组只有一个 seed 的一次运行，不能据此给出长期显著性结论。

语义启用的两组均没有语义独立触发的 corpus 准入；PC+edge 与联合反馈分别有 142、158 个无新 PC、仅新 edge 的种子准入。所有已记录语义特征的 `operation_exit` 都为 `unknown`，因为 `kcov_feedback.augment()` 读取 run 的 `operations` 键，而生命周期报告输出的是 `boundaries`。因此这轮记录的是存在语义出口缺口的实现，不能代表完整语义反馈效果。本次审计保留原始产物；之后的接线修复和验证见下文。

本轮固定 `uniform_round_robin` 和 `fixed`，没有验证第四批 `rarity_fair`/`dynamic` 的收益。当时待办是修复语义出口接线、验证语义独立准入并定位饱和原因；已完成项记录如下。

## 2026-10-23 语义出口修复、独立准入与饱和诊断

`trace_lifecycle.py` 将 native syscall 返回分类写入 `runs[*].boundaries`。反馈端此前只读取 `runs[*].operations`，因此所有语义特征的 `operation_exit` 落成 `unknown`。`kcov_feedback.py` 现优先读取 `boundaries`，并保留 `operations` 作为旧报告兼容键。

新增 `scripts/audit-semantic-admission.py`，可以从一段已完成的真实 campaign 读取 KCOV、生命周期报告和 timeline，预先把该样本的所有 PC 和 syscall 内 edge 放入已知集合，再分别重放 PC-only 与 PC+语义准入。此前实测产物 `campaign-8oewt01x/semantic-independent-admission-audit.json` 显示：PC 和 edge 新增都为 0；PC-only 不准入，PC+语义得到 10 个新语义特征并准入，出口分类为 `consistent`。该历史产物已清理；这次测试使用真实 VM 生成的输入和报告，验证语义信号可以独立触发入库。

同一 campaign 是 3 段短 VM 检查：48 个 native syscall 全部通过核验，3 个 KCOV 样本完整，计数校验、fsck 和离线磁盘检查均通过。首段实际报告包含 native syscall 出口，语义特征中的 `operation_exit` 不再是 `unknown`。原始结果 `campaign-8oewt01x` 已按清单清理。

对旧饱和样本的复查位于 PC+语义长跑的第 1093 段。header 为 flags `5`（旧格式仅表示 saturated 和 call ranges），总 PC 数 8,389,724；最大单调用保留 3,752,679 项。旧 runtime 只保存一个饱和位，未保存触发分支或运行时 `CT_KCOV_ENTRIES`，所以不能从保留的调用范围唯一确定原因。该段的 `fallocate`（call index 14）结束时累计数恰为 8,388,608，随后 `fsetxattr` 又追加 1,116 项。这个形状与“前一次扩容失败，截到原容量；后一次扩容成功后继续追加”相容，也不能排除累计上限截断等路径。最大调用范围可能已经被截断，不能据此反推出实际 buffer 大小。

前一次审计曾根据“最终累计数不是 2 的幂”推断单调用 buffer 满，并给出 `CT_KCOV_ENTRIES=3,752,680`；这个推断遗漏了失败后后续调用仍可再次分配成功的路径，现已撤回。已用保存的旧 runtime 编译 C harness 验证反例：第一次 `realloc` 失败后累计数为 65,536，第二次分配成功后累计数为 65,539，饱和位仍为 1。审计现保留源码哈希匹配和容量边界观察，将原因记为 `legacy_or_unclassified_saturation`，不声明有未留档的环境覆盖值。

新 runtime 移除逐步扩大的累计 PC heap，改为把每次 syscall 的 PC 流式写入匿名临时 spool，结束时再组装 KCOV v3 样本。仍保留每调用 16,777,216 项和总计 16,777,216 项的显式上限，并在 flags 中区分 `per_call_buffer_full`、`aggregate_pc_limit`、`sample_spool_io_failed`；无法创建 spool 时，workload 会明确记录 `spool_create_failed` 并失败退出。workload 摘要同时记录累计 PC 数、最大调用及其 PC 数、原因 flags；host campaign 摘要汇总可解释的饱和原因。不能由保存源码和调用范围消歧的旧样本仍归类为 `legacy_or_unclassified_saturation`。短 VM 的最大单调用为 3,843,304 项，总计最大 3,853,554 项，原因 flags 为 0，没有再出现饱和。

可复算的旧/新样本比较此前保存在 `campaign-jr3i_8l_/kcov-saturation-audit.json`，该历史产物已清理。在专门检查诊断字段的 1 段 VM 中，新格式经 guest 日志、segment manifest 持久化到 host；16 个 native syscall 核验通过，样本累计 30,526 个 PC，最大单调用 9,607 个 PC，饱和原因 flags 为 0，campaign 检查通过。这个小样本验证了诊断字段链路；它不等于大样本压力边界测试。

相关离线测试覆盖真实 `boundaries` 字段语义出口、PC/edge 已知时的语义独立准入、重复特征不重复入库，以及新旧饱和原因解析；runtime C harness 也分别触发并检查 per-call 满和累计上限 flags。本次四组回归测试全部通过：反馈 24 项、syzkaller 9 项、campaign 42 项、lifecycle 22 项。短 VM 使用 `--feedback-mode pc_semantic --segments 3 --workers 1 --syzkaller-calls 16 --target-syscalls 48`，结束状态为 `passed=true`、`coverage_complete=true`。

## 2026-10-23 覆盖收益诊断

当前完成的是语义出口接线、独立准入机制和 KCOV 饱和诊断修复；修复后的长期覆盖优势尚未验证。语义独立准入审计预先将真实样本的 PC/edge 标为已知，只能证明语义新颖性可以单独触发准入，不等价于自然长跑中已出现语义独立准入或覆盖提升。

复核旧四组长跑与当前代码，有以下证据和解释：

- **语义信号在旧运行中没有独立改变 corpus。** 两组启用语义的运行，语义独立准入均为 0。出口字段接通后，短 VM 的新特征分类为 `consistent`，但还需要自然执行出现新增语义且没有新增代码信号，再评估这些父程序的后代覆盖收益。
- **新增 edge 语料与均匀调度存在预算稀释风险。** PC+edge 的 corpus 从 PC-only 的 147 增至 278；父选择中位数从 4 降到 2。edge-only 父程序占 237/605 次选择，其直接子代新增 88 PC；PC 准入父程序占 368 次选择，子代新增 1,746 PC。联合反馈的对应数为 290 次/826 PC 和 297 次/2,364 PC。这支持按收益分配预算的假设，但观察性归因不能证明调度调整必然更优。旧矩阵固定 `uniform_round_robin`/`fixed`，未验证第四批的稀有度和动态策略。
- **输入结构相似度高。** 四组全部 resource-valid 程序都是 16 个调用，其中 15 个来自同一 namespace 模板；普通路径和若干名字随 seed 改变，模板的调用关系保持固定。约一半程序使用该模式，因此约 46.9% 的总调用预算用于同一保护前缀。它有利于构造有效资源，但仅剩一个生成尾调用，限制了新操作组合的探索。`fresh_generate_every=0` 也关闭了普通路径的全新 Generate；仍有变异和资源模板的随机尾部，不能说完全没有随机探索。
- **旧版本的奖励目标比 ext4 覆盖更宽。** 这项审计针对 target-scope 接线前的历史代码：当时 corpus 接纳任意全内核新 PC，稀有度也使用全内核特征。PC-only 的 148 次 PC 准入中，74 次没有新增 ext4/JBD2 PC；联合反馈的 135 次 PC 准入中有 69 次。新运行默认使用 `target_scope=ext4_jbd2`，这些历史比例不能外推到修复后的结果。
- **当前“语义”主要区分插桩事件，较少区分持久资源状态。** PC-only 观察到的 118 个旧语义特征中，106 项的 delta bucket 为 `na`，其余 12 项为 `0`；联合反馈的 115 项中是 103 项 `na` 和 12 项 `0`。这不代表 118/115 个不同文件系统状态。当前 key 没有显式编码文件大小/extent 桶、空闲资源压力、目录规模、资源间关系或状态转换序列，也没有包含 regular/bigalloc/inline 类型。每个程序还会重新准备并清理私有目录，旧长跑使用 baseline 场景，限制了通过同一资源逐步构造复杂状态的机会。修复 operation exit 不会自动补齐这些状态表达。
- **执行吞吐限制了绝对探索量。** PC-only 的 3 小时仅执行 19,488 次生成 syscall，约 1.80 次/秒；记录的 workload 阶段为 915.7 秒，占采集时间约 8.5%。生成、编译、SSH 上传、快照、取回与分析消耗了大量时间。阶段计时有嵌套，不可直接相加。相同执行量下联合反馈仍落后，所以吞吐不是四组排序的全部解释，但它是增加探索总量的重要约束。
- **单次结果包含反馈波动。** PC-only 与 PC+语义前 19 个程序（segment 0–18）内容相同，但 segment 9 的 PC-only 新增 1 PC，另一组为 0，导致 corpus 提前分叉；segment 19 首次选出不同父程序。说明即使没有语义独立准入，运行时覆盖差异也会放大成不同的探索轨迹。当前没有每条新语料的自动重复验证，且只有单 seed、单次运行，不能将最终约 1%–6% 的差异全部归因于反馈模式。

下一轮宜先缩短必要资源前缀、增加模板/状态构造组合，并检查新增状态是否独立保留且能产生后代收益；再对 edge-only/语义-only 语料的预算和 PC 基线做受控对照，检查覆盖稳定性，最后以配对多 seed 比较同时间和同 syscall 预算的 ext4/JBD2 增长。自动逐语料 deflake、特征去重/代表种子缩减、按后代收益分配的变异能量仍属于未完成的增强，不能用已有内容哈希去重或调用数伸缩代替。

## 当前状态

实现位于 `tests/syz-runtime.h`、`scripts/kcov_feedback.py`、`scripts/campaign_runner.py` 和 `scripts/run-feedback-study.py`。四批工作的当前实现和实测状态如下。

| 批次 | 实现 | 验证与边界 |
| --- | --- | --- |
| 1. 选择与归因 | 当前父种子按实际成功的 `Mutate` 推进；支持均匀轮转和有界公平的稀有度调度。PC/edge/语义收益按程序记录；edge 只在单个 syscall 内生成；语义出口从 lifecycle `boundaries` 关联到相同 program/call_index。mismatch/unverified 保留为诊断。 | 四组旧长跑均完成 native syscall 核验；修复后短 VM 和真实样本重放证明 operation exit 可用、语义可独立准入。全事件来源归因仍不完整，后台事件不能视作当前程序的奖励。 |
| 2. 重放与稳定基线 | 起始 regular/bigalloc/inline 镜像按 seed 首次生成后加锁保存并复用；campaign 记录各镜像、rootfs 和 kernel SHA256。`run-feedback-study.py replay` 固定 seed 重跑相同 Generate 程序，比较程序 hash、起始镜像、运行环境和 PC/语义特征交集。corpus 仍按程序内容 SHA256 去重。 | 先前 replay 报告与 campaign 已按要求删除；同起点重放工具和离线测试保留，新的运行基线待重跑。 |
| 3. 输入与反馈消融 | `--resource-valid-every` 控制资源有效输入；`--feedback-mode` 可选 `pc`、`pc_edge`、`pc_semantic`、`pc_edge_semantic` 或 `none`，用来控制 corpus 准入信号。study 驱动器支持配对 seed、预算和重复配置。 | 四种旧反馈配置各 3 小时已完成；本轮 PC-only 最终覆盖最高。修复后的短 VM 已验证语义出口和语义独立准入；长时间四组对照需用修复后的代码重跑。输入策略本轮固定，未重跑资源准备对照。 |
| 4. 稀有度与动态预算 | `--scheduler-policy rarity_fair` 根据父程序持有特征在本轮执行中的频率求逆频率分数，以确定性加权抽样选种子；连续未选次数达到上限时强制选择最久未选者。`--mutation-budget-policy dynamic` 根据分数按对数提高调用数，最多为基础预算 2 倍且受 `--max-mutation-calls` 限制。 | 先前 VM campaign 已按要求删除；离线测试仍覆盖稀有父程序偏好、确定性和有界无饥饿。动态策略长期覆盖增益尚待后续长跑验证。 |

重跑同起点稳定性基线：

```sh
python3 experiments/ext4-counter-trace/scripts/run-feedback-study.py replay --repetitions 2 --calls 16 --seed 20261023 --duration-seconds 300
```

运行资源与反馈消融矩阵：

```sh
python3 experiments/ext4-counter-trace/scripts/run-feedback-study.py ablation --repetitions 2 --segments 4 --calls 16 --seed 20261030 --duration-seconds 900 --batch-timeout 30
```

四种反馈配置并行长跑，每种默认采集 12 小时。矩阵默认使用 `rarity_fair` 父程序调度和固定调用预算；这是完整反馈策略的对比，调度稀有度使用各组实际启用的特征。如果要单独比较 corpus 准入信号、排除稀有度调度的影响，可加 `--scheduler-policy uniform_round_robin`。动态预算单独对照时再设置 `--mutation-budget-policy dynamic`。每个配置的日志和 `summary.tsv` 保存在独立的 `results/feedback-long-*` 目录：

```sh
nohup bash experiments/ext4-counter-trace/scripts/run-feedback-long-matrix.sh --duration-seconds 21600 --seed 20261024 > "experiments/ext4-counter-trace/results/feedback-long-launch-$(date +%Y%m%d-%H%M%S).log" 2>&1 < /dev/null &
```

四组为 PC-only、PC+edge、PC+语义、PC+edge+语义；共享 seed、resource-valid/fresh-generate 输入周期、`rarity_fair` 调度和固定变异预算，使用独立 VM、corpus、结果目录和 SSH 端口 11883–11886。VM 启动间隔默认 60 秒，以错开 syzkaller 编译峰值；每组在自己的 VM 准备完成后起算 21,600 秒，checkpoint、关机、fsck 和最终报告时间另计。机器至少需要 4 个 CPU、6 GiB `MemAvailable`，并在结果盘预留四组各 8 GiB 的逻辑配额和 16 GiB 全局空闲空间（启动门槛合计 48 GiB）；运行中每组继续监视全局空闲空间并在自己的 8 GiB 配额附近优雅收尾。当前检查到 12 CPU、约 8 GiB `MemAvailable` 和约 79 GiB 可用磁盘，满足门槛；四台 TCG VM 并行会降低单组吞吐，因此相同六小时时长下的程序数可能少于顺序运行。

需要消除执行量差异时，可给矩阵增加 `--target-syscalls N`。该值会传给四个 campaign，矩阵只有在每组 `generated_syscalls` 和 `native_verified` 都达到 N、报告停止原因为 `syscall_budget` 且没有 KCOV 缺口时才标记通过；不指定时仍使用六小时 duration-only 口径。建议先用固定 syscall 预算做配对比较，再用 duration-only 长跑观察实际吞吐。

矩阵以严格 KCOV 模式启动每组。如果一个段发现缺失、非法或饱和样本，该组会完成段级记录后在边界请求停止、正常 checkpoint 和关机；不完整 KCOV 会令 campaign `passed=false`，矩阵汇总也将其标为失败。严格模式能保证有缺口的数据不会被汇报为成功，不能保证 fuzz 执行过程中永远不会产生饱和。矩阵成功还要求每组达到完整 21,600 秒、stop reason 为 `duration_limit`、所有段完整且 KCOV 零缺口。内存、磁盘和四个 SSH 端口会在启动 VM 前检查；同一矩阵进程通过锁文件避免端口重复。结果目录含每组日志及最终 `summary.tsv`。

2026-10-23 并行编排烟测使用 `--duration-seconds 60` 和 60 秒 VM 启动间隔，证据目录 `feedback-parallel-smoke-20261023` 已清理。四种模式分别绑定端口 11883–11886，均完成完整 segment（分别为 6、6、5、6 段）、正常 checkpoint/关机与磁盘检查，`passed=true`、`kcov_feedback_complete=true`、0 KCOV gap；汇总表四行均为 `validation=passed`。PC+语义组在 60 秒内完成的段较少，体现并行 TCG 吞吐差异；各组仍按各自 VM ready 后的实际时间达到 60 秒采集预算。这是并行运行和报告闸门的端到端短测，不是六小时覆盖收益结论。

启用稀有度调度和动态预算的 campaign 示例：

```sh
bash experiments/ext4-counter-trace/scripts/run-campaign.sh --scheduler-policy rarity_fair --mutation-budget-policy dynamic --max-mutation-calls 64 --resource-valid-every 2 --fresh-generate-every 0 --syzkaller-calls 16
```

父程序观察频率、稀有度分数和选择历史在 `feedback/scheduler.json`；每个程序的 seed、选择索引、预算、实际调用数和 parent hash 在其 `program.json`。完整 PC/edge/语义统计与各消融均值在 `feedback/coverage.json`、`ablation-report.json`。短测的新增边和 PC 统计不构成跨 seed 的统计显著性结论。

KCOV 每 syscall 缓冲默认 16,777,216 项；v5 只把每调用唯一 PC/edge 写入 spool，避免重复执行记录把跨调用累计样本推到旧上限。v5 flags 和 workload 摘要区分每调用满、累计上限、feature table 耗尽或 spool 写入问题。KCOV 饱和仍会被报告为反馈缺口，不会作为完整样本入语料；无法结合源码和调用范围消歧的旧样本记为 `legacy_or_unclassified_saturation`。离线反馈测试覆盖公平调度、调用边界、调用级公式归因、内容去重及不可信样本拒绝；这些测试不能替代 VM 短测。

## 2026-10-23 输入/调度调整与端到端 VM 短测

长跑输入周期改为每 4 个程序槽生成一个资源有效程序，并在第 7 个程序槽强制普通 `Generate`；资源输入交替使用 namespace 与 8 调用 file-state 模板，其余槽位从 corpus 变异。启用 `rarity_fair` 后，根据已执行程序持有的稀有 PC/edge/语义特征选择变异父程序，并以有界未选间隔防止饥饿。本次长跑矩阵仍使用固定调用预算，避免把动态预算收益混入反馈模式对照。短测确认已产生真实 `Mutate` 子代和普通 `Generate` 输入。

修复后的端到端联合反馈 VM campaign `campaign-tamhibig` 已清理。它以 128 个 native syscall、8 个 16-call 程序为预算，运行约 6 分钟（含准备、关机和汇总），通过 `rarity_fair` 与 `pc_edge_semantic` 反馈执行。最终报告显示 `passed=true`、8/8 segment 完整、128/128 syscall 核验、counter validation 通过且无 issues；KCOV 反馈 8/8 完整样本、0 饱和。最终覆盖为 7,598 个内核 PC（其中 ext4/JBD2 2,716）、8,992 条 syscall 内 edge、83 个语义特征，内容去重 corpus 有 8 个程序。两种资源模板均实际执行；8 段包含 2 个 resource-valid、5 个 Mutate 和 1 个 fresh Generate。最后一个 fresh Generate 仍增加 225 个 PC 和 299 条 edge，因此这次短测没有显示覆盖已饱和。

这次自然联合运行中的 83 个新语义特征都分类为 `consistent`，但首段同时带来 6,367 个新 PC 和 7,323 条新 edge，故没有语义-only corpus 准入样本；这些特征只关联到 9 个可归因 syscall，program 级 `attribution_complete` 为 false。语义独立准入仍由此前真实样本的离线重放审计单独证明；本次运行不证明语义反馈自然提高覆盖。Campaign 的全局 `source_attribution_complete` 也为 false，因为 trace 还包含未归属到 workload 的后台事件；反馈只把可明确关联到 generated syscall 的事件用于语义特征。128/128 native syscall 的执行核验和 8/8 完整 KCOV 样本不代表所有 trace 事件都已归因，这几项证据需要分别看待。

第一轮短测执行时发现顶层 `campaign.json` 的 KCOV 摘要在 segment 的反馈 augment 前保存，最后停在 6/8；最终权威数据在 `feedback/coverage.json` 和 campaign 报告中为 8/8。runner 现于 augment 后同步并保存最终摘要，并新增持久化回归测试。之后修复版 runner 的同规模确认 `campaign-32dyqakh` 已清理：`campaign.json` 与 `feedback/coverage.json` 均为 8/8、0 饱和、7,853 个 PC、9,312 条 edge、2,840 个 ext4/JBD2 PC、corpus 8，campaign `passed=true`、128/128 syscall 核验和 `kcov_feedback_complete=true`。它同样执行了 2 个 resource-valid、5 个 Mutate 和 1 个 fresh Generate，并完成 VM 关机与磁盘检查。当前相关回归为 feedback 24 项、campaign 43 项、lifecycle 24 项、syzkaller 10 项，均通过。

本短测足以验证资源模板生成、父程序选择/变异、native 调用核验、KCOV 采样、PC/edge/语义反馈、corpus 入库和 VM 收尾链路，可以开始长时间对照。它只说明短时间闭环工作并仍有新代码特征，不代表任何一种反馈模式的长期覆盖优势已经建立；长跑按矩阵结果再比较 ext4/JBD2 PC、edge、语义特征和 corpus 的稳定增长。

四批闭环代码及离线测试保留。2026-10-23 早先完成的四组各 3 小时产物保留；它们运行于语义出口修复前，仍只能作为旧版本对照，旧矩阵中 PC-only 覆盖最高不构成修复后版本的结论。当前端到端短测证明联合反馈链路完整且代码反馈仍在增长；语义自然准入和长期反馈模式排序需要后续同起点/配对长跑回答。该单 VM 闭环仍不包含多 VM syzkaller manager。

## 2026-10-23 并行长跑中止与累计上限审计

16:42 启动的六小时四组矩阵 `feedback-long-20261023-164223` 仍保留。PC+语义组于 21:02:50 因 `aggregate_pc_limit` 请求严格停止，21:10:31 汇总为 `passed=false`。按用户要求，21:22:20 向矩阵发送 SIGTERM，停止其余三组；全部 VM 退出后，宿主端于 21:29 完成报告并退出。原始产物已按当前保留清单保留。人工中止的最后一个不完整段已从比较中排除；三个中止组没有正常 checkpoint/fsck，不能判成完整合格六小时结果。

失败段 `campaign-hs5zsfyy/segments/segment-001549` 保留。KCOV 保存 16,777,216 个 PC 项，flags=21，明确为程序累计上限而非单调用 buffer 满。call 10 的 `fallocate` 产生 11,727,307 个记录、仅 2,210 个不同 PC；`mb_find_buddy` 中同一 PC 重复超过 125 万次。call 11 追加时撞到总上限，后续调用虽执行但 PC 未保存。失败组 native syscall 核验、计数检查和三盘 fsck/磁盘检查通过；其失败项为 KCOV 不完整。之前流式 spool 消除了堆扩容路径，但没有消除仍然保留的累计硬上限；短测通过不能证明长跑不会触及该边界。

按共同有效前缀 1,499 个程序、23,984 次 syscall 比较：

| 模式 | 全内核 PC | ext4/JBD2 PC | 语义特征 | 语义必须参与才入库 |
| --- | ---: | ---: | ---: | ---: |
| PC-only | 11,965 | 4,202 | 115 | 0 |
| PC+edge | 11,773 | 4,003 | 120 | 0 |
| PC+语义 | 12,025 | 4,171 | 118 | 0 |
| PC+edge+语义 | 11,678 | 4,032 | 149 | 0 |

这里的目标 PC 沿用现有函数名前缀统计口径。当前自然运行没有出现语义必须参与才能入库的样本，虽已观察到出口分类全部为 `consistent`，仍不能据此声称丰富状态反馈已自然带来独立探索收益。四组全部 corpus 父程序都有至少一次选择，没有复现首种子独占；加入 edge 后 corpus 更大、父选择中位数更低。等调用预算下联合反馈目标 PC 比 PC-only 少约 4.05%，但只有单 seed、部分运行且未逐语料 deflake，不构成普遍优劣的因果结论。

审计还发现三个编排/持久化问题：旧矩阵按 PID 顺序 `wait`，未及时汇总后退出的失败组；旧 SIGTERM 走异常中断路径而非段边界正常收尾；旧 `coverage.json` 的逐程序 `admitted` 未同步 augment 后结果。现已修复：矩阵使用 `wait -n -p` 并以稳定配置顺序写入四行 summary，同时记录实际完成顺序；首次 SIGTERM 请求当前段结束后 checkpoint、fsck 和报告，第二次信号或超时才强制结束；augment 后回写 `coverage.json`、`manifest.json` 和 joint feedback 的准入字段。审计仍使用真实 `joint_feedback.admitted_repro_programs`、程序 SHA256 和 corpus 文件交叉核验，避免旧结果字段误读。原 `summary.tsv` 在中断时只有表头，另存 `feedback-long-20261023-164223/stopped-summary.tsv`，没有覆盖原始证据。

后续重跑仍应使用本次大样本及多个调用合计超过旧上限的压力用例验证紧凑表示，检查真实调用内 edge 没有改变；随后再验证自然语义独立准入及后代覆盖收益。本次停止审计没有修改旧运行结果或启动新的长跑。完整分析、可复算脚本、逐段曲线和饱和诊断见 `feedback-long-20261023-164223/STOPPED-ANALYSIS.md`。

上述重跑前修复现已完成并通过离线回归。KCOV v4 的文件格式为：header `<magic, version=4, token_count, flags>`，随后是 `raw_pc_count`、`token_count` 个 `<pc, run>` token，以及调用范围表。连续重复 PC 压缩为一个 token；host 解码每个 run 为一个 PC，run 大于 1 时保留一次 self-edge，run 大于 2 不重复展开，因为额外重复不会增加 PC 或 edge 特征。跨 token 的相邻边仍按原始顺序计算。`token_count` 仍有 16,777,216 上限，`raw_pc_count` 可以超过该值；flags bit 128 表示 compact，bit 8/16/64 继续分别表示单调用满、累计 token 上限和 spool I/O 失败。v1-v3 样本继续兼容。

修复后 2026-10-23 的单 VM 短测 `campaign-3wzl566q` 已清理。它使用 1 个 resource-valid 程序、16 个 native syscall，结果 `passed=true`、KCOV 1/1 完整、0 饱和、6,435 个 PC、2,331 个 ext4/JBD2 PC、7,369 条 syscall 内 edge、81 个语义特征。样本 header 为 v4，raw PC 数 84,158，token 数 71,184；`coverage.json` 的 `admitted=true`、`admission_signals=[pc, edge, semantic]` 与 `feedback.json` 的实际 corpus 路径一致，三种磁盘均完成 fsck 和最终快照校验。一次 4 段尝试的第二段因输入 workload 超过 30 秒 batch timeout 而被严格拒绝，这属于输入超时证据；它没有产生 v4 解析错误或饱和，后续长跑应使用足够的 batch timeout 并单独记录超时段。

随后为真实长跑中的交替重复序列增加 v5 feature-set 格式：header 为 `<magic, version=5, feature_count, flags>`，随后是 `raw_pc_count`、`feature_count` 个 `(from, to)` 记录和调用范围表。`to=0` 的记录是 PC，非零 `to` 的记录是同一 syscall 内 edge；唯一表按 syscall 清空，避免多个调用合计的重复 PC 重新触发累计 token 上限。host 的 `read_sample_features()` 保留每个调用的 PC/edge 集合，`collect()` 使用显式 edge 集合，因而不会跨调用合成边。bit 256 表示 feature-set，bit 512 表示 `feature_table_limit`；唯一表耗尽的样本严格记为 saturated，不会进入 corpus。`raw_pc_count` 仍记录 KCOV 原始执行次数，`feature_count` 记录保存的唯一 PC/edge 记录数，两者不能混为覆盖数量。

离线压力测试用 200,000 个交替 PC 验证 v5 只保存 4 条记录（两个 PC、两条 edge），仍保留 `raw_count=200000`；调用范围测试确认不会跨 syscall 产生 edge，表耗尽测试确认 `feature_table_limit` 被单独诊断，重复记录和 raw-count 不一致也会被拒绝，单 PC 自边和跨 syscall 的合法重复都能正确解析。相关回归为 KCOV feedback 34 项、runtime/syzkaller 13 项、campaign 45 项、feedback matrix 5 项、lifecycle 24 项。

启用 v5 后的真实 VM 短测 `campaign-m8g1j4ar` 已清理。单个 resource-valid 程序的 16/16 native syscall 全部核验，KCOV 1/1 完整且 0 饱和；样本为 `version=5`、`flags=260`，`raw_count=84637`，保存 40,440 条每调用唯一记录（19,436 个 PC 记录、21,004 个 edge 记录；不同调用之间重复记录合法），host 全局去重后为 6,384 个内核 PC、2,348 个 ext4/JBD2 PC 和 7,314 条 syscall 内 edge，语义特征 81 个。campaign `passed=true`、`kcov_feedback_complete=true`，checkpoint、分析、三种磁盘 fsck 和关机均通过。该短测验证了 v5 的 guest 写入、host 解码、准入持久化和 VM 收尾；它仍不替代多 seed 长期覆盖对照。

## 2026-10-23 edge-only 父程序预算修复

旧三小时对照还显示了一个会直接降低代码覆盖的调度效应：PC+edge 有 278 个 corpus 父程序，其中 142 个只因为新 edge 入库；这些 edge-only 父程序被选择 237 次，只产生 88 个子代新 PC（每次 0.37 个），而带 PC 新颖性的父程序被选择 368 次，产生 1,746 个子代新 PC（每次 4.74 个）。联合反馈中 edge-only 父程序为 206 个，477 次选择产生 586 个子代新 PC；PC 父程序 486 次选择产生 2,309 个。旧矩阵的均匀轮转把两类父程序等量对待，导致 edge-only corpus 扩张后稀释了有效变异预算。

`FeedbackCorpus` 现在把 edge 统计和长期父程序准入分开：edge-only 父程序最多占 PC-bearing 父程序的 25%，在尚无 PC 父程序时保留 8 个最小探索槽。超过配额的样本仍更新全局 edge/PC 覆盖并在逐程序反馈中记录 `admission_suppressed=edge_only_quota`，但不会加入 active corpus，因此不会被后续变异调度反复抽取。带 PC 信号或 semantic-only 信号的准入不受该配额影响；同一程序内容再次出现也不会重复占用父程序槽位。`feedback/coverage.json` 和 `feedback/scheduler.json` 现在记录 `admission_parent_counts`、`edge_only_parent_limit` 与 `edge_only_admissions_suppressed`，可在长跑中验证调度池是否按预期受控。新增配额回归测试和既有 KCOV、campaign、syzkaller、矩阵测试均通过。

针对未完成六小时矩阵中 edge-only 选择仍可能在早期 active corpus 中占比过高的问题，又增加了实际变异选择份额限制：存在普通父代时 edge-only 的选择份额最多为 25%，没有普通父代时不阻断探索；`selection_parent_counts` 和 `edge_only_selection_share` 会写入覆盖与 scheduler 摘要。这样长跑可以区分“edge 被观察到”与“edge 父代消耗了多少变异预算”。

六小时停止数据中的 `aggregate_pc_limit` 来自旧 v3 运行时把重复 PC 记录累计到 16,777,216 项，并不是覆盖点数量耗尽。当前默认 v5 feature-set 按 syscall 保存唯一 PC/edge，保留原始计数并用 `feature_table_limit` 单独诊断唯一表耗尽；新的 VM 短测已验证 128/128 syscall、0 KCOV 饱和。旧六小时失败段不能与修复后的长跑结果混合比较。

## 2026-10-24 全内核 PC 与 ext4/JBD2 PC 调度修复

最新三小时矩阵 `feedback-long-20261023-223036` 的有效结果显示，PC-only 完成 11,328 个 syscall、全内核 PC 11,731、ext4/JBD2 PC 4,005；PC+edge 完成 11,008 个 syscall、全内核 PC 11,620、ext4/JBD2 PC 4,053；PC+语义完成 11,424 个 syscall、全内核 PC 11,606、ext4/JBD2 PC 3,983；联合反馈完成 10,944 个 syscall、全内核 PC 11,514、ext4/JBD2 PC 4,052。四组均无缺失、非法或饱和样本，但执行量不同，比较时应使用相同 syscall 前缀。联合反馈在共同 10,944 syscall 前缀上比 PC-only 少 0.94% 全内核 PC，说明 edge/semantic 反馈在旧调度下仍有预算和吞吐代价，不能只看最终绝对值。

逐段数据进一步表明覆盖在首段之后快速变稀：PC-only 最后 308 段只新增 429 个 PC，PC+edge 最后 288 段只新增 1,174 个 PC，PC+语义最后 314 段只新增 365 个 PC，联合反馈最后 284 段只新增 602 个 PC。旧 scheduler 中 PC-only 约 78/144 个父程序、PC+edge 120/177 个父程序、PC+语义 76/132 个父程序、联合反馈 107/159 个父程序已经有过零新 PC 后代。原策略只按混合 feature rarity 选父程序，稀有 edge/semantic 数量可以抬高一个不再产出 PC 的父程序；同时没有显式区分 ext4 目标 PC 和其他内核 PC。

当前代码将这两个问题接入反馈闭环：

- 每个父程序的特征表增加 `ext4:<pc>`，并按 `pc`、`ext4`、`edge`、`sem` 分别计算稀有度。PC 权重为 1、ext4 权重为 2，edge/semantic 权重分别为 0.15/0.25；每类使用平均逆频率和对数 breadth，并设上限，避免大量 edge 记录压过 PC 目标。分量和总分写入 `parent_candidate`、`scheduler.json` 及程序的 `schedule_decision`。
- 每个 Mutate 子代记录 `new_pcs`、`ext4_new`、`new_edges` 和 `new_semantic_features`。父程序保存累计值、EMA、观测次数和 `zero_yield_streak`，并将 ext4 yield 以更高权重纳入选择权重。
- 连续三次没有任何新反馈的父程序从正常 rarity 池降温；每 16 次选择重新开放一次探索，且在所有父程序都降温时恢复全池，避免饥饿。edge-only 父程序仍受准入上限和最多 25% 实际选择份额限制。
- 动态变异预算对 rarity 和近期目标 yield 只做有界小幅加成，对连续零收益父程序减预算；固定预算模式不改变，因此反馈模式对照不会混入调用量变化。
- `campaign.json` 的 `verified_syscalls` 现在累计所有已完成 segment，不再只写最后一个 segment 的 16 个调用。

实现后的短 VM `campaign-qjlc32uu` 已清理。它使用明确的 `work/debian-clean-smalljournal.img`、`rarity_fair`、联合反馈和固定 16-call 程序，完成 4/4 segment、64/64 native syscall、KCOV 0 缺口/0 饱和、三盘 fsck 和最终磁盘校验，`passed=true`。最终覆盖为全内核 PC 7,449、ext4/JBD2 PC 2,758、edge 8,745、semantic 81；scheduler 记录 2 次父选择、`yield_aware_scheduler=true`、上述 rarity 权重和每个父程序的 ext4 yield。该短测证明接线、统计和 VM 收尾有效，不代表长期覆盖已经提升；下一次长跑应使用相同 syscall 预算比较四种模式，并检查 `feedback/scheduler.json` 的 yield、冷却和 edge-only 份额。

## 2026-10-24 三小时/未完成六小时复核与冷却修复

两轮旧矩阵的证据快照不包含本节之前刚加入的 ext4 稀有度、yield EMA 和零收益冷却：三小时快照的 `kcov_feedback.py` SHA 为 `15f64666…`，六小时快照为 `465f84d5…`；当前工作树才包含这些调度字段。因此旧结果可以证明低收益 edge-only 父代和吞吐差异的机制风险，不能证明当前调度器已经改善长期覆盖。

按共同 syscall 前缀复核三小时结果（684 段、10,944 次 syscall）：PC-only 为 11,623/3,996 个全内核/ext4 PC；PC+edge 为 11,620/4,053（-0.03%/+1.43%）；PC+语义为 11,584/3,975（-0.34%/-0.53%）；联合为 11,514/4,052（-0.94%/+1.40%）。所以 PC+edge 和联合的 ext4/JBD2 PC 并没有低于 PC-only；语义组两项略低。共同前缀末 171 段中 PC+edge 和联合分别新增 497/163、347/91 个全内核/ext4 PC，说明它们后期仍在追赶，差异受早期语料分叉和吞吐共同影响。

未完成六小时的共同前缀为 1,499 段、23,984 次 syscall：PC-only 11,965/4,202；PC+edge 11,773/4,003；PC+语义 12,025/4,171；联合 11,678/4,032。PC+语义因旧版 `aggregate_pc_limit` 触发 `kcov_feedback_gap`，其他组随后人工停止，不能当作完整六小时对照。该饱和是重复 PC 记录累计上限，当前 v5 feature-set 按调用保存唯一 PC/edge，并用独立 `feature_table_limit` 标记唯一表耗尽；两类问题不能混为覆盖率平台期。

本次复核还发现当前冷却定义存在目标错配：`_record_child_yield()` 原来只要新增 edge 或语义特征就清零 `zero_yield_streak`，而父代收益分数只使用 PC 与 ext4 PC。于是没有新增目标 PC 的父代可能逃过冷却。现在冷却只在 `new_pc > 0` 或 `ext4_new > 0` 时清零；edge/语义仍分别保留 EMA，并记录 `zero_any_signal_streak` 作为诊断。候选、程序级 scheduler decision 和 `feedback/scheduler.json` 暴露 `zero_target_yield_streak`、`zero_any_signal_streak`，旧的 `zero_yield_streak` 保留为目标 streak 兼容字段。ext4 PC 同时属于一般 PC 和 ext4 目标 yield，当前有效权重为 0.5+2.0=2.5；普通 PC 权重为 0.5，这是有意的目标偏置。

回归测试为 43 项 KCOV 测试全部通过，新增 edge/语义-only 连续收益仍会进入目标冷却、PC/ext4 收益会解除冷却的断言。当前代码短 VM `campaign-y_sn25ag` 已清理。它使用 `work/debian-clean-smalljournal.img`、联合反馈、`rarity_fair`、固定 16-call 程序完成 4/4 段、64/64 native syscall、KCOV 0 缺口/0 饱和，三盘 fsck 和关机检查通过，`passed=true`。scheduler 已写入 `cooldown_yield_basis=new_pc_or_ext4_pc`，两个 Mutate 父代均记录 PC/ext4 EMA。该短测证明修复已接入真实 VM；它不能替代同 seed、同 syscall 预算的四模式长跑。

历史三小时和六小时均没有 semantic-only 入库，因此无法从旧数据估计其长期预算风险。为防止长跑中语义出口突然产生大量独立特征，当前代码已落实受控探索配额：semantic-only 父代最多占 PC-bearing 父代的 25%，早期保留 8 个槽位，选择份额同样限制为 25%；超额样本仍计入语义/PC 全局统计，但不进入 active corpus，并记录 `semantic_only_quota`。最终代码的两段 PC+语义 VM `campaign-sonw7otv` 已清理；它完成 2/2 段、32/32 native syscall、KCOV 0 缺口/0 饱和，覆盖 6,463 个全内核 PC、2,348 个 ext4/JBD2 PC、7,423 条 syscall 内 edge 和 83 个语义特征，`passed=true`。因此当前长跑前的 edge-only 与 semantic-only 预算、目标冷却和饱和诊断均已接线。

## 2026-10-24 30,000 syscall 矩阵故障复核与修复

矩阵 `feedback-long-20261024-023033` 为四组配置了 30,000 个目标 syscall 和 43,200 秒上限，但没有一组完成目标。PC-only 为 21,232/30,000，PC+edge 为 18,592/30,000，PC+语义为 20,960/30,000，联合反馈为 19,936/30,000；已完成段的 generated syscall 和 native verified 数完全一致，逐段 `execution_issues=0`。PC+edge 在第 1,162 段触发严格 KCOV 停止；当时矩阵脚本随后把 SIGTERM 发给另外三组，并在 120 秒宽限后将仍在汇总报告的 runner 强制结束。因此那三组不是运行满 12 小时，也不是自然达到目标。本次已有的矩阵脚本改为单组失败后继续等待其他组，新增回归测试覆盖这个停止路径。

PC+edge 失败样本 `campaign-gox9yva5/segments/segment-001161` 包含 `fallocate(-1, mode=0, off=2, len=0x100000000)`。runtime 把无效 fd 修复到稳定普通文件后，原先未限制 4 GiB 长度；该调用实际记录 22,960,042 个 raw PC，KCOV 缓冲区最多保留 16,777,215 项，触发 `per_call_buffer_full`（flags=8），样本因此被拒绝，未进入 corpus。当前 runtime 在 fd 修复后把生成 `fallocate` 的正长度限制为默认 64 MiB，并记录原/执行长度及 adaptation 数；这直接封住已观察到的输入路径，不改变其他 syscall，也保留严格 KCOV 完整性门槛。故障矩阵的 evidence runtime 不含这个限制，不能用作新代码的验证。

沿相同运行的段数对齐时，PC+edge 在约 1,000 段达到 12,057 个内核 PC，PC-only 为 11,827；到约 1,160 段分别为 12,073 和 11,861。它说明 edge 组早期确实找到更多 PC，但之后增长很少；这次饱和发生在停止前，不是覆盖平台期的证据。PC+语义和联合反馈均没有 semantic-only 准入或父代选择，所以自然运行仍未证明语义信号独立贡献。由于这轮未完成且另外三组被中止，不能据此追加改变调度权重或比较模式优劣；改变反馈策略会混淆下一轮 30k 对照。

矩阵当前会逐项验证 `target_syscalls`、generated 数、native verified 数、`syscall_budget` stop reason、所有 segment 完整和 KCOV 无 gap；单组失败后继续等待其他配置，最终矩阵仍会以失败状态报告该组。低于 30,000 个 verified syscall 时不会给出通过结果。执行循环现在只有在 generated 和已经封存的 native verified 数都达到目标后才设置 `syscall_budget`，避免最后一个不完整调用把生成数误当成核验数。按本轮并行实测速率约每小时 3,300–3,700 个 syscall，30k 约需 8–9 小时；12 小时上限有时间余量，但主机中断、资源不足或新 KCOV gap 仍会使对应组失败，不能把预算估算当成通过保证。

本轮 edge/joint 的结果还显示 8 GiB 单 campaign 配额对 30k 只剩很小余量：按已用空间线性外推，两组分别接近 7.6–8.0 GiB。矩阵默认配额已提高到 9 GiB，并继续在启动前预留四组配额加 16 GiB 共享空闲空间；当前磁盘不足时会拒绝启动，而不是中途产生无证据的欠额结果。这个配额修复只解决已观测的输出空间边界，不能屏蔽宿主机 SIGTERM、OOM 或新的 KCOV 饱和。

本次执行循环修复后的 PC+edge VM 短测目标为 32 个已核验 syscall，最终 generated/native verified 都是 48；因为预算按完整 16-call 程序段收集，并要等段封存后才能完成 native 核验，实际可比目标多一个程序段。campaign `passed=true`、stop reason 为 `syscall_budget`、3/3 段完整、KCOV 无 gap/无饱和，regular/bigalloc/inline 三盘检查与 fsck 均通过。临时 campaign `campaign-wo39vyf0` 已在验证后删除，没有留在正式结果目录。

重跑命令：

```bash
nohup bash experiments/ext4-counter-trace/scripts/run-feedback-long-matrix.sh \
  --duration-seconds 43200 \
  --target-syscalls 30000 \
  --seed 20261026 \
  --max-run-mb 9216 \
  > "experiments/ext4-counter-trace/results/feedback-long-launch-$(date +%Y%m%d-%H%M%S).log" \
  2>&1 < /dev/null &
```

本次 `results/` 清理已删除上述历史矩阵、短测、audit 产物和旧 campaign 目录；本文件仍保留当时记录的统计摘要，历史 campaign 名称只用于定位结论，不再作为当前可用 artifact。
