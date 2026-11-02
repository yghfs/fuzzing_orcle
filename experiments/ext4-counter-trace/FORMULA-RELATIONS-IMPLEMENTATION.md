# 新增公式实现与下一步路线

更新时间：2026-10-16（Asia/Shanghai）

根目录 `公式.md` 新增的四组运行时关系已经接入独立的 Linux 6.12.1 开发树。插桩只位于 `experiments/ext4-counter-trace/dev/linux-6.12.1/`，不会修改主项目或官方 pristine 源码。

## 已实现的关系

| 公式 | 独立观测来源 | 主要插桩点 | 适用边界 |
| --- | --- | --- | --- |
| `R-IH` inode 表高水位 | inode bitmap 与 `bg_itable_unused` | `INODE.ALLOC.HIGHWATER`、稳定快照 | 需要 descriptor checksum 且 group 不是 `INODE_UNINIT`；旧格式返回 `unverified` |
| `R-IEVICT` 最终释放位 | eviction 前后目标 inode bitmap bit | `INODE.EVICT.TARGET.BEFORE/EXIT` | 只对普通、非坏 inode 的最后一次 eviction 判定 |
| `R-IDELTA-EVICT` 释放增量 | 同一目标 group descriptor 与 global free-inode counter | `INODE.EVICT.DESCRIPTOR.EXIT`、`INODE.EVICT.GLOBAL.EXIT` | 只在同一 writer 窗口、没有其他 inode 转换时判定；否则 `unverified` |
| `O-LIFE` orphan 生命周期 | orphan slot 内容与 inode orphan owner | `ORPHAN.FILE.SLOT.ADD`、`ORPHAN.FILE.ADD.EXIT`、`ORPHAN.FILE.DELETE.EXIT`、稳定扫描 | 只适用于启用 orphan-file 的文件系统；旧链表模式不声称覆盖此关系 |
| `O-LIFE-DELTA` 空槽增量 | 删除前后的 orphan slot 和 `ob_free_entries` | `ORPHAN.FILE.SLOT.DELETE`、`ORPHAN.FILE.FREE.EXIT` | 内存状态后置条件；不等同于 journal 已写入磁盘 |
| `O-LIFE-FREE` 空槽总量 | 独立遍历 orphan block slots 与 free-entry counter | `SNAPSHOT.MEMORY.STABLE` | 快照必须稳定且 orphan block 可读 |
| `XATTR-REF` 外部 xattr 引用 | 独立扫描 inode `i_file_acl` 与 xattr header `h_refcount` | `XATTR.BLOCK.REF.INIT`、`XATTR.INODE.POINTER`、稳定扫描 | 扫描已被 inode 引用的外部块；零引用泄漏需 bitmap/离线检查补充 |
| `XATTR-REF-DELTA` xattr 释放增量 | detach 后实际 header decrement 或 refcount-one block free | `XATTR.BLOCK.REF.INC/DEC/RELEASE`、`XATTR.INODE.DETACH.EXIT` | journal write-access 失败、并发或 block 不可读时为 `unverified` |
| xattr checksum | header 内容和 block number 的独立 checksum 重算 | 稳定快照 `XATTR-REF.CSUM` | 仅 metadata checksum 文件系统有 checksum 语义 |

每个事件保留 `actual`、`expected`、`delta/update_delta`、状态和原因。`RAW` 是转换证据，不是“一致”；分析器只把状态为 consistent 且比较有效的事件计入一致数。后续同一个 inode 或 block 的成功事件不会修复之前对象的失败事件。

## 定向验证

`scripts/run-relations.sh` 启动一个短时 TCG VM，在 regular、bigalloc、inline 和 legacy 四种镜像上运行共享 xattr、open-unlinked、最终 eviction 和 inode reuse 负载。历史通过结果目录已清理；本文件保留该运行的范围和结论摘要。

该运行同时通过独立磁盘 checker、`e2fsck -fn`、序号/CPU 丢失检查和稳定快照。三种 orphan-file 镜像均有 `O-LIFE` 与 `O-LIFE-DELTA` 一致事件；legacy 镜像按设计把 orphan-file 和 `R-IH` 标为不适用。此轮结果目录已按历史材料清理要求删除，本文件保留其范围和结论摘要。

代码层回归包括：kernel GCC 14 构建、patch replay、source audit/build binding、生命周期 22 项、point 12 项、campaign 33 项、syzkaller、KCOV 和 disk-layout 测试。完整 VM 关系短测不是长时间稳定性证明。

## 当前不能声称的内容

1. 关系扫描是有界的：最多扫描 `1<<20` 个 inode；inode table、orphan block 或 xattr block 读不到时必须是 `unverified`。
2. 运行时 xattr 扫描从 inode 引用集合重算引用数，不能单独发现没有任何 inode 引用的泄漏块；持久化结论仍依赖卸载/恢复后的离线 checker。
3. `O-LIFE` 在没有 orphan-file 的旧格式上不适用；它不能替代旧 orphan 链表的持久化检查。
4. 事件完整性、稳定窗口和 CPU 丢失证据是结论前提。任何 trace loss、活动端点或无法归属的后台更新都不能当成一致。
5. 定向负载目前证明了正常转换和正常返回路径；journal write-access 失败、eviction 错误出口和 xattr 损坏需要单独 fault-injection fixture 才能声称已实测命中。

## 长跑前四项门槛

截至 2026-10-16，四项门槛均已完成实现并通过当前源码构建的短时验收；本节记录可复核的实测结果，不把它等同于 12 小时稳定性结论。

1. **资源关系有效的操作序列：通过受控等量 A/B。** 该短测结果目录已清理；保留的汇总显示普通与 resource-valid/stateful 两组各生成并核验 640 条 syscall，两组计数器 campaign 通过，KCOV 样本完整。普通组观察到 23 次独立公式转换（3.59%），resource-valid 组观察到 183 次（28.59%）。`linkat/unlinkat/rmdir/renameat2` 成功数分别为普通组 `0/0/0/0`、resource-valid 组 `20/22/40/81`。这是一组固定 seed 的短测，证明方向有效，不代表统计置信区间或长期吞吐。
2. **联合反馈与语料调度：MVP 已实现。** 完整、且所有 syscall 坐标经 native trace 核验的样本才能更新覆盖集合或进入语料；未核验样本不会改变公式稀有度/mismatch 签名。父程序按新普通 PC、新 ext4 PC、罕见公式点、可复现 mismatch 和 unverified 信号加权轮转。`tests/test-kcov-feedback.py` 覆盖准入门槛、mismatch 信号和加权选择；A/B campaign 实际记录了 PC 集合、语料增长和非零联合反馈分数。尚未接入 syzkaller manager、多 VM corpus 合并或 CMP feedback。
3. **场景矩阵：通过历史短 campaign 与定向 VM。** 六段 `scenario=all` 短 campaign 目录已清理；历史汇总显示 192 条生成 syscall 全部 native 核验，KCOV 六个样本完整，regular/bigalloc/inline 三个镜像均通过稳定快照、独立磁盘 checker 和 `e2fsck`。relations 和 special 定向场景分别覆盖 orphan/xattr/inode eviction、bigalloc、resize、corruption 和 fast-commit replay。
4. **新增公式：通过当前源码的定向 VM 检查。** 根目录 `公式.md` 中 `R-IH`、`R-IEVICT`、`R-IDELTA-EVICT`、`O-LIFE`、`O-LIFE-DELTA`、`O-LIFE-FREE`、`XATTR-REF`、`XATTR-REF-DELTA` 和 xattr checksum 已进入 runtime/offline checker、trace 与定向测试。定向运行目录已清理，范围和限制以本文件为唯一摘要。

**Fast-commit 特殊路径观察到真实 `R-UG` 持久差异。** 原始 special 运行目录已清理；根目录 [`FAST-COMMIT-DIR-COUNT-FINDING.md`](FAST-COMMIT-DIR-COUNT-FINDING.md) 保留唯一 finding 报告。该报告记录断电恢复后 inode bitmap 独立扫描得到目录数 4，而 group descriptor 保存 3；独立磁盘 checker 与 `e2fsck -fn` 均报告同一个不一致。它不是“磁盘计数正确”的通过，也不应被普通稳定一致性检查吞掉。

syzkaller 生成器现将 `-calls N` 补齐为恰好 N 个 syscall，而不把 syzkaller 的“最多 N 个”误当成固定测量预算。KCOV guest 默认缓冲为 16,777,216 项，runtime 上限、host parser 和回归测试保持一致。

## 仍未完成的研究边界

- `BLOCK_UNINIT` 磁盘布局仍有未支持形态；checker 对这些布局返回 `unverified`。
- worker 外的后台释放/跨线程 parent-operation 因果仍不能完整归属；相关事件保持 ambiguous/unverified。
- eviction、orphan、xattr journal 错误出口还缺独立 fault-injection fixture；现有 VM 命中证明正常转换路径，不证明这些错误出口。
- 多 VM syzkaller manager、自动恢复长跑和 12 小时持续性/吞吐趋势需要后续独立实现或实验。

这些边界不阻止受控单 VM 长跑启动，但不应被写成已覆盖或已验证。
