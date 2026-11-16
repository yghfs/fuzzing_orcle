# 覆盖反馈不佳的原因与改进顺序

后续实现与验证见 [第一批修复记录](IMPLEMENTATION.md)。以下保留修复前的诊断与历史数值。

审计日期：2026-11-12（Asia/Shanghai）。本次阅读项目设计、当前实现、保留的原始运行结果，
并重新遍历十二小时两臂的逐段数据；没有启动新 campaign，也没有修改生成器、内核或反馈实现。

**当前最应优先处理的是资源模板锁定、edge 奖励范围和执行效率。现有材料不能证明联合反馈
机制本身无效，也不支持把问题主要归因于语义特征过多。**

项目目标是通过 syscall 序列发现 ext4 元数据计数、分配释放和错误回滚的不变量问题。
因此，改进应增加可验证的目标操作与公式阶段探索，并保留独立 oracle；仅增加全内核覆盖
或 checker 自身分支数量不能作为项目成功标准。

## 证据与核对方式

主要材料：

- [一小时三臂比较](../../results/target-comparison-long-20261109/comparison.json)。
- [十二小时比较](../../results/target-comparison-12h-20261109-014902/comparison.json)。
- PC-only：[campaign-9chz7m3j](../../results/campaign-9chz7m3j/campaign-report.json)。
- Full：[campaign-fyubc_lv](../../results/campaign-fyubc_lv/campaign-report.json)。
- [已有历史审计程序](../2026-11-11-target-coverage/audit_history.py)及两臂的
  `segments/*/coverage.json`、`syz-programs/*/program.json`、`feedback/scheduler.json`。

本次将现有审计程序重新运行到 `/tmp/coverage-audit-now.json`，样本数、调用数以及
raw/effect/semantic novelty 求和等六项一致性检查在两臂均通过。这只证明聚合自洽。
十二小时归档中的 `kcov_feedback.py`、`campaign_runner.py`、`syzgen/main.go`、
`syz-runtime.h` 和 `guest-campaign.sh` 与当前源码 SHA256 相同，下述相关源码定位适用于历史运行。

### 覆盖表现

| 实验及口径：ext4 + JBD2 目标 PC | PC-only | Full |
| --- | ---: | ---: |
| 一小时 admitted corpus | 1854 | 2068 |
| 一小时全部完整样本 raw 并集 | 2637 | 2630 |
| 十二小时全部完整样本 raw 并集 | 3819 | 3668 |
| 十二小时 effect / corpus | 3636 | 3256 |
| 十二小时完整样本数 | 1513 | 1397 |
| 十二小时语义特征数 | 141 | 116 |
| 对齐前 1397 样本：raw | 3704 | 3668 |
| 对齐前 1397 样本：effect / corpus | 3519 | 3256 |
| 对齐前 1397 样本：语义特征数 | 116 | 116 |

一小时 corpus 增益不能解释成总探索覆盖增益。十二小时 Full 最终 effect 少 10.45%，
raw 少 3.95%，同时样本少 7.67%；对齐样本数后 effect 仍少 7.47%，raw 只少 0.97%。
吞吐差异解释了一部分结果，有效输入差异仍需要处理。

这里只比较同一轮、同一构建中的绝对 PC 数，没有完整目标 PC 分母，不能称为代码覆盖百分比。
两个小时长度实验使用不同起始 ext4 镜像，不能直接拼接曲线；十二小时仅有一个 seed。

## 1. 资源模板轮换实际上会被最早的资源父本锁住

[campaign_runner.py](../../scripts/campaign_runner.py) 的 `program_input_plan()`（65–70 行）
原本交替安排 `namespace` 与 `file-state`，但 1069 行先从所有 resource parents 中取父本，
1078 行再用父本模板覆盖计划模板。第一种模板一旦有父本，另一种模板就可能永远不再播种。

| 十二小时 resource 输入 | PC-only | Full |
| --- | ---: | ---: |
| 次数 | 324 | 300 |
| namespace / file-state | 322 / 2 | 0 / 300 |
| 对 effect 新目标 PC 的顺序归因贡献 | 994 | 5 |
| 对新语义特征的顺序归因贡献 | 56 | 0 |

Full 第 4 段的 file-state seed 入库后，第 8 段本应轮到 namespace，却变成 file-state
的 `ResourceMutate`；后面继续继承。PC-only 则在 namespace seed 入库后锁定 namespace。
可直接查 Full [第 8 段元数据](../../results/campaign-fyubc_lv/segments/segment-000008/syz-programs/b1-w0/program.json)。

这也对应明显的操作差异：Full 的 `linkat` 成功 1/1387、`renameat2` 成功 58/705；
PC-only 分别为 303/1734、1212/2954。目标成功且未改写调用占全部已分类调用的比例为
19.955% 与 31.545%。这里的成功率是输入诊断，失败后的真实 ext4 回滚路径仍有探索价值。

不能将锁定起因全部归给 edge：两臂第 4 段 `.syz` 内容相同，但 Full 当时有 3 个新
effect PC，PC-only 为 0，因此初始状态和覆盖顺序的微小分叉也会被该机制放大。
模板贡献是首次发现的顺序归因，不能当作模板本身的因果收益。

**建议：按模板选父本，为每个模板保留独立 seed 刷新份额。** 某模板没有可用父本时，
执行该模板 seed；记录计划模板和执行模板。资源池全部冷却时，应回到该模板的新 seed，
而不是继续消耗已耗尽的父本。当前 `kcov_feedback.py` 740 行的 `active or indices`
会在资源池全部冷却后回退到原池；Full 两个资源 edge-only 父本分别执行 70/73 个子代，
新目标 PC 仅 0/2。

## 2. edge 奖励与 ext4/JBD2 目标不一致

[kcov_feedback.py](../../scripts/kcov_feedback.py) 1453–1463 行在
`target_scope=ext4_jbd2` 时将 PC novelty 限于目标，edge novelty 却仍用全内核
`effect_fresh_edges`；1490–1493 行的 edge rarity 也使用全内核 edge。
因此，目标 syscall 中的 VFS 或通用参数错误分支仍可以影响目标反馈准入与调度。

十二小时 Full 的 15 个纯 edge-only 父本入库时，`effect_new_target_edges` 全部为 0，
它们凭全内核 effect edge 入库，后来获得 296/1196 次父本选择，即 24.75%。
一小时 Full 的纯 edge-only 父本也有同样情况。

但这些 edge-only 父本的后代合计发现过 426 个新目标 PC，不能认定边反馈没有价值。
现有实现也已经有辅助父本数量限制、25% 选择份额以及 yield cooldown，不能将
“增加上限或冷却”重复列为尚未实现的修复。

**建议：首先统一 target edge 的准入、频率和 rarity 口径，再单独比较探索份额。**
全内核 edge 继续作为诊断信息保留；若需要用于探索，应明确设为独立、受限的辅助策略。
当前实现从同一 syscall 的 PC 序列构造相邻对，它是执行转移信号，不能直接当成精确 CFG 边。

## 3. 当前语义信号有界，但对不同文件系统状态区分不够

[kcov_feedback.py](../../scripts/kcov_feedback.py) 1316–1326 行的语义 key 为：

```text
formula | point | status | value_kind | delta_bucket | syscall_outcome | operation_outcome
```

它不包含 inode、设备号、随机 ID 或绝对 counter，因此没有证据支持“高基数特征爆炸”。
Full 只有 1 个最终 semantic-only 父本（最初准入信号实际为 edge + semantic），
执行 15 个子代且没有新信号；它不是这次调度预算消耗的主要来源。
Full 的语义特征在约 4.66 小时后停在 116；等样本比较两臂恰好同为 116。

现有 key 主要区分公式点和判定结果。同一条正常分配路径在不同空间压力、extent 深度、
目录规模下，可能仍得到相同 key。因此，加入语义信号未必能引导进入新的计数边界状态。

**建议：在独立 oracle 不变的前提下，增加小规模、有界、可验证的状态转移特征。**
例如：

- 分配/释放量分桶，以及空闲 cluster/inode 的低空间区间；
- 目录从小目录到索引目录、extent 层级变化、共享 xattr 引用变化；
- 操作进入、更新、回滚、退出等阶段中的公式状态转移。

状态来源和因果关联必须可验证。不要把绝对 inode/事务号作为特征，不把 `unverified`
当成新的有效状态，也不为追求新颖度改写被检查 counter。异步 journal/writeback 的事件需有
单独的事务关联证据：当前运行时使用普通 `KCOV_ENABLE/KCOV_TRACE_PC`，语义准入要求
`same_task_native_syscall`，无法据此宣称完整覆盖后台 JBD2 线程。

## 4. 执行流程过重，优化 Python 反馈计算解决不了主瓶颈

| 十二小时性能，按 collection wall time | PC-only | Full |
| --- | ---: | ---: |
| 已核验 syscall/s | 0.559 | 0.517 |
| 平均秒/程序 | 28.56 | 30.93 |
| guest execution 中位数，秒 | 20.38 | 22.66 |
| analysis 中位数，秒 | 2.38 | 2.50 |
| feedback 中位数，秒 | 0.048 | 0.045 |

每个程序约 16 次调用，guest execution 占总采集时间约 72–73%，feedback 不足 0.2%。
`syzkaller_calls=16` 是每程序调用预算；custom allowlist 实际为 21 项，不能混为一谈。

[guest-campaign.sh](../../tests/guest-campaign.sh) 每段执行前后都对三个文件系统做稳定快照；
[campaign_runner.py](../../scripts/campaign_runner.py) 串行完成前段分析后才收集下一段；
[syz-runtime.h](../../tests/syz-runtime.h) 363 行对每个唯一 PC/edge 做一次 16-byte `write()`。
性能报告的 guest 阶段包含这些工作，尚未细分各项成本，不能把约 20 秒全归因于快照或某次写入。

**建议按风险和验证成本推进：** 先给 guest 阶段补细分计时，再尝试 feature 批量缓冲写、
常驻执行器和程序批量传输。完整 trace/关系报告可异步处理，但影响准入的轻量反馈必须及时到达。
运行时增量 oracle 应持续启用；稳定总量检查继续使用合法边界，不能用并发中间值替代稳定快照。

## 5. 短输入与固定初始对象限制深状态；corpus 缺少稳定性筛选

[syz-launcher.c](../../tests/syz-launcher.c) 为每程序建立私有小文件树，结束后清理。
底层镜像持续使用不代表目录和文件状态持续演化。短序列不容易构造大目录、深 extent、
空间压力以及有意义的共享对象生命周期。

[syzgen/main.go](../../syzgen/main.go) 已使用 typed `Prog.Mutate`，但
`BuildChoiceTable(nil, usable)` 不从 corpus 学习调用关系，变异也没有传入 corpus splice 输入。
应先保证现有资源模板有效，再评估这些能力，避免同时改变多个因素。

适合本项目的扩展是可复现的状态构造前缀：目录生命周期、碎片化/extent、delalloc、
xattr 共享、near-ENOSPC 等；每类状态独立计量探索份额，并保留对象依赖关系。
错误路径探索需要保留，但 EBADF/EINVAL 等进入 ext4 之前的失败不应支配目标探索预算。

[kcov_feedback.py](../../scripts/kcov_feedback.py) 953–961 行直接按整程序 SHA 去重并保留父本，
未在准入前确认新信号的重复稳定性，也未按目标特征最小化输入。建议只对准入候选做有限复跑，
在一致初始状态下确认信号，再进行保留资源构造依赖的最小化。corpus 应绑定可恢复镜像或
可重放构造前缀、epoch 和配置，不能只保存 `.syz` 内容 hash；复跑与最小化在独立恢复镜像上进行。

## 6. 测量与公式链完整性还要修正

两臂 KCOV invalid/missing/saturated 均为 0，不存在证据表明本次低覆盖主要来自 KCOV 丢采。
生命周期失败则为 PC-only 7 段、Full 3 段，全部紧随 fault/reset；旧 carry epoch 与新
mount epoch 的连续性、设备身份检查失败。它们使整轮 counter validation 未通过，
但不能据此否定完整 KCOV 样本或认定 ext4 缺陷。

另一个测量问题是目标前缀把项目 helper 也算进 ext4。按 `initial/kallsyms.txt` 将每段
`feature_ids` 中 `target:` PC 并集映射回符号，PC-only 的 3636 个 effect PC 中有 123 个，
Full 的 3256 个中有 100 个属于 `ext4_ct_` / `ext4_checker_` / `ext4_fault_` 前缀。
仅剔除这些命名 helper 后是 3513/3156，趋势不变。这只是部分净化，并非精确的上游源码覆盖。
建议将上游 ext4/JBD2、checker 和 fault 实现分开计量，并逐步按源码文件归属统计。

native 比较也不适合作因果排名：实际 syscall closure 为 151，custom 为 21；native
mount 重建镜像，custom 跨段保留镜像；native 缺少逐调用设备归因。十二小时 native
还有 seed 执行证据缺失及 207 次 executor failure，不能拿其 4004 PC 宣称优于或劣于自定义反馈。

## 建议的实施和验证顺序

1. **先修模板分池、独立 seed 份额和 target edge 口径。** 加定向验证：namespace 与
   file-state 都能持续被选择；只有通用内核新边时不能冒充目标新边；资源池冷却后能够重新播种。
2. **修 reset/carry epoch 的关联，并补 guest 阶段细分性能数据。** 随后优化 feature 写入、
   传输与执行器；保留完整证据模式，用相同程序检查优化前后 PC/edge、调用归因和公式判断一致。
3. **独立增加状态构造与有界阶段转移反馈。** 优先选择现有 oracle 能可靠检查的公式族，
   同时报告 verified 公式阶段、目标对象状态转移和候选复现情况。
4. **最后评价联合反馈收益。** 先用共同改进后的生成/执行链，对 PC、PC+目标 edge、
   PC+semantic、PC+目标 edge+semantic 做独立消融；保持模板预算、调度、镜像策略、调用集合、
   变异预算和内核完全一致。可先固定 `uniform_round_robin`，随后将 `rarity_fair` 作为独立因素。
   第一轮建议至少 5 个预先固定的 seed，同时给 wall-time 与
   等执行次数曲线，报告中位数及离散程度。先跑无注入主对照，再用独立、可记录的故障计划
   评价错误回滚探索；注入发现与自然发现分别统计。

主要指标应包括上游目标 PC/执行转移、成功未改写目标操作率、verified 公式阶段与状态转移、
有效程序吞吐和可复现的不变量候选。corpus 数量、公式命中次数和全内核总 PC 作为辅助诊断。

## 工具借鉴

可以先借鉴现有 pinned syzkaller 的成熟环节：[pkg/fuzzer/job.go](../../../../tools/syzkaller/pkg/fuzzer/job.go)
已有 `deflake()` 和 `prog.Minimize()`；[pkg/fuzzer/fuzzer.go](../../../../tools/syzkaller/pkg/fuzzer/fuzzer.go)
有 corpus 驱动的 choice-table 更新。它们分别对应稳定性确认、依赖保持的最小化和调用组合学习，
无需重新实现一套通用 fuzzing 引擎。集成时仍需保留本项目的目标设备归因和公式反馈。

[JANUS](https://github.com/sslab-gatech/janus) 的文件系统状态与 syscall 序列联合探索、
[Hydra](https://github.com/sslab-gatech/hydra) 的语义检查思路可作后续阅读方向。
本次访问其远端材料超时，因此不据此声称已核实版本或可直接复用其实现；具体建议以上述本地
源码和原始证据为依据。项目应继续围绕运行时 metadata oracle 发展，crash recovery
需要单独的实验目标与判定协议。
