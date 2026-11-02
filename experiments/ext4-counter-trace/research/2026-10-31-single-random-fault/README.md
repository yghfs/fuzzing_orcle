# 三组单 worker 随机错误注入 campaign 对比

分析日期：2026-10-31，时区 UTC+08:00。简称 `lhze`、`yk`、`dzq` 分别对应
`campaign-lhze43mu`、`campaign-yk3_8nzt`、`campaign-dzq91z1h`。

补充复核：[lhze 候选逐条分析](lhze-candidates/README.md) 已从原始 trace 恢复 segment 53
的 inode_evict_final_dirty 事件。因此下文基于原 manifest 的“19 已解析 + 1 unresolved”
现可补全为 **20 次全部解析，包含 6 次 eviction**；原始 report/manifest 未改写。
44 条候选进一步归并为一个 eviction 错误路径问题组的 6 次持久化残留实例及重复观测，
不能将其解释为 44 个漏洞。

## 主要结论

1. 这三组是三个配置/实现阶段的探索实验，不能当作只改变时长的 3h/6h/10h 对照。
   CPU 数、随机 seed、hook 集合、errno 合同、调度策略及插桩构建都发生过变化。
2. 最新版本的收集吞吐和直接故障归因已经改善，但生命周期验证仍受 reset/carry 和
   跨文件系统快照污染影响。实际 lifecycle `passed` 是 **53/459、79/517、211/1019**，
   并非报告里的 `complete_segments=52、259、536`。
3. 长时间运行带来更多 fault 样本和 hook/errno 组合，但新 hook 覆盖明显趋缓。
   yk 6h 与 dzq 10h 都只命中 50/87 个 hook，两组并集为 51 个。
4. `persistent_mismatch=0` 不能解释为没有持久化问题。lhze 的两个最终镜像有离线
   fsck 错误；yk 后续 64 次定点实验中 20 次有持久化后置条件异常。
5. 下一步优先修复观测链、保留 reset 前镜像并做定点复核。继续延长相同配置的运行，
   会继续增加候选，但不能自动补齐持久化证明或尚未进入的功能路径。

## 1. 实验配置和可比性

| 配置 | lhze | yk | dzq |
| --- | --- | --- | --- |
| 开始时间 | 10-25 22:01 | 10-29 16:54 | 10-30 23:53 |
| 设定收集时长 | 3h | 6h | 10h |
| 实际收集时间 | 10,807.261s | 21,603.514s | 36,000.212s |
| CPU | 2 | 1 | 1，显式 qemu64 |
| worker / batch | 1 / 1 | 1 / 1 | 1 / 1 |
| 每个生成程序 | 32 个 syscall | 32 个 syscall | 32 个 syscall |
| scenario | baseline | baseline | baseline |
| 随机 seed | 20261010 | 20261029 | 20261031 |
| fault hook | 3 个指定 hook | all，87 个 | all，87 个 |
| errno | 手工宽集合 | native | native，catalog 有小幅调整 |
| scheduler | rarity_fair | rarity_fair | uniform_round_robin |
| 每个 eligible hook 检查的注入概率 | 1% | 1% | 1% |

三组 `checkpoint/kernel.txt` 都显示 6.12.1，但 kernel image 和 patch 的哈希不同。
yk→dzq 的 patch 差异主要涉及 fault-injection PID/PGID 定位、相关 trace 字段和
`mb_buddy_load` native errno 集合。不能将其理解为 ext4 错误路径都已被修复。
同样，`all` 是启用全部 hook，不代表 workload 已执行全部 hook。

这里的 single 指单 worker。每个 segment 的程序仍然按生成/变异策略变化，三个
文件系统轮流承载 workload；分析同时读取三个文件系统的快照。它不是固定程序在
同一镜像上不加重置地重复 3/6/10 小时。

`segment_seconds=10` 也不是每个 segment 的完整墙钟成本。单 batch、32 个调用之外，
还有生成、编译、传输、六个 before/after 快照、清理、分析及故障后的 reset。

## 2. 总体结果：必须区分统计层级

| 指标 | lhze | yk | dzq |
| --- | ---: | ---: | ---: |
| segments | 459 | 517 | 1,019 |
| `complete_segments` | 52 | 259 | 536 |
| **实际 lifecycle passed** | **53** | **79** | **211** |
| 实际 lifecycle 通过率 | 11.55% | 15.28% | 20.71% |
| 已验证 syscall 返回 | 14,688 | 16,544 | 32,608 |
| syscall 成功率 | 50.18% | 46.55% | 47.71% |
| 报告 fault hits | 40 | 189 | 337 |
| 去除 `off` 后的 hit 记录 | 20，含 1 个 unresolved | 189 | 337 |
| operation 已解析的 hit | 19 | 189 | 337 |
| candidate 条目 | 44 | 117 | 184 |
| candidate 涉及的去重 segment | 22，另有 2 个 offline disk 条目 | 91 | 149 |
| unverified 条目 / 去重 segment | 21 / 21 | 156 / 156 | 298 / 298 |
| operation-exit mismatch episodes | 0 | 80 | 119 |
| persistent mismatch episodes | 0 | 0 | 0 |
| 完成的 filesystem resets | 0 | 189 | 336 |
| campaign raw coverage complete | true | true | false |
| KCOV complete / invalid / missing | 459 / 0 / 0 | 517 / 0 / 0 | 1019 / 0 / 0 |

三个最终报告均为 `candidate_review_required`，`passed=false`、
`counter_validation_passed=false`、`source_attribution_complete=false`。
这并不等于生成程序全部失败，也不能把 candidate 条目数当成漏洞数。

yk/dzq 会将直接命中 fault 的 segment 的部分生命周期/guest 错误转入
`fault_outcome_issues`，保留其收集完整性。因此 coverage-complete 中仍分别包含
**180、325** 个 lifecycle 分析失败的 segment；`259-180=79`、`536-325=211`。
lhze 的 segment 19 lifecycle 通过，但旧版 errno-choice 检查不通过，因而完整数
比 lifecycle passed 少 1。

candidate 的来源也不同：lhze 是 42 个 stable snapshot 条目 + 2 个 offline disk
条目；yk 是 75 个 workload_exit + 35 个 lifecycle + 7 个 stable snapshot；
dzq 是 128 个 workload_exit + 56 个 lifecycle。同一个 segment 可贡献多个来源。

### 2.1 “workload_exit”实际发生在哪里

所有 guest 日志中的 `WORKLOAD_END` 都是 `exit=0 signal=0`。但 batch 返回 90：

| launcher 清理错误 | yk | dzq |
| --- | ---: | ---: |
| `rmdir tree: Directory not empty` | 31 | 53 |
| `rmdir tree: Input/output error` | 17 | 25 |
| `unlink tree: Input/output error` | 22 | 49 |
| `rmdir/unlink tree: Read-only file system` | 5 | 0 |
| `rmdir tree: Structure needs cleaning` | 0 | 1 |
| 合计 | 75 | 128 |

这与 workload_exit 候选数完全一致。生成程序正常结束后，launcher 的
`remove_tree()` 失败并 `exit(90)`，导致 cleanup BEGIN 没有对应 END。
`source_attribution_issues` 中恰好分别有 75、128 个 unclosed cleanup phase。
因此不能将这些事件直接描述为 workload crash。

EIO/EROFS 可能是注入后 journal abort 等错误语义的后果；ENOTEMPTY 和 EUCLEAN
则值得优先保留现场并检查 namespace。它们都需要离线后置条件，不能仅凭清理失败
判定持久化损坏。代表例子：dzq segment 4，`rename_replaced_inode_dirty` 后出现
`rmdir tree: Directory not empty`。

## 3. 观测链的两个确定问题

### 3.1 每一次 reset 都被生命周期分析当成 invalid trace

| 项目 | yk | dzq |
| --- | ---: | ---: |
| 完成 reset 的 segment | 189 | 336 |
| 其中 lifecycle 带 `invalid_trace` | 189 | 336 |
| 不属于 reset segment 的 invalid-trace segment | 0 | 0 |

例如 dzq segment 3 的 regular raw trace 只有新 epoch `172415675830`，内部 seq 连续。
但 `analysis/regular-start.json` 被前一段 carry anchor 设为旧 epoch `62553917130`，
分析 trace 又直接拼接旧 epoch carry 和新 epoch raw trace。
于是出现 `sequence_missing_duplicate_or_wrong_epoch` 和 `device_identity_not_unique`。
这是 reset domain 边界没有贯穿到 lifecycle carry 构造的问题，不能视为 336 次
真实随机 trace 丢失，也不能直接当成 336 次文件系统 bug。

证据位置：`scripts/campaign_runner.py` 的 `finalize` 中先拼接 `self.carry`，随后
无条件沿用各 kind 的旧快照并重写 start identity；campaign 聚合层虽认识
`filesystem_reset_completed`，lifecycle interval/carry 层没有完成相同的断链。

### 3.2 dzq segment 910 的 raw 缺口确实存在

该条不能简单用 epoch reset 豁免：

- segment 909 regular：epoch `31880601795272`，seq 到 `6201`；
- segment 910 regular：仍是同一个 epoch，seq 从 `6212` 开始；
- **缺少 seq 6202..6211，共 10 个 counter event**；
- segment 910 重置的是 bigalloc，不是 regular；regular 的 reset 到 segment 912 才发生。

retained `evidence/tests/guest-campaign.sh::fault_reset_kind()` 会全局关闭
ext4 counter、fault 和 raw syscall 事件，然后执行全局 `sync`、目标 filesystem
卸载/mkfs/mount，最后再启用。其他 filesystem 在全局 sync 或后台活动中仍可能
推进自己的 counter 序号，却没有事件记录。这与缺口所在窗口吻合。

因此，事实是存在跨 segment 的序号缺口；由全局禁用事件导致是有代码和时间窗口
支持的高可信解释，但缺失事件的具体内容已无法从 raw trace 恢复。
ring-buffer 的 dropped/overrun 为 0 不能排除这种主动停采。
`raw_coverage_complete=false` 在这个边界有依据，不应只为让报告变绿而放宽校验。

### 3.3 非目标文件系统污染了“未命中”的 segment

yk 的 328 个未命中 segment 中 258 个 lifecycle failed；dzq 的 682 个未命中
segment 中 483 个 failed。**这两组所有未命中 segment 的目标 filesystem
before/after 快照均通过 valid、namespace_valid 和 relations_valid。**

但是，分别有 191、356 个未命中 segment 的其他 filesystem 快照不完整。
另外，即使当前六个快照均有效，reset 跨 epoch 的 carry 仍然可以让整个分析失败。
一个受注入影响的 filesystem 往往要等轮转回自己时才 reset；期间它也会被其他
kind 的 segment 读快照，所以一次故障可以拖累后续多个 segment。

这解释了为什么“只有一个 worker”，仍能看到 `no_valid_stable_confirmation`、
`single_writer_unverified_episode`、`concurrent_or_unhealthy`。单 worker 并不
保证三个 filesystem、journal、后台线程和跨 epoch carry 均形成可用稳定锚点。
也不能把这几个标签都解释成线程竞争。

## 4. 三组持久化证据分别支持什么

### lhze：最终镜像有实际损坏，且旧 hit 统计存在膨胀

旧 40 次 hit 中 20 次 operation 是 `off`。其余为 mballoc_mark 11、
inode_evict_final_dirty 5、unlink_inode_dirty 3，以及 unresolved 1。
已解析的 19 条也不能自动补成新版本的完整 syscall/errno attribution。

- regular：raw checker 与 fsck clean。
- bigalloc：fsck exit 4，inode bitmap `-31 -35 -38`，group 0 directory count
  为 9，实际 counted=7。
- inline：fsck exit 4，inode bitmap `-30 -35 -39`，directory count 为 8、实际 7；
  raw checker 还记录 inode 39 的目录引用数 1 与 stored nlink 0 不一致。

这组没有 fault 后自动 mkfs reset，损坏和不完整状态可以累积。早期 fault 后大量
后续 segment 继续在同一镜像上运行，不能将多个 stable snapshot 条目当成多个
独立根因，也不能仅凭最终镜像把损坏唯一归因到某次具体 hook。

### yk：长跑最终镜像 clean，但后续定点复核提供了更强证据

三个最终 raw/fsck 均 clean。然而 yk 共 reset 189 次，等于 fault hit 数。
最终镜像只覆盖最后一次 reset 之后的状态，不保留此前所有命中造成的磁盘现场。
所以它与 lhze 的最终镜像结果不能作为“代码修复后不再损坏”的直接对照。

已有 `CANDIDATE-REPRO-ANALYSIS.md` 和 `repro-yk3-20261030/` 的结果为：

| 定点实验判定 | 数量 |
| --- | ---: |
| injected_error_no_persistent_damage | 20 |
| injected_journal_abort_no_persistent_damage | 24 |
| injected_persistent_postcondition_violation | 18 |
| not_triggered，但 raw/flag 仍显示持久化异常 | 2 |
| 合计 | 64 |

`persistent_postcondition_mismatch=true` 总计 20。两个 not_triggered 是
orphan-file-delete-access-erofs 和 xattr-release-access-erofs：实际 hit_count=1，
但验证器对 source_defined 合同拒绝，不能隐去这项验证限制。

按后置条件可归并为 7 类，而不是 117 个漏洞：

| 类别 | 代表性磁盘后置条件 |
| --- | --- |
| inode eviction bitmap 未释放 | inode bitmap `-14`，最后一个 link 已删除 |
| orphan-file slot 残留 | orphan-file block slot 仍指向 inode 14 |
| 共享外部 xattr refcount | block 5184 refcount=2，实际应为 1 |
| rmdir 部分提交/回滚不完整 | 父/子目录 link count、`..` 或连接性不一致 |
| unlink 部分提交/回滚不完整 | 目录项已删除但 inode 仍有 link，unattached inode |
| rename 部分提交/回滚不完整 | source/target 引用和 nlink 不一致 |
| group extend bitmap 发布不完整 | superblock 容量增大，新范围 bitmap 未正确发布 |

其中 group extend 是补充定点实验，两个长跑都没有直接命中 resize_extend_buddy_load。
“7 类”是现有复核文档的归并口径，不是本轮重新完成的 7 个独立源码根因证明，
也不是无故障运行的发生率。allocator/extent/cache 和 mkdir 的定点组未显示
同类持久化损坏，说明很多随机 fault 后的错误和瞬时差异可以正确终止/恢复。

### dzq：更多有价值样本，仍欠缺 reset 前持久化闭环

regular、inline 最终 raw/fsck clean，且与最终稳定快照一致。
bigalloc 的 raw/fsck 也 clean，但 `matches_final_stable_snapshot=false`，
最终 snapshot 为 concurrent_or_unhealthy，并标记 fault_tainted。
337 次 hit 只有 336 次 reset，最后一个 bigalloc 故障现场未进入下一次同 kind reset。

这支持“最终保存的镜像未检查出 raw/fsck 错误”，不支持“337 次注入都没有造成过
持久化错误”。现有 candidate 184 条去重后是 149 个 segment，应再按 hook、errno、
namespace 后置条件以及离线诊断归并。

## 5. 运行更久到底增加了什么

### 5.1 用墙钟时间归一化

| 指标 | lhze | yk | dzq |
| --- | ---: | ---: | ---: |
| segments / hour | 152.90 | 86.15 | 101.90 |
| verified syscall / wall second | 1.3591 | 0.7658 | 0.9058 |
| 独立公式转换调用 / wall second | 0.2267 | 0.1209 | 0.1385 |
| filtered hit / hour | 6.66，含 unresolved | 31.49 | 33.70 |
| candidate 条目 / hour | 14.66，旧口径 | 19.50 | 18.40 |
| 转换调用 / 已验证 syscall | 16.68% | 15.79% | 15.29% |

yk→dzq 的 segment/syscall 墙钟吞吐提高 18.28%，转换调用吞吐提高 14.55%，
hit/hour 提高 7.00%，但 candidate/hour 下降 5.63%。每个 syscall 的转换密度
下降 3.15%。因此可以说跑得更快，但不能说每个程序更有效或发现漏洞效率已提高。
CPU 模型、调度器和 seed 同时变化，尚不能单独评价哪项修改贡献最大。

performance-report 中的 syscall/s 是按 `sum(segment_elapsed_seconds)` 计算，
分别为 1.6575、0.9077、1.0953；它与这里按 collection wall time 的吞吐不同。
一些分析时间跨段流水执行，stage total 也不能简单相加为端到端墙钟。

### 5.2 同一 campaign 内的覆盖增长

这里的 target PC = ext4 PC + jbd2 PC。历史字段名 `total_ext4_coverage` 实际
也对应这个 target 总和，不能误写为纯 ext4 PC。

| campaign / 时间 | segments | target PC | 不同 hook | hook/errno 组合 | candidate 条目 |
| --- | ---: | ---: | ---: | ---: | ---: |
| yk 1h | 93 | 4003 | 25 | 33 | 20 |
| yk 3h | 269 | 4315 | 43 | 70 | 60 |
| yk 6h | 517 | 4417 | 50 | 88 | 117 |
| dzq 1h | 109 | 3857 | 26 | 35 | 20 |
| dzq 3h | 308 | 4180 | 41 | 67 | 58 |
| dzq 6h | 606 | 4430 | 47 | 93 | 109 |
| dzq 8h | 815 | 4530 | 49 | 102 | 149 |
| dzq 9h | 916 | 4663 | 50 | 107 | 167 |
| dzq 10h | 1019 | 4663 | 50 | 109 | 184 |

小时边界采用 `measurement_start_wall_ns + h`，只纳入已经收集结束的 segment。
不是为制造相同样本量而重采样，也不是统计显著性检验。

dzq 6h 时 target PC 4430，yk 6h 为 4417，仅差 13（0.29%），尽管 dzq 已跑了
更多 segment。dzq 从 6h 延长到 10h，新增 413 个 segment、138 次 hit、75 个
candidate 条目，但只增加 233 target PC 和 3 个 hook。最后一小时 target PC
完全不增长，却继续增加 36 次 hit、17 个 candidate。这体现了新样本/错误组合仍
有收益，但路径和 hook 覆盖的边际收益下降。

最终 PC/corpus：

| 指标 | lhze | yk | dzq |
| --- | ---: | ---: | ---: |
| 纯 ext4 PC | 3715 | 4035 | 4282 |
| jbd2 PC | 346 | 382 | 381 |
| target PC | 4061 | 4417 | 4663 |
| corpus programs | 185 | 226 | 284 |
| semantic features | 158 | 334 | 344 |
| edges | 15365 | 16598 | 17512 |

这些是插桩构建内的 PC 集合规模，不是源码行覆盖百分比；不同 kernel hash 的
PC 数量可以作探索规模参考，不能当作精确的同地址集合覆盖对比。

## 6. 随机注入的偏置和盲区

yk/dzq 命中 50 个 hook，但共同 49 个：yk 独有 xattr_release_access，dzq 独有
mkdir_add_entry_access；并集 51/87，仍有 36 个未命中。
未命中的路径主要有 resize/group-add、directory rename/exchange、共享 xattr
释放的多个子阶段、EA inode、ACL、fileattr、inline conversion 和 migrate。

这更像 workload 结构没有充分进入路径，不能靠延长 baseline 单纯解决。
尤其 resize 和一些共享 xattr 生命周期需要特定 fixture；group extend 的既有
定点异常正是长跑没有直接命中的路径。

max_hits=1 且按 eligible hook 顺序做 1% 随机检查，会偏向较早、较常访问的 hook。
已经归因到 syscall 的 hit 中，yk 164/184（89.13%）、dzq 278/327（85.02%）
发生在 call_index 0..10，即 32 个调用的前 11 个位置。
这不是对 87 个 hook 的均匀抽样，也不是“每个 syscall 有 1% 概率被注入”。

直接故障归因方面：yk 184 verified + 5 hook_only，dzq 327 verified + 10 hook_only，
各约 97% 能对上 native envelope。helper errno 与最终 syscall raw return 不同
的分别有 85/184 和 139/327 次；这包含错误被转换或用户调用仍成功的情况，
不能把 helper 注入值直接抄成用户可见 errno，也不意味着这些归因全部无效。

yk 的 reviewed/experimental hit 为 8/181，dzq 为 27/310。大部分 hook 仍是
experimental admission，需结合 source-return 合同、真实 helper 边界和持久化
预期逐项复核，不能由名字含 dirty/access 就自动认定相同故障语义。

## 7. Oracle、覆盖和程序有效性

### 7.1 episode 的解释

transient episodes 是 114,698、86,561、181,585，不是这么多个 bug。
其 duration 中位数为 77µs、84µs、75µs；p95 为 937µs、5486µs、4107µs。
大多数是 descriptor/global/independent publication 顺序中的短暂差异。

yk 的 80 个 operation-exit mismatch 全在直接 fault-hit segment，包含
71 个 R-LINK、6 个 R-IDELTA、3 个 R-UDELTA。dzq 的 119 个同样都在 hit
segment，且全部为 R-LINK。这表明当前最强的运行时异常主要集中在 namespace
更新的错误出口，而不是未注入 workload 普遍产生操作出口差异。

不过，未命中 segment 的 lifecycle 验证并不完整，上述事实不能扩大为
“所有无故障执行都被证明正确”。persistent 分类需要后续可用稳定证据，而
invalid/unhealthy snapshot、reset 都会阻断该证明。

### 7.2 point local comparison

| 指标 | lhze | yk | dzq |
| --- | ---: | ---: | ---: |
| observed point/formula hits | 920,020 | 737,534 | 1,461,578 |
| point/formula pair 种类 | 116 | 123 | 125 |
| local consistent | 46.86% | 25.93% | 26.36% |
| local inconsistent，事件数 | 6 | 70 | 100 |
| local unverified | 1.58% | 6.70% | 6.10% |
| raw_only | 51.56% | 67.36% | 67.53% |
| incomplete_capture_hits | 0 | 81,504（11.05%） | 141,923（9.71%） |

raw_only 常是 BEGIN/END 或没有 local expected 的原始边界，设计上就不提供 local
一致性判断，不能全部当成漏采。incomplete_capture_hits 是另一项指标，不能与
raw_only 简单相加。yk→dzq 的 incomplete 比例下降，但仍需要改进稳定锚点。

dzq 的 100 个 local inconsistent 事件为 INODE.EVICT.TARGET.EXIT 66、
ORPHAN.FILE.DELETE.EXIT 17、ORPHAN.FILE.FREE.EXIT 17，存在同一次生命周期
多个观测点重复表达的问题，不能当成 100 个独立异常根因。

### 7.3 workload 的有效调用密度

dzq 高转换密度调用为 ftruncate 65.95%、rmdir 44.65%、fallocate 43.65%、
renameat2 31.07%、mkdirat 28.16%、unlinkat 27.17%。这些更接近目前计数不变量
关注的修改路径。

同时 openat 3300 次只有 32 次独立转换调用，openat$dir 4371 次只有 50 次；
read 720 次、removexattr 843 次均没有独立转换调用。后者成功率仅约 3%。
dzq 中 EBADF 占全部已验证调用 7137/32608=21.89%，说明仍有大量无效 FD 路径。
open/read 等依赖调用和观察调用不能全删；但应优化资源关联、路径/FD 生命周期、
xattr 名称及 flag 合法性，把更多执行预算送入目标路径。
write/pwrite64 的即时转换密度低也可能因为 delalloc 和后台归因，不能机械地
解释为数据写入完全没有文件系统效果。

## 8. 下一步优先级

1. **先修 reset/carry 的 per-device epoch 边界。** reset 的设备丢弃旧 epoch carry，
   用新 stable snapshot 建 anchor；其他设备保留其连续轨迹。验收条件是已知 reset
   不再产生 device_identity_not_unique，但真正缺口仍然会报警。
2. **处理 reset 时全局关闭 trace 的盲窗。** 优先保留连续采集、显式标注 maintenance
   来源和 per-device reset 边界，再在分析层排除 workload 统计；不要对所有设备
   的 seq 跳变统一豁免。若确需停采，必须记录全部设备的前后 identity 和不可验证区间。
3. **在 mkfs 前保存候选现场。** 对新增 hook/errno/后置条件签名做 CoW snapshot 或
   有界镜像保留，执行离线 fsck/raw check；将 generation、镜像和 segment id 绑定。
   最终镜像 clean 不能替代这一步。
4. **把 cleanup exit 与 workload exit 分开。** cleanup phase 在错误路径也闭合，
   单列 ENOTEMPTY/EIO/EROFS/EUCLEAN 和 child exit/signal；保留错误作为候选。
5. **优先复核 dzq 的 R-LINK 和 53 次 ENOTEMPTY。** 对照 yk 已有 rmdir/unlink/rename
   fixture，按真实 namespace 后置条件归并；再处理 inode/orphan 尾部。
6. **用专项 fixture 打开未覆盖路径。** resize/group-add、shared external xattr、
   EA inode、ACL/fileattr、inline conversion、directory exchange 分别建种子。
   随机探索负责找新组合，定点 single-hit/native-errno 实验负责证明条件性后置条件。
7. **重新做可比较的实验矩阵。** 固定同一 kernel/patch、CPU、scheduler、hook 集合、
   errno catalog、初始镜像和预算，跨多个 seed 重复；分别比较等墙钟和等 syscall。
   对 duration 可用同一条已完整保留的运行做 1/3/6/10h 前缀比较；若评估最终持久化，
   还需在各时间点保存镜像，而不是只截取最后报告数字。

## 证据与复算

原始 campaign 和 evidence 未被本分析重写。分析新增的文件仅在本目录。

- [结构化指标](metrics.json)：总体、逐小时、质量、hook、episode、定点复核统计。
- [逐 segment 数据](segments.csv)：1995 个 segment 的 hit/pass/reset/snapshot 口径。
- [复算脚本](compare_campaigns.py)：Python 标准库；运行后重建 JSON/CSV，内置总数核对。
- [lhze campaign report](../../results/campaign-lhze43mu/campaign-report.json)
- [yk campaign report](../../results/campaign-yk3_8nzt/campaign-report.json)
- [dzq campaign report](../../results/campaign-dzq91z1h/campaign-report.json)
- [yk 定点复核说明](../../results/campaign-yk3_8nzt/CANDIDATE-REPRO-ANALYSIS.md)
- [dzq segment 3 chain manifest](../../results/campaign-dzq91z1h/segments/segment-000003/analysis/chain-manifest.json)
- [dzq segment 909 manifest](../../results/campaign-dzq91z1h/segments/segment-000909/manifest.json)
- [dzq segment 910 manifest](../../results/campaign-dzq91z1h/segments/segment-000910/manifest.json)
- [dzq retained reset script](../../results/campaign-dzq91z1h/evidence/tests/guest-campaign.sh)

本分析读取已有报告并抽查对应 raw trace 边界、snapshot、代码和 fsck 文本；没有重新
运行 QEMU、重新执行 fsck 或将旧版本全部证据用当前 analyzer 重分析。不同版本
的结果保留原有口径，不能用新分析器结果覆盖原始记录后再声称实验可直接比较。
