# campaign-lhze43mu 候选复核

分析日期：2026-10-31。对象仅为 `campaign-lhze43mu`，不将另外两组的新版逻辑套用到本组。
本次只读取保留证据及最终镜像；未重新运行 workload，未修复镜像，未修改原始 report/manifest。

## 结论与证据边界

**44 条候选不能按 44 个缺陷计数。现有证据支持归并为一个 inode eviction 错误路径问题组，
包含 6 次有持久化残留证据的触发；其余大量条目是已损坏状态的重复观测。**

进一步对照 upstream 源码和定点 fixture 后，六次残留目前**不能定性为 ext4 真实 bug**。
final dirty 失败后跳过 `ext4_free_inode()` 是源码明确写出的保守错误路径；这里确认的是
“注入错误后的持久化后置状态”，不是正常路径违反合同。详细裁决见
[bug-adjudication.md](bug-adjudication.md)。

- 6 次 `inode_evict_final_dirty` 命中，每次留下 1 个零链接、位图仍分配的 inode。
  6 个目标 inode 与最终两个镜像的 fsck 错误逐一对应。
- 这是“给定注入模型下持久化回收不完整”的强证据，尚不是 6 个独立漏洞，
  也不是已确认的新内核 bug。当前更合适的标签是 expected error-recovery degradation。
- retained patch 和当前 upstream 源码的原代码注释明确说明：最终 `mark_inode_dirty`
  失败时不能释放 inode。因而 `R-IEVICT` 不能在失败分支无条件要求释放。
- 六次中 5 次 errno 属于 canonical final-dirty native 集合（EROFS/ENOSPC/EUCLEAN），
  segment 139 的 EINVAL 属于旧 campaign 宽集合；即使是 native errno，也还没有证明
  该 helper 错误在这个精确状态下自然发生。
- 原报告遗漏的 segment 53 注入事件已从原始 trace 恢复。真实命中现为 **20/20 可解析**，
  不是此前仅基于 manifest 得到的“19 已解析 + 1 unresolved”。原始文件未回写。

完整派生数据：

- [candidate-ledger.csv](candidate-ledger.csv)：44 条候选逐条分类。
- [real-hits.csv](real-hits.csv)：20 次真实注入，含 syscall、errno、inode 与计数变化。
- [snapshot-timeline.csv](snapshot-timeline.csv)：459 个 segment × 3 个文件系统 × before/after，共 2,754 条快照。
- [eviction-evidence.json](eviction-evidence.json)：6 次 eviction 的原始 trace 行、对象告警及 exit_group 证据。
- [offline-inodes.json](offline-inodes.json)：最终镜像的位图、inode 与目录引用复核。
- [summary.json](summary.json)：汇总及完整状态变化时间线。

## 1. 先拆开“候选数”“命中数”“缺陷数”

原始 `campaign-report.json::fault_injection`：

| 原始统计项 | 数量 | 复核解释 |
| --- | ---: | --- |
| hit_count | 40 | 20 次真实注入 + 20 次 off 状态残留误计 |
| candidate_count | 44 | 42 条 stable_snapshot + 2 条 offline_disk |
| unverified_count | 21 | 20 次 off 状态残留 + segment 53 的事件读取遗漏 |
| regular 候选 | 0 | 该镜像最终检查也干净 |
| inline snapshot 候选 | 23 | 12 个 segment，53 仅 after，其余 before/after |
| bigalloc snapshot 候选 | 19 | 10 个 segment，139 仅 after，其余 before/after |

按原始 manifest 的 enabled/operation 和完整快照重新分组：

| 候选条目类别 | 数量 | 应如何解释 |
| --- | ---: | --- |
| 首次从 namespace 可验证变为不可验证 | 2 | inline 53 after；bigalloc 139 after |
| 真实注入 segment 中，注入前已不可验证 | 18 | 延续旧问题的观测；其中仍包含后续 4 次真实新增 inode 残留 |
| off 状态残留所贡献的重复快照 | 22 | 11 个 segment × before/after，不能作为新注入结果 |
| 最终离线镜像确认 | 2 | 每个坏镜像 1 条；不是额外 2 个根因 |
| 合计 | 44 | 与原始 report 完全对账 |

不能简单“只保留 before 干净的 segment”：那会丢掉后续 4 次独立新增的 inode 残留。
正确做法是用目标 inode 位图后置条件、计数增量、最终磁盘状态区分新触发与旧状态。

11 个 off 候选 segment 为：
`56, 89, 142, 214, 229, 247, 259, 284, 329, 350, 455`。
它们均是相应真实命中后下一个同文件系统 segment（全局 segment_id + 3）。
原聚合函数只检查 `fault.hit`，没有排除 `status.enabled=false / operation=off`，
因此上一段保留的 `injected=1` 被再次算入。所有 20 个 off hit 都属于这个统计问题。

真实 20 次命中分布：`mballoc_mark=11`、`inode_evict_final_dirty=6`、`unlink_inode_dirty=3`。
本次用原始事件重新核对 errno、choice_index、choice_draw、seed、概率阈值和 status，20 次全部通过。
这只验证注入记录内部的一致性，不等于验证该 errno 在该 hook 的真实可达性。

## 2. 优先候选：inode 最终回收后分配位没有清除

下表 call_index 使用原始程序的 **0 起始编号**。五次存在 generated syscall envelope 的事件，
该 syscall 均返回 0；segment 53 发生在程序 `exit_group` 期间。

| segment | 文件系统 | 注入 errno | inode / 类型 | 实际位置 | free_inodes 前→后 | dirs 前→后 |
| --- | --- | --- | --- | --- | --- | --- |
| 53 | inline | -30 EROFS | 30 / 普通文件 | exit_group 退出回收 | 4075→4074 | 7→7 |
| 139 | bigalloc | -22 EINVAL | 38 / 目录 | rmdir，call_index 6，返回 0 | 4075→4074 | 7→8 |
| 211 | bigalloc | -28 ENOSPC | 35 / 普通文件 | renameat2，call_index 9，返回 0 | 4074→4073 | 8→8 |
| 256 | bigalloc | -28 ENOSPC | 31 / 目录 | rmdir，call_index 7，返回 0 | 4073→4072 | 8→9 |
| 281 | inline | -28 ENOSPC | 39 / 目录 | rmdir，call_index 6，返回 0 | 4074→4073 | 7→8 |
| 347 | inline | -117 EFSCORRUPTED | 35 / 普通文件 | renameat2，call_index 9，返回 0 | 4073→4072 | 8→8 |

errno 数字是 helper 级注入值，不能直接当成用户态 syscall 返回。
尤其 `INODE.EVICT END result=-117` 是观测器根据分配位仍为 1 合成的结果，
源码 `ext4_ct_evict_end()` 明确这样记录；它也不是底层函数或 syscall 实际返回 -117 的证据。

### 2.1 六次共同的证据链

每次均能连接到以下事件和最终状态：

1. 目标已经零链接，进入 `INODE.EVICT`；TARGET.BEFORE 显示其 inode 位图分配位为 1。
2. `ORPHAN.FILE.SLOT.DELETE` 清空对应 inode 的 orphan slot；
   DELETE.EXIT 为 `actual=0 expected=0 comparison_valid=1`，FREE.EXIT 也一致。
3. 在 `inode_evict_final_dirty` 处注入负 errno，跳过真实的最终 `ext4_mark_inode_dirty()`。
4. `INODE.EVICT.TARGET.EXIT` 为 `actual=1 expected=0 status=2 comparison_valid=1`。
   六次 lifecycle 的 `object_checks.findings` 都记录了同一目标 inode 的 R-IEVICT 告警。
5. 同设备、同目标 inode 在该注入之后没有 `INODE.FREE.BITMAP` 事件。
   注意 segment 53 中 inode 30 在更早时曾释放并重用，不能把那次早期释放算作本次成功回收。
6. segment 前后 `free_inodes` 净减少 1；目录事件同时使 `dirs` 净增加 1。
7. 正常关机后的最终 raw 镜像仍保留该分配位，inode 的 `links=0`、`dtime=0`。
   fsck 指出的 inode 编号与注入目标完全相同。

最直接的机制解释是：orphan 跟踪已经删除，最终 dirty 被注入为失败，
控制流只执行 in-core inode clear，未执行 inode 位图释放；最后留下无名称的已分配 inode。
这解释了运行时、计数增量和离线状态三层证据。

### 2.2 源码支持，但不能据此直接认定新 bug

保留补丁 `evidence/0001-counter-trace-core-and-probes.patch` 的 inode.c hunk 在行 3533 附近
保留原有注释：`we can't free the inode if the mark_dirty fails`，并把注入放在最终 dirty 调用之前。
错误分支明确调用 `ext4_clear_inode()`；正常分支才进入释放路径。

同时，观测器 `ext4_ct_evict_end()` 只根据零链接、引用计数、排他性、非 replay 等条件
决定 comparison_valid，**没有接收 dirty 的错误结果，也没有把失败分支排除在“必须释放”规则之外**。
所以 R-IEVICT 告警证明的是“未释放”这一事实，不能单独证明该错误处理违反 ext4 的设计合同。

本组使用手工的宽 errno 集合。直接在调用前返回 errno，也没有执行真实 helper 失败前
可能产生的 journal/error 状态更新。当前证据尚未证明 EINVAL、ENOSPC、EROFS 等在这个
精确位置的自然可达性及副作用等价性。因此建议暂记：
**“最终 dirty 注入失败后，零链接 inode 持久化残留 / 回收不完整”**，归并为一个问题组、六次实例。

### 2.3 segment 53 已补全归因

原 manifest 的 `event=null`，旧聚合把 `status.operation=random` 归成 unresolved。
但 `segments/segment-000053/trace.txt.gz` 的解压后原始行 **111304** 保存完整事件：

```text
operation=inode_evict_final_dirty errno=-30
pid=10817 target_pid=10817 group=0 block=30
errno_choice_index=4 errno_choice_draw=766534 random_draw=3664
```

下一行 111305 是 inode 30 的 TARGET.EXIT 告警。原始行 **111284** 为同一 PID 的
`sys_enter: NR 231`，即 exit_group；最后一个 generated fchmod 的 END 已在行 111106 出现。
所以这次归到退出时的 inode 回收，不应归到 fchmod，也不应凭注入事件中的默认 call_index=0
归到第一个 openat。

旧 runner 在 `collect_segment()` 内解析仍由异步 collector 写入的 trace，直到下一段 rotate
才等待前一段 sealed。源码存在提前解析的时序缺口；这是 event 缺失的合理解释。
但没有当时线程调度/读取边界的证据，不能进一步声称已经精确还原这一次遗漏的竞态过程。

## 3. 最终磁盘证据：六个对象，不是两个额外根因

保留的 finish_disk 逻辑先正常关闭虚拟机，再运行 `e2fsck -fn`，未修复镜像。
bigalloc 和 inline 的 fsck 退出码均为 4。

| 镜像 | fsck 指出的零 dtime 已删除 inode | inode bitmap differences | 目录计数 |
| --- | --- | --- | --- |
| bigalloc | 31、35、38 | -31 -35 -38 | stored 9，counted 7 |
| inline | 30、35、39 | -30 -35 -39 | stored 8，counted 7 |

本次以只读 `debugfs stat/testi` 和保留的原始格式解析器独立核对：
六个 inode 的位图都仍为分配，links 和 dtime 都为 0。
bigalloc 多保留两个已删除目录（31、38），inline 多保留一个（39），
刚好解释目录计数分别多 2、多 1；普通文件残留不会增加目录计数。

### 3.1 为什么 bigalloc 的 disk checker 仍然 valid=true？

`check-disk.py` 按分配位统计 inode，并把分配位中的目录 mode 计入 r_ug，
没有从目录总量里排除这些已删除但仍分配的目录。
因此 bigalloc 的位图/descriptor/superblock 彼此一致，目录扫描也是 9 对 9，
它报告 verified；这不覆盖“零链接 inode 本应已经回收”的语义。

这是**被核对的元数据彼此一致，但共同保留了不应继续存活的对象**。
它说明单纯计数守恒检查无法代替生命周期终态检查。

### 3.2 inline 的 R-LINK 告警能否另算一个 namespace bug？

原始 disk checker 对 inode 39 报 `directory_refs=1, stored=0`。
本次枚举最终镜像所有已分配目录后，该 inode 的唯一引用为：

```text
directory=39, name='.', target_inode=39
```

没有任何存活目录里的普通名称引用它。inline 目录的旧内容仍保存在 inode 内部，
保留解析器会为它构造 `.` 和 `..` 两条引用，即使该目录自身 links 已经为 0。
bigalloc 的两个残留目录 size 为 0，没有同样的内部目录项残留。

因此，这个 R-LINK 告警属于 segment 281 的残留目录问题的同一后果，
当前没有证据把它升级为另一条“存活 namespace 错链”根因。

## 4. 其余真实命中但被标成候选的五个 segment

| segment | 文件系统 | hook | errno | generated syscall | 是否已有坏 namespace | 本段 free_inodes/dirs 是否新增变化 |
| --- | --- | --- | --- | --- | --- | --- |
| 86 | inline | mballoc_mark | -30 | fallocate / index 3，返回 -30 | 是 | 否 |
| 226 | bigalloc | mballoc_mark | -74 | fallocate / index 3，返回 -74 | 是 | 否 |
| 244 | bigalloc | mballoc_mark | -5 | mkdirat / index 5，返回 -5 | 是 | 否 |
| 326 | inline | mballoc_mark | -117 | fdatasync / index 17，返回 -117 | 是 | 否 |
| 452 | inline | unlink_inode_dirty | -117 | unlinkat / index 4，返回 -117 | 是 | 否 |

它们的 object_checks 均没有新增 mismatch；before 已是 inode_read_failed。
42 条 snapshot 候选中，这五段贡献 10 条；候选标签本身没有给它们建立新的因果关系。
本次未发现可与它们单独关联的新增持久化对象问题，但这不排除未被现有 oracle 覆盖的数据语义问题。

segment 452 尤其容易误归因：注入对象是 inode **34**，而最终 inline 的 R-LINK 对象是 **39**。
最终 inode 34 的位图已清除，debugfs 中旧 inode 内容有非零 dtime；它没有进入最终 fsck 的错误集合。
因此不能把最终 inode 39 的告警归给 segment 452 的 unlink 错误。

另外，所有 11 次 mballoc_mark 和 3 次 unlink_inode_dirty 的 segment 前后 free_clusters、
free_inodes、dirs 均无保留增量；regular 的全部 918 条 namespace 快照始终可验证，最终镜像干净。

## 5. 为何重复候选会不断增加，且 persistent_mismatch 仍是 0？

所有 **42 条 snapshot 候选** 的完整快照实际上都是：

```text
valid=true
namespace_valid=false, namespace_reason=inode_read_failed
namespace_compared=0, namespace_mismatched=0
relations_valid=true, observed_mismatches=0, relations_mismatches=0
```

旧 `stable_snapshot_health()` 把任一 validity=false 都归成 candidate，
并在缩略 observation 中只保留顶层 reason=stable，丢掉了关键 namespace_reason。
这会让摘要看起来像“稳定状态检测到错数”，实际是 namespace 扫描中止，尚未作有效比较。

源码 `ct_ns_scan_group()` 根据分配位遍历 inode，调用 `ext4_iget(...EXT4_IGET_NORMAL)`；
某个 inode 读取失败就返回 inode_read_failed。
本次的残留 inode 与第一次失败时间一致，且会持续保留；“残留零链接 inode 阻断扫描”
是与源码和镜像相符的机制解释。快照未记录失败 inode 编号/iget errno，故不能声称每次扫描的
失败对象都已逐次定位。

全量 2,754 条快照的时间线：

| 文件系统 | before/after 总数 | namespace 不可验证数 | 首次失败 | 后续恢复 |
| --- | ---: | ---: | --- | --- |
| regular | 918 | 0 | 无 | 不适用 |
| bigalloc | 918 | 639 | segment 139 after | 无 |
| inline | 918 | 811 | segment 53 after | 无 |

也就是说，本次总共看到 **1,450 条 namespace 不可验证快照**，而报告只挑出了其中
hit 标签下、与 workload kind 相同的 42 条。44 这个数高度依赖筛选口径，不能衡量问题发生次数。

最后，六次 R-IEVICT 已在 lifecycle report 的 **object_checks.findings** 中出现，
旧 `record_lifecycle_fault_candidate()` 却只看顶层 classification_counts 里的
operation_exit_mismatch/persistent_mismatch，没有汇入 object_checks。
所以 persistent_mismatch=0 和 lifecycle candidate=0 都不能覆盖或否定这六个持久化残留。

## 6. 建议如何登记和安排下一步复核

1. 主问题登记为一个 eviction/final-dirty 错误路径问题组，记录以上 6 个触发实例，
   证据状态写“注入条件下运行时位图后置条件失败，最终镜像确认残留”。
2. 为“真实 helper 错误模型”单列待验证项：选择 native 可达 errno，明确 journal/abort 副作用，
   检查保留 inode 是否属于允许的错误退化行为，避免把无条件成功后置条件用于失败分支。
3. 定点最小复核应优先选 139 或 281 的 rmdir，以及 211 或 347 的 rename-replace；
   同一初始镜像分别运行无注入与单次注入，保留注入后、卸载后、再次挂载后镜像。
   segment 53 则另保留 exit_group 下的匿名/未命名 inode 回收场景。
4. 把 oracle 失败（inode_read_failed）与有效数值 mismatch 分栏；加入首次坏状态、
   新增目标对象、已存在坏状态三种标记。不能仅用 before 是否已坏来过滤新问题。
5. 聚合层排除 disabled/off 残留，在 trace sealed 后解析事件；把 object_checks.findings
   纳入候选；离线增加 allocated && nlink==0 的终态检查，并区分 deleted directory 内部引用。

这些是从本次证据得到的后续建议。本次未改动运行代码或执行新的控制复现实验。

## 复算

从仓库根目录执行：

```bash
python3 experiments/ext4-counter-trace/research/2026-10-31-single-random-fault/lhze-candidates/analyze_candidates.py
```

脚本只读取 results 下原始证据及 raw 镜像，向本目录写入派生文件。
它包含 44 条候选对账、20 条真实命中、6 条 eviction 及最终残留 inode 集合相等的断言。
