# ext4 Bug Inventory

历史复核主体截至 2026-11-02，extent v2 补充截至 2026-11-10，独立 ext4 三候选补充截至 2026-11-16；文档边界核对于 2026-11-11。
本次未重跑下列内核实验，也未重新查询上游最新 HEAD；“主线状态”仅指各条明确列出的历史版本。
当前实现与结果可用性见 [实现状态](experiments/ext4-counter-trace/IMPLEMENTATION-STATUS.md)。此清单记录已观察问题、源码定位和证据结论。重复或过时的历史运行目录
按本轮清理要求移除；规范的当前块层 EIO 记录、必要的源码语义注入材料、修复对照和可重跑
脚本保留。历史数值与证据边界保留在这里。核心公式范围见 [`公式.md`](公式.md)。下文的
“证明错误”说明可观察结果违反的关系或操作后置条件，
不把 fault 注入本身当成自然 syscall 触发频率证据。调用者边界注入可以用 helper
真实返回集合中的 errno 替代一次 helper 调用结果，以验证原调用者错误分支；这不表示
helper 内部执行过或真实发生了 I/O/分配故障。只有原调用者走到该错误分支，且正常卸载后
的磁盘检查或 raw 元数据解码确认状态不一致时，才记录为条件性错误路径 finding。

判定口径：本清单只保留已由稳定快照、离线检查、自然 syscall，或源码语义注入后正常
卸载的离线检查支持的 finding，另保留明确标注的历史已修复问题和通过验证的对照路径。
仅有源码推断、fault-only 中间态、并发在线告警或尚未确认的 API 契约不列为漏洞记录。

针对本轮“禁止 `EXT4_IOC_SHUTDOWN`”约束的最终分类以
[`FINAL-AUDIT-20261031.md`](experiments/ext4-source-fault-study/FINAL-AUDIT-20261031.md)
为准：确认 6 类持久化缺陷，其中 5 类违反容量/命名空间计数后置条件
（group extend、mkdir、rmdir、unlink、普通 rename），另 1 类是 group add 失败后
resize inode 持久化了超出已发布容量的非法块引用。`rename_setent_dirty` 的 `-ENOSPC`
调用边界注入从另一错误出口复现了同一个 rename 链接计数不一致，不另算一个独立缺陷
根因；该 hook 跳过 JBD2 内部 credit check，没有模拟真实 credit exhaustion。group add 的证据不支持
旧的 `R-BSUM`/`R-ISUM` totals mismatch 说法。inode eviction 的 bitmap 未释放属于保守恢复
语义，orphan-file/xattr 条目不作为已确认 finding。

对 `rename-setent-dirty-enospc` 追加的无注入压力验证见
[`NATURAL-RENAME-JBD2-ENOSPC-20261031.md`](experiments/ext4-counter-trace/NATURAL-RENAME-JBD2-ENOSPC-20261031.md)：
ordered 模式 20 秒、60 秒和 `data=journal` 30 秒的普通 syscall workload 均未自然
得到 `ENOSPC`，也未出现 JBD2 `errcode -28`；这项 finding 继续只表示“条件于 JBD2
真实返回 `-ENOSPC`”，不表示普通 rename 已被自然触发；本轮 `rename_setent_dirty` 注入
也只替代 helper 返回值，没有执行 JBD2 的 credit exhaustion 分支。

旧 checker 的附加计数模型按用户要求保留，包括本清单的对应条目、模型代码、现存
测试及已有复现证据；本次清理不以它们不属于核心公式为由删除。

## 分类速览

- **核心公式已审计路径**：group extend 容量/bitmap 一致性、普通 mkdir/rmdir/unlink/rename/linkat 的目录计数及 `R-LINK`。group add 失败后 resize inode 指向未发布容量之外的块，是同一审计范围发现的持久化 resize metadata defect；本轮并未证明旧声称的 `R-BSUM`/`R-ISUM` mismatch。inode eviction 仍保留作生命周期审计材料，但按本轮最终报告降级为保守恢复语义。自然 inode/block quota `EDQUOT` 的失败回滚验证通过；新增 fast-commit replay 目录计数漏洞单列如下。
- **新增已复现公式漏洞**：fast-commit replay 新目录后 `bg_used_dirs_count` 少计一个，违反 `R-UG`。使用普通 `mkdir`、文件 `fsync` 和 host hard-stop 自然触发；稳定运行时扫描、离线 bitmap/inode 扫描及 `e2fsck` 三方确认。复现报告见 [`FAST-COMMIT-DIR-COUNT-FINDING.md`](experiments/ext4-counter-trace/FAST-COMMIT-DIR-COUNT-FINDING.md)。
- **历史 forced-shutdown 样本**：早期 `EXT4_IOC_SHUTDOWN` 竞态没有作为当前持久化 finding 证据；其无效和重复运行目录已于 2026-11-02 清理。当前结论使用正常卸载后的源码语义复核与块层 `EIO` 记录。
- **历史主线核对**：当次记录的 torvalds/linux HEAD 为 `32c52ec87d6245ee2d12ed5dd509e40062939b50`。已从该 HEAD 核对 `fs/ext4/namei.c`：下列 unlink、rename 和 mkdir 错误出口更新顺序均仍存在，未发现相应回滚修复。
- **此前 syscall-only 扩展不变量**：extent `i_blocks`、EA inode 引用、共享 xattr block COW 引用，以及 orphan-file slot；它们分别由 extent 可达性、EA 容器引用、xattr 条目引用和 orphan 生命周期约束。
- **2026-11-01 源码语义注入复核**：除 fast-commit 外，5 项违反核心容量/命名空间计数后置条件的条件性缺陷得到正常卸载后的磁盘证据：group extend、mkdir、rmdir、unlink、普通 rename；group add 另有失败 resize 后非法 resize-inode 引用的持久化缺陷，但没有证明 active-group `R-BSUM`/`R-ISUM` totals mismatch。`rename_setent_dirty` 的 `-ENOSPC` 合法返回值替代是同一 rename 链接计数缺陷的另一条 caller-error-path 复现；该 hook 跳过 `jbd2_journal_dirty_metadata()`，因此没有实际执行 credit exhaustion 分支，也不构成第七个独立根因。eviction 属源码规定的保守恢复行为；orphan-file/xattr 本轮没有取得可归因于该单项且完成 journal recovery 的持久化证据，不计入。逐项边界和证据见下方复核表及 [`FINAL-AUDIT-20261031.md`](experiments/ext4-source-fault-study/FINAL-AUDIT-20261031.md)。
- **2026-11-02 真实块层 EIO 复核**：通过 QEMU `blkdebug` 让 guest 的实际块请求返回 `EIO`，并在正常卸载后离线检查。group extend、rmdir、unlink、普通 rename 均命中清单所述错误位置并留下对应磁盘不一致；mkdir 的块层 EIO 命中 `add_dirent_to_buf()` 较早的错误出口并留下悬空目录项，但在父 `nlink` 已递增后的精确 dirty 分支暂停/清缓存复核中没有命中 EIO，mkdir 成功。group add 的真实写 EIO 触发 JBD2 abort；该 finding 依赖 `ext4_mb_add_groupinfo()` 的分配失败 `-ENOMEM`，块 I/O 错误不验证这一条件。故 5 项核心计数 finding 中 4 项有精确块层 EIO 复核，mkdir 有相邻错误分支的真实 EIO 持久化复核，精确分支仍只有源码语义注入证据；细节和证据链接见 [`PHYSICAL-IO-CORE-20261102.md`](experiments/ext4-source-fault-study/PHYSICAL-IO-CORE-20261102.md)。这里的“真实块层 EIO”指 QEMU 向 guest 返回 I/O 错误，不是宿主物理介质故障。
- **2026-11-16 独立 ext4 三候选复核**：在独立 Linux 快照上使用 QEMU `blkdebug`，只对目标 syscall 窗口返回真实 guest-visible `EIO`。`linkat`、`FALLOC_FL_PUNCH_HOLE` 和碎片化 `FALLOC_FL_COLLAPSE_RANGE` 均在正常卸载后留下持久化不一致；对应故障组 `e2fsck -fn` 返回 4，控制组返回 0。三项分别违反 `R-LINK`、extent ownership/bitmap 和 extent `i_blocks` 后置条件；`RENAME_EXCHANGE` 父目录计数候选因真实 EIO 先命中既有 cross-rename 路径、ENOMEM 复核 clean，未计入独立 finding。完整报告见 [`INDEPENDENT-THREE-CANDIDATES-20261116.md`](experiments/ext4-source-fault-study/INDEPENDENT-THREE-CANDIDATES-20261116.md)，只读校验器见 [`verify-independent-ext4-audit.py`](experiments/ext4-source-fault-study/scripts/verify-independent-ext4-audit.py)。
- **历史修复**：inline data 长度溢出 Oops 已由上游修复，本清单只保留修复记录。

## 核心计数公式

### fast-commit replay 新目录后 group directory count 未增加（已确认）

- 公式归属：`R-UG`，稳定运行时的 group `bg_used_dirs_count` 必须等于该 group 中已分配目录 inode 数；该关系也在离线磁盘检查中独立验证。
- 源码路径：`ext4_fc_replay_create()` 重放 directory `CREAT` 时调用 `ext4_mark_inode_used()`、初始化 `.`/`..` 并链接目录，见 [`fast_commit.c:1652`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/fast_commit.c#L1652) 和 [`fast_commit.c:1666`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/fast_commit.c#L1666)。[`ialloc.c:807`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/ialloc.c#L807) 的 `ext4_mark_inode_used()` 设置 inode bitmap 并递减 `bg_free_inodes_count`，但没有检查恢复 inode 的 mode 或增加 `bg_used_dirs_count`。普通 inode 分配路径则在 [`ialloc.c:1291`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/ialloc.c#L1291) 对目录增加 group 目录数。
- 问题描述：崩溃恢复通过 fast commit 将新目录 inode 和目录项 replay 到文件系统后，该 inode 成为已分配目录，但所属 group descriptor 的 `bg_used_dirs_count` 没有相应增加。随后 mount 初始化的 `s_dirs_counter` 也以少计后的 group descriptor 求和为来源；稳定 `R-UG` 检查会发现它与 inode mode 扫描不符。
- 自然触发与证据：负载先建立全提交基线，再执行 `mkdir child`，在新目录内创建文件并 `fsync()`，host 随即硬停止 VM；第二次 mount 执行 fast-commit replay。ext4 标准 trace 明确记录 `tag 3 (EXT4_FC_TAG_CREAT), ino 14`，checker trace 显示目录 inode 14 被 replay claim 并初始化。重启后的 syscall 验证目录及文件可见。稳定内存快照报告 group 0 `bg_used_dirs_count=3`、inode mode scan `=4`；同一镜像离线 checker 得到 `descriptor_dirs=3`、`inode_dirs=4`，`e2fsck -fn` 报 `Directories count wrong for group #0 (3, counted=4)`，退出码 4。没有使用 fault injection。
- 主线状态：审计时从 torvalds/linux `master` 获取的 HEAD 为 `0fcb05b72344f40806cbe810de8872f1698bf053`（2026-10-14）。该版本 `fs/ext4/ialloc.c:761` 的 `ext4_mark_inode_used()` 仍只减少 free inode 计数，没有增加 used directory 计数；`fs/ext4/fast_commit.c:2031-2078` 的 `ext4_fc_replay_create()` 也没有补做该更新，故该具体问题在检查的主线 HEAD 中尚未修复。
- 证据：完整分析报告见 [`FAST-COMMIT-DIR-COUNT-FINDING.md`](experiments/ext4-counter-trace/FAST-COMMIT-DIR-COUNT-FINDING.md)；复现规则由 [`analyze-fc-dir-count.py`](experiments/ext4-counter-trace/scripts/analyze-fc-dir-count.py) 验证。原始运行目录已按清理要求移除。该 replay 同时观察到 directory link-count 的独立差异；本条只按 `R-UG` 计为一个 finding，不将同一触发链拆成多个漏洞。

### inode eviction 失败后 bitmap 未释放（降级：保守恢复语义）

- 公式归属：`R-IEVICT`，属于 inode 生命周期后置条件。该错误出口同时跳过 bitmap 和 free-inode counter 更新，因此 `R-IG` 仍可能成立；`R-IDELTA-EVICT` 只适用于 inode slot 实际释放后的计数增量。
- 源码位置：[`inode.c:304`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/inode.c#L304) 在最终 inode dirty 失败时只清理内存 inode，不调用 `ext4_free_inode()`；mode 30 注入定义在 [`fault-inject.c:223`](kernel/linux-6.12.1/fs/ext4/fault-inject.c#L223)。inode 真正释放和 bitmap/global 更新在 [`ialloc.c:347`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/ialloc.c#L347)。
- 问题描述：eviction 的 inode 元数据无法写入时，磁盘 inode bit 可能继续占用，导致 inode 空间泄漏。
- 旧的 forced-shutdown/close 运行不是当前缺陷证据，原始临时目录已清理。无错误注入的测试没有命中 eviction 错误出口；当前分类依据是 `ext4_evict_inode()` 的保守恢复语义。
- 观测判据：最后一次 eviction 结束后 bit 仍为 1 可被 checker 记录，但只有最终 inode 更新成功、正常释放应当完成时才适用 `R-IEVICT` 正常释放保证。final-dirty 错误保留 bit 的分支不满足该前提。如果 bitmap 与 descriptor/global free-inode 数一起保持未释放状态，不能仅凭这一点称为 `R-IG` 违例。目录 inode 未释放还会使离线 `e2fsck` 将 `bg_used_dirs_count` 判为高于有效目录重算值。
- 本轮最终分类：`ext4_evict_inode()` 源码明确要求 mark-dirty 失败时不能调用 `ext4_free_inode()`，以避免 inode-table 未安全写回时复用 inode number；因此 bitmap 未释放是保守恢复代价，暂不作为独立 patch finding。
- checker 覆盖：eviction 错误出口现在直接读取目标 inode bitmap bit，检查后置条件 `allocated(t1)=0`；本轮无 fault、无断电运行报告 `model=inode_bitmap source=inode_eviction_error ... expected=0 actual=1`。
- 证据边界：旧 mode 30/forced-shutdown 运行只作过故障窗口探索，不计为持久化 finding；对应临时输出已清理。

### orphan-file slot 在 inode eviction 错误出口中未释放（本轮未确认独立持久化缺陷）

- 公式归属：扩展 orphan 计数不变量 `O_free + O_used = O_slots`，其中每个仍被磁盘 orphan-file slot 引用的 inode 必须处于需要 orphan cleanup 的状态；完成最后一次 `close()` 后，已清理 inode 不得继续占用 slot。
- 源码位置：[`orphan.c:230-264`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/orphan.c#L230) 的 `ext4_orphan_file_del()` 在 `handle == NULL` 或 `ext4_journal_get_write_access()` 失败时跳到 `out`，直接清除 `EXT4_STATE_ORPHAN_FILE` 和内存链表，却没有清零磁盘 slot，也没有恢复 `ob_free_entries`。inode eviction 在 [`inode.c:233-240`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/inode.c#L233) 的错误清理路径会调用 `ext4_orphan_del(NULL, inode)`。这描述源码可达路径，不构成本轮已确认的持久化 finding。
- 本轮源码语义复核：旧的直接 helper 返回注入已被替换。新注入设置文件系统设备的 writeback-error sequence，再调用真实 JBD2 write-access helper；日志确认 JBD2 原生 `Aborting journal`，hook 命中一次，正常卸载。离线 `e2fsck -fn` 因 journal 未清洁而跳过 recovery，并报告 orphan slot 未清洁及 inode eviction/bitmap 等待恢复；这不能证明已提交的 orphan slot 独立计数错误。见 [`orphan-access-EIO.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101-native-jbd2/orphan-access-EIO.log) 和 [`.e2fsck.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101-native-jbd2/orphan-access-EIO.e2fsck.log)。
- 历史 forced-shutdown 实验和在线 checker 输出已清理，不作为持久化证据；`slot(t1)=1` 的在线观察不能替代 journal recovery 后的磁盘后置条件。
- checker 覆盖：仅有 `O_free + O_used = O_slots` 守恒式时，slot 与缓存可以一起保持错误而不报警；现改为在 orphan 删除错误出口检查 inode 生命周期后置条件 `slot(t1)=0`。同一无 fault、无断电运行报告 `model=orphan_file_entries source=orphan_file_error ... expected=0 actual=1`。
- 本轮分类：**不计为 finding**。JBD2 abort 后未恢复的镜像无法判断 orphan slot 的最终一致性；本轮没有得到独立、可归因且完成恢复检查的持久化 mismatch。
- 主线状态：检查的 `kernel/linux-6.12.1/fs/ext4/orphan.c` 仍保留该错误出口；本项目主线基线没有对应修复。

### 共享 xattr block 引用计数未递减（本轮未确认独立持久化缺陷）

- 公式归属：扩展 xattr block 引用不变量 `h_refcount ==` 所有 inode 的 `i_file_acl` 对该 block 的实际引用数；删除一个持有共享 xattr block 的 inode 后，持久化 `h_refcount` 必须减少一。
- 源码位置：[`xattr.c:1376-1390`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/xattr.c#L1376) 的 `ext4_xattr_release_block()` 在 `ext4_journal_get_write_access()` 失败时直接返回，未递减 `h_refcount`。`ext4_xattr_delete_inode()` 在 [`xattr.c:3127-3135`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/xattr.c#L3127) 仍继续清理 inode 的 xattr 指针；forced shutdown 后最后一次 inode eviction 因而可丢失共享 block 的一次持久化引用递减。
- 历史 forced-shutdown 样本曾报告共享 xattr block 引用计数差异，但后续复核没有得到独立、完成 journal recovery 后的持久化 mismatch；该样本不作为 finding，原始运行目录已清理。
- 主线状态：检查的 `kernel/linux-6.12.1/fs/ext4/xattr.c` 仍保留该错误出口；本项目主线基线没有对应修复。
- 本轮源码语义复核：旧的直接 `EROFS` 返回注入不执行 JBD2 原生错误处理，已由设置文件系统设备 writeback-error sequence、再调用真实 JBD2 write-access helper 的 `EIO` 注入取代。JBD2 随后原生 abort；正常卸载后的 `e2fsck -fn` 跳过 journal recovery，且未报告 xattr block `h_refcount` mismatch。该镜像还有未恢复 transaction 的 free-block 差异，不能据此归因于 xattr 引用计数。见 [`xattr-release-EIO.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101-native-jbd2/xattr-release-EIO.log) 和 [`.e2fsck.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101-native-jbd2/xattr-release-EIO.e2fsck.log)。
- 本轮分类：**不计为 finding**。尚无独立、完成 journal recovery 后仍存在的 xattr `h_refcount` mismatch。

### group extend 错误返回后 superblock 容量与 bitmap 不一致（条件性源码语义 finding）

- 公式归属：`R-BG`、`R-BSUM` 与 `P-DISK`。resize 是容量变化，不能套用固定容量的普通 `R-BDELTA`；完成或失败边界仍须让 superblock、descriptor 与 bitmap 表示同一容量。
- 源码位置：[`resize.c:1813`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/resize.c#L1813) 先增加 `s_blocks_count` 和 `s_free_blocks_count`，随后才由 `ext4_group_add_blocks()` 更新 bitmap/descriptor；失败跳到 [`resize.c:1828`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/resize.c#L1828)，没有回滚这两个 superblock 字段。
- 问题描述：group extend 在新块尚未成功发布到 bitmap/descriptor 时已增加总容量和空闲计数。真实错误返回后，superblock 与该 group 的有效 bitmap 不一致。
- 当前主证据（真实块层 EIO，2026-11-02）：QEMU `blkdebug` 让 guest 对 sector 72 的 group 0 block-bitmap READ 实际收到 `EIO`；`EXT4_IOC_GROUP_EXTEND` 返回 `EIO` 并正常卸载，`e2fsck -fn` 报 blocks 16,384–19,999 的 bitmap 差异和 free-block count 错误。guest、离线检查和 QEMU 命令见 [`PHYSICAL-IO-CORE-20261102.md`](experiments/ext4-source-fault-study/PHYSICAL-IO-CORE-20261102.md#结果)。此前 `ext4_mb_load_buddy()` 调用结果边界注入仍保留为 caller-path 语义复核，不作为块设备确实发生读错误的证据。
- 主线状态：torvalds/linux HEAD `32c52ec87d6245ee2d12ed5dd509e40062939b50` 的 `fs/ext4/resize.c` 仍先改 superblock 计数、再调用 `ext4_group_add_blocks()`，失败出口没有回滚，未修复。

### group add 失败后 resize 元数据越过尚未发布的文件系统容量（条件性源码语义 finding）

- 公式归属：本轮证据不构成 `R-BSUM`/`R-ISUM` totals mismatch：新组尚未发布，因此不应计入 active group 求和。它证明的是 failed-resize metadata persistence defect，直接离线证据为 resize inode 指向当前容量之外的块。
- 源码位置：[`resize.c:1387-1398`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/resize.c#L1387) 先设置并 dirty 新 group descriptor，再调用 `ext4_mb_add_groupinfo()`；失败返回后 `ext4_flex_group_add()` 跳到 `exit_journal` 并 stop 当前 transaction。只有成功走过该 helper 后才会调用 [`resize.c:1603`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/resize.c#L1603) 的 `ext4_update_super()` 发布新容量、group count 和全局计数。
- 问题描述：`ext4_mb_add_groupinfo()` 分配失败时，caller 返回错误但已写入的 resize metadata 仍由 transaction 提交；新 group 尚未发布，导致 resize inode 引用旧 superblock 容量之外的块，离线 checker 报告非法块引用。
- 当前有效证据：在 `ext4_mb_add_groupinfo()` 调用结果边界替代其真实分配错误 `-ENOMEM`；descriptor 和 resize-inode 更新此前已加入同一 transaction。`EXT4_IOC_GROUP_ADD` 返回 `ENOMEM`，hook 命中一次并正常卸载。raw 解码显示 superblock 仍为 32,768 blocks/inodes、1 group，而 group 1 descriptor 已写入；但未启用 descriptor 本身不作为 totals 不变量的单独证据。直接磁盘错误由 `e2fsck` 确认：resize inode 7 指向旧 superblock block count 之外的 blocks，并报告 `i_blocks` 不符。见 [`qemu-source-replay-20261101/group-add-ENOMEM.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/group-add-ENOMEM.log)、[`raw descriptor`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/group-add-ENOMEM.raw-descriptor.txt) 和 [`.e2fsck.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/group-add-ENOMEM.e2fsck.log)。注入替代调用结果，验证原调用者错误分支；不模拟真实内存耗尽。
- 块层 `EIO` 复核边界：sector 192 的真实 guest WRITE `EIO` 使 JBD2 abort，但没有得到干净卸载后的磁盘证据；它不验证这里的 `ext4_mb_add_groupinfo() == -ENOMEM` 条件。完整边界见 [`PHYSICAL-IO-CORE-20261102.md`](experiments/ext4-source-fault-study/PHYSICAL-IO-CORE-20261102.md#结果)。
- 主线状态：torvalds/linux HEAD `32c52ec87d6245ee2d12ed5dd509e40062939b50` 仍在 descriptor 已 dirty 后调用可能返回 `-ENOMEM` 的 `ext4_mb_add_groupinfo()`；该错误出口 stop journal 并返回，未撤回 transaction 中的 resize metadata；未修复。

### rmdir 错误出口父目录 nlink 未与目录项同步（条件性源码语义 finding）

- 公式归属：`R-LINK`。父目录删除一个子目录项后，`i_nlink` 应从 `old_dir_nlink` 减一（若大于 2）。
- 源码位置：上游顺序见 [`namei.c:3370`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/namei.c#L3370)：先 `ext4_delete_entry()`，清零子目录 nlink 并在 [`namei.c:3389`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/namei.c#L3389) dirty 子 inode；只有这一步成功后才在 [`namei.c:3392`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/namei.c#L3392) 调用 `ext4_dec_count(dir)`。子 inode dirty 失败直接到错误出口，没有恢复已删除的父目录项或 parent link count。
- 问题描述：子 inode link count 清零且目录项已删除后，若子 inode dirty helper 返回错误，错误出口没有恢复目录项或 parent `i_nlink`；`rmdir()` 返回错误时留下不一致状态。
- 当前主证据（真实块层 EIO，2026-11-02）：sector 352 READ 错误命中 `ext4_rmdir:3391`；`rmdir()` 返回 `EIO` 并正常卸载。离线 `e2fsck -fn` 报 unconnected directory、inode 50 link count 2 应为 1，parent inode 48 仍保留多出的 link count。guest 和离线检查见 [`rmdir-serial.log`](experiments/ext4-source-fault-study/results/physical-io-core-rmdir-real-eio-repeat-20261102/rmdir-serial.log) 与 [`rmdir-disk0-e2fsck.log`](experiments/ext4-source-fault-study/results/physical-io-core-rmdir-real-eio-repeat-20261102/rmdir-disk0-e2fsck.log)。2026-11-01 的 `-ENOMEM` 调用边界注入另行验证了该 caller 错误出口；它不声称实际发生内存耗尽。
- 主线状态：torvalds/linux HEAD `32c52ec87d6245ee2d12ed5dd509e40062939b50` 的 `fs/ext4/namei.c` 保持相同操作顺序，错误出口未回滚 parent `i_nlink`；未修复。

### __ext4_unlink() inode-table READ EIO：已认可、已修复

- 公式：`R-LINK`。旧路径删除目录项后，父 inode-table READ `EIO` 使错误出口提前返回，目标 `nlink` 未递减。
- 最终修复：v2 在删除目录项前预留父 inode 和目标 inode 的 inode-table buffer/journal write access，失败时保留名字。
- 主线 Linux 7.4-rc3 的匹配对照：未修复组卸载后无 `/p/file`、inode 2064 `Links: 1`，fsck 报 unattached；修复组返回同一 `EIO`，名字保留且 fsck 通过。两组无故障对照均通过。
- 未修复组 journal abort 探针 0 次命中，unlink 的 TID 3 正常 stop 并完成 commit。
- 最终补丁、原始日志、离线检查、构建和复现工具统一见 [最终报告](experiments/ext4-source-fault-study/UNLINK-FINAL.md)。

### rename 错误出口源/目标目录项与 inode nlink 不一致（条件性源码语义 finding）

- 公式归属：`R-LINK`。普通文件的 `i_nlink` 必须等于完整稳定命名空间中指向该 inode 的目录项数。
- 源码位置：目标不存在时，[`namei.c:4141`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/namei.c#L4141) 先插入目标目录项；随后 [`namei.c:4160`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/namei.c#L4160) 更新源 inode。若此处失败便跳到 `end_rename`，不会执行 [`namei.c:4168`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/namei.c#L4168) 的旧目录项删除。
- 问题描述：目标目录项已插入、旧目录项尚未删除时，若源 inode dirty helper 返回错误，错误出口没有撤回新目录项；`rename()` 返回错误时旧、新名字同时指向同一 inode，而 `i_nlink` 仍为 1，违反 `R-LINK`。
- 当前主证据（真实块层 EIO，2026-11-02）：sector 352 READ 错误命中 `ext4_rename:4163`；`rename()` 返回 `EIO` 并正常卸载。raw 镜像中 inode 49 的两个目录项仍同时存在而 `Links: 1`，`e2fsck -fn` 报 link count 1、应为 2。guest 与离线结果见 [`rename-serial.log`](experiments/ext4-source-fault-study/results/physical-io-core-rename-real-eio-repeat-20261102/rename-serial.log)、[`rename-disk0-e2fsck.log`](experiments/ext4-source-fault-study/results/physical-io-core-rename-real-eio-repeat-20261102/rename-disk0-e2fsck.log)、raw 目录项 [`rename-disk0-parent-p.txt`](experiments/ext4-source-fault-study/results/physical-io-core-rename-real-eio-repeat-20261102/rename-disk0-parent-p.txt) 和 inode [`rename-disk0-inode-49.txt`](experiments/ext4-source-fault-study/results/physical-io-core-rename-real-eio-repeat-20261102/rename-disk0-inode-49.txt)。源码语义结果边界注入和 setent dirty `-ENOSPC` 仍是补充 caller-path 记录；后者没有执行 JBD2 内部 credit check，也不另计根因。
- 主线状态：torvalds/linux HEAD `32c52ec87d6245ee2d12ed5dd509e40062939b50` 的 `fs/ext4/namei.c` 普通 rename 路径仍先添加目标目录项、再 dirty 源 inode，之后才删除旧目录项；`ext4_cross_rename()` 仍顺序更新两个目录项，第二次 `ext4_setent()` 失败直接退出，没有撤回第一次更新。

### mkdir 失败后的目录 inode/free-inode/link 计数不一致（条件性源码语义 finding）

- 公式归属：`R-UG`、`R-UDELTA`、`R-LINK`，属于核心公式。
- 源码位置：创建和错误清理从 [`namei.c:3173`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/namei.c#L3173) 开始；父目录 link count 在 [`namei.c:3219`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/namei.c#L3219) 增加，父 inode dirty 失败回到 inode 清理出口。mode 34 注入点为 [`namei.c:3222`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/namei.c#L3222)，定义在 [`fault-inject.c:256`](kernel/linux-6.12.1/fs/ext4/fault-inject.c#L256)；失败路径检查器在 [`checker.c:1082`](kernel/linux-6.12.1/fs/ext4/checker.c#L1082) 对比调用前父 nlink 并检查父目录是否仍含新 dirent。
- 问题描述：mkdir 在 child dirent 已插入、父目录 link count 已增加后，如果父 inode dirty 失败并进入 `out_clear_inode`，该清理会回收新目录 inode，却没有移除已发布 dirent 或撤回 parent nlink 增量。
- 源码语义主证据：在父 inode `ext4_mark_inode_dirty()` 结果边界替代返回 `-EIO`。`mkdir()` 返回 `EIO`，正常卸载后 `e2fsck -fn` 报 parent inode 13 link count 为 3、应为 2，且 `child` 指向已删除/未分配 inode 14；这仍是本条所指 `parent nlink` 已递增后显式 dirty 失败分支的精确复核。见 [`mkdir-parent-EIO.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/mkdir-parent-EIO.log) 和 [`.e2fsck.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/mkdir-parent-EIO.e2fsck.log)。
- 真实块层 EIO 补充（2026-11-02）：sector 344 READ 错误命中更早的 `add_dirent_to_buf()` parent dirty 出口；`mkdir()` 返回 `EIO`，正常卸载后 `e2fsck -fn` 报 `newdir` 指向已删除/未分配 inode。它证明相邻错误出口也能留下持久化目录项/分配不一致，但没有命中本条特指的 parent `nlink` 已递增后、显式 parent inode dirty 失败分支；精确暂停点复核没有收到 EIO、mkdir 成功。详见 [`PHYSICAL-IO-CORE-20261102.md`](experiments/ext4-source-fault-study/PHYSICAL-IO-CORE-20261102.md#结果)。
- 错误判据：失败 mkdir 不应留下已发布的 child dirent，也不应让父目录 nlink 相对调用前增加；`R_U` 与 group directory count/已分配目录 inode 仍须一致。该条件用于判断源码语义注入与块层 EIO 后的持久化状态。
- 主线状态：torvalds/linux HEAD `32c52ec87d6245ee2d12ed5dd509e40062939b50` 的 `fs/ext4/namei.c` 仍在 `ext4_inc_count(dir)` 后 dirty 父 inode；失败仍跳到清理 child 的 `out_clear_inode`，没有递减父目录 nlink。

### linkat 目录项插入后 inode nlink 未同步（条件性块层 EIO finding）

- 公式归属：`R-LINK`。稳定状态下，普通 inode 的 raw `i_nlink` 必须等于指向它的目录项引用数。
- 源码位置：`fs/ext4/namei.c:3476-3479` 的 `__ext4_link()` 先成功调用 `__ext4_add_entry()` 插入目录项，再调用 `ext4_mark_inode_dirty(handle, inode)` 写回目标 inode；该 dirty 失败时没有删除已经插入的目录项或恢复 inode link count。
- 问题描述：`linkat()` 返回错误时，目录命名空间已经增加一个引用，但目标 inode 的持久化 `nlink` 仍为旧值，形成目录项引用数与 inode link count 不一致。
- 当前证据（2026-11-16）：QEMU `blkdebug` 在 guest 已挂载并进入 syscall 窗口后，对目标 inode-table sector 368 返回真实 `EIO`。`linkat()` 返回 `EIO`，在线 checker 报 `refs_live=2`、raw `actual=1`；正常卸载后的 `e2fsck -fn` 报 inode 93 `ref count is 1, should be 2`，返回码 4。无故障控制组 `expected=2 actual=2`，`e2fsck -fn` 返回码 0。证据目录为 [`linkat-eio-precise`](work/ext4-independent-audit/runs/linkat-eio-precise/)、[`linkat-control`](work/ext4-independent-audit/runs/linkat-control/)。
- 去重边界：该路径是“新硬链接目录项已插入、目标 inode dirty 失败”，与已有 unlink/普通 rename 的目录项顺序不同；不把同一 `R-LINK` 公式按多个错误点重复计数。
- 可重复核验：运行 [`verify-independent-ext4-audit.py`](experiments/ext4-source-fault-study/scripts/verify-independent-ext4-audit.py)，会同时验证本条及另外两项独立候选。

## 2026-11-01 源码语义错误注入复核

本节逐项复核核心公式范围内除 fast-commit 外的候选。使用 Linux 6.12.1 ext4/JBD2
源码和一次性、operation-specific 注入器；每项都核对调用点、允许的 errno、实际执行的
错误分支和正常卸载后的磁盘状态。实验没有调用 `EXT4_IOC_SHUTDOWN`，也没有新增
`ext4_force_shutdown()`。

这里区分两种注入：调用边界注入以 helper 源码真实可返回的 errno 替代一次调用结果，
用于执行未经修改的 caller error branch；它不声称 helper 内部执行了清理或真实发生了
物理 I/O/内存故障。更深的 unlink 和 rename `-ENOSPC` 复核则执行相应 helper 的错误
处理路径。判为缺陷还要求错误 syscall 返回后，正常卸载的镜像仍有违反磁盘不变量的
状态。

2026-11-02 又使用 QEMU `blkdebug` 使 guest 实际块请求返回 `EIO`，结果按精确覆盖范围
补入 [`PHYSICAL-IO-CORE-20261102.md`](experiments/ext4-source-fault-study/PHYSICAL-IO-CORE-20261102.md)：
group extend、rmdir、unlink 和普通 rename 均命中对应错误位置并在正常卸载后留下磁盘差异；
mkdir 的块层 EIO 命中较早的相邻错误出口，尚未命中 parent `nlink` 增加后的精确 dirty 分支；
group add 的写 EIO 触发 JBD2 abort，不能替代其 `-ENOMEM` 分配失败条件。下方语义注入表仍用于
说明块层 EIO 未覆盖的精确 caller 分支和负向结果，不应将语义注入描述为物理块请求失败。

| 候选 | 注入语义和调用者错误路径 | 正常卸载后的证据与判定 |
|---|---|---|
| group extend | 在 `ext4_mb_load_buddy()` 调用结果边界替代 `-EIO`；它属于 helper 的真实返回集合。容量字段已先增加，原 `ext4_group_add_blocks()` 错误出口执行。 | ioctl 返回 `EIO`、命中一次、正常卸载；superblock block count 为 20,000，但 bitmap 未释放 16,384–19,999；`e2fsck` 报 bitmap 差异。**确认条件性缺陷。**见 [`group-extend-EIO.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/group-extend-EIO.log)、[`.dumpe2fs.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/group-extend-EIO.dumpe2fs.log) 和 [`.e2fsck.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/group-extend-EIO.e2fsck.log)。 |
| group add | 在 `ext4_mb_add_groupinfo()` 调用结果边界替代其真实分配错误 `-ENOMEM`；新 descriptor 已写入同一 transaction，helper 失败后原 `exit_journal` 路径仍 stop/提交该 transaction，尚未调用 `ext4_update_super()`。 | ioctl 返回 `ENOMEM`、命中一次、正常卸载；raw decode 显示 superblock 仍为 1 group，同时 descriptor/resize-inode 更新已写入。更直接地，`e2fsck` 报 resize inode 7 含超出旧 block count 的非法块并指出 `i_blocks` 不符。**确认条件性缺陷**；不能只把未启用的 group-1 descriptor 与 superblock totals 求和当作证明。见 [`group-add-ENOMEM.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/group-add-ENOMEM.log)、[`raw descriptor`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/group-add-ENOMEM.raw-descriptor.txt) 和 [`.e2fsck.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/group-add-ENOMEM.e2fsck.log)。 |
| mkdir | 在父 inode `ext4_mark_inode_dirty()` 调用结果边界替代可传播的 `-EIO`；child dirent 和 parent nlink 已更新，原 mkdir 错误清理分支执行。 | `mkdir` 返回 `EIO`、命中一次、正常卸载；child dirent 指向已删除/未分配 inode 14，parent inode 13 nlink 为 3 而应为 2。**确认条件性缺陷。**见 [`mkdir-parent-EIO.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/mkdir-parent-EIO.log) 和 [`.e2fsck.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/mkdir-parent-EIO.e2fsck.log)。 |
| rmdir | 在 child inode `ext4_mark_inode_dirty()` 调用结果边界替代可传播的 `-ENOMEM`；child link count 和目录项已清理，原 rmdir 错误分支执行。 | `rmdir` 返回 `ENOMEM`、命中一次、正常卸载；parent inode 13 nlink 为 3 而应为 2。**确认条件性缺陷。**见 [`rmdir-child-ENOMEM.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/rmdir-child-ENOMEM.log) 和 [`.e2fsck.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/rmdir-child-ENOMEM.e2fsck.log)。 |
| 普通 rename | 在源 inode `ext4_mark_inode_dirty()` 调用结果边界替代可传播的 `-EIO`；目标 dirent 已插入、旧 dirent 尚未删除，原 rename 错误分支执行。 | `rename` 返回 `EIO`、命中一次、正常卸载；同一 inode 有两个目录项引用但 nlink 为 1，`e2fsck` 报 ref count 应为 2。**确认条件性缺陷。**见 [`rename-source-EIO.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/rename-source-EIO.log) 和 [`.e2fsck.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/rename-source-EIO.e2fsck.log)。 |
| rename setent dirty `-ENOSPC` | faultable ext4 dirty-metadata wrapper 在调用边界替代 `jbd2_journal_dirty_metadata()` 的合法 `-ENOSPC` 返回，并经 ext4 通用 dirty-error handler 返回；该 hook 跳过 JBD2 函数本身，没有把 handle credits 置零。 | 原 setent/rename 错误路径执行；正常卸载后 `e2fsck` 发现同类 link-count / unattached inode mismatch。**确认同一 rename 缺陷的条件性 caller path**；不证明 JBD2 credit exhaustion 已实际发生，普通压力 workload 也未自然触发它。见 [`FINAL-AUDIT-20261031.md`](experiments/ext4-source-fault-study/FINAL-AUDIT-20261031.md) 及 [`NATURAL-RENAME-JBD2-ENOSPC-20261031.md`](experiments/ext4-counter-trace/NATURAL-RENAME-JBD2-ENOSPC-20261031.md)。 |
| inode eviction | 在最终 inode dirty 结果边界替代 `-ENOMEM`，观察到 bitmap 保留。 | `ext4_evict_inode()` 明确要求 mark-dirty 失败时不能 free inode，以免 inode number 在旧 inode 内容未安全写回前被复用。该 bitmap 保留是源码规定的保守恢复行为，**不计为缺陷**。见 [`evict-final-ENOMEM.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/evict-final-ENOMEM.log) 和 [`.e2fsck.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/evict-final-ENOMEM.e2fsck.log)。 |
| orphan-file slot | 旧的直接返回注入已撤销；新注入设置设备 writeback-error sequence 后调用真实 JBD2 write-access helper，触发原生 journal abort。 | hook 命中、正常卸载，但 `e2fsck -fn` 跳过 journal recovery 并报告未清 orphan slot；未恢复镜像不能证明已提交的独立 slot 计数缺陷。**不计为已确认 finding。**见 [`orphan-access-EIO.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101-native-jbd2/orphan-access-EIO.log) 和 [`.e2fsck.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101-native-jbd2/orphan-access-EIO.e2fsck.log)。 |
| xattr shared-block refcount | 旧的直接 `EROFS` 返回注入已撤销；新注入经设备 writeback-error sequence 进入真实 JBD2 write-access 检查并 abort。 | hook 命中、正常卸载，`e2fsck -fn` 跳过 journal recovery，未发现 `h_refcount` mismatch；镜像的 free-block 差异不能单独归因于 xattr。**不计为已确认 finding。**见 [`xattr-release-EIO.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101-native-jbd2/xattr-release-EIO.log) 和 [`.e2fsck.log`](experiments/ext4-source-fault-study/results/qemu-source-replay-20261101-native-jbd2/xattr-release-EIO.e2fsck.log)。 |

因此本轮不是“所有注入都证明漏洞”：确认 6 类持久化缺陷（其中 5 类违反核心容量/命名空间计数后置条件，group add 是 failed-resize metadata defect）；rename 的 `-ENOSPC` 合法返回值替代是同一链接计数缺陷的另一条已复现 caller path，不证明真实 credit exhaustion，也不另计根因。eviction、orphan-file 和 xattr 不进入缺陷计数。完整注入实现、构建命令和运行说明见 [`ext4-source-fault-study/README.md`](experiments/ext4-source-fault-study/README.md)。

## 旧 checker 的附加计数模型

以下计数类问题使用旧 checker 的专用模型，不属于当前 [`公式.md`](公式.md) 核心公式集。
这些模型、条目及现存材料均保留；各项历史材料的可用性由对应的证据边界说明。

### punch hole 后释放块仍被 extent tree 认领（条件性块层 EIO finding）

- 公式归属：extent ownership 与 `i_blocks`/bitmap 守恒，属于附加计数模型；不是已有 extent-grow 增深 finding 的同一源码边界。
- 源码位置：`fs/ext4/inode.c:5324-5328` 调用 `ext4_ext_remove_space()` 释放 hole 区间，随后 `inode.c:5343-5345` 调用 `ext4_mark_inode_dirty()` 持久化 inode；该 dirty 失败后没有回滚已经完成的 extent/block 释放状态。
- 问题描述：释放区间的块位图已经被清除，但 inode 的持久化 extent tree 仍可达这些数据块，形成 bitmap 与 extent ownership 不一致。
- 当前证据（2026-11-16）：真实块层 sector 368 `EIO` 命中 inode dirty 错误路径；`fallocate(FALLOC_FL_PUNCH_HOLE)` 返回 `EIO`，checker 报 `expected_free_reachable=0 actual=128`，正常卸载后 `e2fsck -fn` 报 `Block bitmap differences: +(2306--2433)`，返回码 4。无故障控制组 `actual=0`，返回码 0。证据目录为 [`punch-eio-verified3`](work/ext4-independent-audit/runs/punch-eio-verified3/)、[`punch-control-seeded`](work/ext4-independent-audit/runs/punch-control-seeded/)。
- 证据边界：EIO 由 QEMU `blkdebug` 返回给 guest，故证明的是 ext4 在真实块层读错误下的持久化错误路径；不把它表述为宿主物理介质故障。

### 碎片化 collapse range 后 extent 与 i_blocks 不一致（条件性块层 EIO finding）

- 公式归属：extent ownership/`i_blocks`，属于附加计数模型；与已有 `ext4_ext_grow_indepth()` 未发布元数据块问题分开计数。
- 源码位置：`fs/ext4/extents.c:5482-5484` 的 `ext4_ext_shift_path_extents()` 在修改 extent 后调用 `ext4_ext_dirty()`；错误返回直接退出。`ext4_collapse_range()` 的释放、移动和 inode accounting 因此可能在同一事务中分裂，缺少对已完成修改的回滚。
- 问题描述：碎片化 extent 树执行 `FALLOC_FL_COLLAPSE_RANGE` 时，区间移除/块释放已发生，后续 extent dirty 返回错误，磁盘 inode `i_blocks` 仍保留旧值，导致 raw accounting 高于 extent 可达块数。
- 当前证据（2026-11-16）：sector 368 真实 `EIO` 命中 `__ext4_ext_dirty`；`COLLAPSE_RANGE` 返回 `EIO`，checker 报 `expected_sectors=776 actual_sectors=1032`，正常卸载后的 `e2fsck -fn` 报 `Inode 94, i_blocks is 1032, should be 776`，返回码 4。无故障控制组为 `776/776`，返回码 0。证据目录为 [`collapse-fragmented-eio`](work/ext4-independent-audit/runs/collapse-fragmented-eio/)、[`collapse-fragmented-control`](work/ext4-independent-audit/runs/collapse-fragmented-control/)。
- 主线状态边界：本轮完成了本地源码快照和 guest 复现，没有据此声明最新上游已修复或未修复；源码与运行版本信息见 [`INDEPENDENT-THREE-CANDIDATES-20261116.md`](experiments/ext4-source-fault-study/INDEPENDENT-THREE-CANDIDATES-20261116.md)。

### extent tree 增深后 `i_blocks` 多计不可达元数据块

- 审阅修订 v2（2026-11-10）：按 Jan Kara 的意见，统一回收 `sb_getblk_gfp()`、`ext4_journal_get_create_access()`、`ext4_handle_dirty_metadata()` 三处失败时尚未接入树的新块；接入树后的 inode dirty 错误不释放该块。三个独立 caller 边界注入的修复前后对照均通过：未修复版各泄漏 7 块，修复版均无泄漏、正常卸载后 fsck=0，且重试、正常增深与截断通过。这不模拟真实 journal abort。正式 `[PATCH v2]`、验证证据和提交命令见 [v2 报告](experiments/ext4-invariant-checker/EXTENT-GROW-ERRORS-V2-20261110.md)。

- 本地修复与验证（2026-11-10）：`sb_getblk_gfp()` 返回 NULL 时通过 `ext4_free_blocks(..., EXT4_FREE_BLOCKS_METADATA)` 回收尚未接入树的新块并撤销记账。原版 Linux 6.12.1 在无 checker 的精确 caller 错误路径注入下复现 7 个 `40 != 32` inode 和 7 个 bitmap 孤块；相同输入的修复版仍返回 `ENOMEM`，但 inode 均为 32，正常卸载后的 `e2fsck -fn` 返回 0，失败后重试、正常增深和截断也通过。注入验证不代表 helper 内部真实发生内存耗尽。独立补丁、复现入口和证据见 [最终 v2 验证报告](experiments/ext4-invariant-checker/EXTENT-GROW-ERRORS-V2-20261110.md)。
- 公式归属：旧模型 `extent_blocks`，不属于核心公式。
- 源码位置：[`extents.c:1334`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/extents.c#L1334) 的 `sb_getblk_gfp()` 调用失败时；`ext4_new_meta_blocks()` 已在 [`extents.c:1329`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/extents.c#L1329) 分配并计费了块，但错误返回后块不会被释放。独立遍历可达 extent 并比较 `i_blocks` 的 checker 在 [`checker.c:956`](kernel/linux-6.12.1/fs/ext4/checker.c#L956)。
- 问题描述：`ext4_ext_grow_indepth()` 中 metadata block 分配已计入 inode block accounting（dquot），但 `sb_getblk_gfp()` 返回 NULL 时，函数直接返回 `-ENOMEM` 不释放已分配的块。新 extent root/index 未发布，出错返回后计费块不可从 extent tree 到达，造成 `i_blocks` 高于实际可达块数。
- 复现方式与结果：历史 syscall-only 测试用普通写入/`fallocate()` 在自然内存压力下触发 `ENOMEM`。记录的旧 checker 统计为 7 次检查、3 次违例；典型观测为预期 32 sectors、实际 40 sectors。另有 2 次 extent-map error exit 验证，其中 2 次可验证且为 `ENOMEM`。mode 25 或块设备错误仅为额外 fault 验证。2026-11-10 精准自然内存压力复现：后台持续 malloc/touch/free 高压力线程、20 个 fallocate 尝试各 20 轮增深循环，关键是**保留 inode 不删除**让 i_blocks 错误持久化。e2fsck 报告 7 个 inode（12-18）的 i_blocks 都为 40，应为 32；块位图泄漏 7 个块；checker 统计 35 次违例。见 [`experiments/ext4-invariant-checker/results/ext4-extent-grow-persistent-iblocks-20261110-113028/`](experiments/ext4-invariant-checker/results/ext4-extent-grow-persistent-iblocks-20261110-113028/)。2026-11-10 源码语义注入复现：在 `ext4_ext_grow_indepth()` 的 `sb_getblk_gfp()` 调用后强制返回 NULL（前 7 次调用），触发错误出口。workload 执行 7 个 `fallocate()` 让 extent 树增深，每次都命中注入点。checker 统计 `model_extent_blocks_violations=28`（7 个 inode × 4 次检查点），`error_exit_extent_map_verified=14`（14 次命中错误出口）。
- 错误判据：稳定 inode extent tree 遍历得到的 data+metadata+xattr blocks 换算成 sectors 后必须等于 `i_blocks`；观测到 40 vs 32，差值正好是一个 4 KiB metadata block 的 8 sectors。该独立结构重算和持久化检查记录构成错误证据。
- 离线证据：自然压力版：7 个 inode（12-18）的 `i_blocks` 都为 40，应为 32（同注入版）；块位图 7 个泄漏块（32833、33285、33290、33795、33800、33805、33810）；fsck 返回码 4。见 [`ext4-extent-grow-persistent-iblocks-20261110-113028/fsck.txt`](experiments/ext4-invariant-checker/results/ext4-extent-grow-persistent-iblocks-20261110-113028/fsck.txt)。注入版：同样 7 个 inode i_blocks=40；块位图 6 个泄漏块；见 [`extent-grow-iblocks-injected-20261110-111034/fsck.txt`](experiments/ext4-invariant-checker/results/extent-grow-iblocks-injected-20261110-111034/fsck.txt)。
- 修改代码（注入版）：在 checker 内核的 `fs/ext4/extents.c` 中，在 `sb_getblk_gfp()` 后加入计数器注入，前 7 次调用时强制 `bh = NULL`，触发既有的错误处理路径。
- 源码版本边界：上述链接固定为 v6.12.1，不能作为“当前 torvalds/linux HEAD 未修复”的证据。已有 extent v2 材料记录主线应用/编译核对，但运行对照来自 Linux 6.12.1；本次未重新核验上游最新状态。
- 证据边界：此问题属于附加计数模型，不是核心公式 finding。**自然压力已稳定复现**（精准保留 inode），源码语义注入也能稳定复现，两种方式结果一致。

### EA inode 引用在容器释放失败路径中未递减

- 公式归属：旧模型 `ea_references` 局部引用转换，不属于核心公式。
- 源码位置：EA inode 读取在 [`xattr.c:499`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/xattr.c#L499)；容器释放逐条读取并递减引用在 [`xattr.c:1262`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/xattr.c#L1262)。`iget`/数组扩容/日志 credit 等错误会记录后继续，调用方仍可能释放该 xattr 容器。当前 checker 的稳定 EA inode 引用扫描在 [`checker.c:2450`](kernel/linux-6.12.1/fs/ext4/checker.c#L2450)，mode 13 定义在 [`fault-inject.c:111`](kernel/linux-6.12.1/fs/ext4/fault-inject.c#L111)。
- 问题描述：容器中每个 EA inode 编号代表一个持有引用。容器销毁时若因 `ENOMEM` 等原因无法 iget EA inode，却继续丢弃容器，引用没有减一，EA inode 会残留引用/空间。
- 复现方式与结果：历史 syscall-only 测试通过 `fsetxattr()`/`unlink()` 和自然内存压力触发 `ENOMEM`。旧模型记录 1,410 次检查、150 次违例；典型记录为 `source=xattr_container_release_iget`、预期引用变化 `-1`、实际 `0`、错误 `-ENOMEM`。mode 13 是额外 fault 验证。
- 错误判据：容器删除使其持有引用数减一；因此同一事务/完成操作中 EA inode reference count 若没有相应减一，等式 `expected_ref_delta=-1, actual_ref_delta=0` 直接显示引用泄漏。
- 证据边界：历史专用运行材料已清理，保留数字来自清理前 checker 汇总。该旧 checker finding 不应误报成核心公式覆盖。

## 非计数后置条件问题

这些问题不属于 `公式.md` 定义的计数关系。下面保留曾经观察到的 syscall 结果；被清理的 campaign 原始日志不再可供逐条重放。

### inline data 转 extent 失败后旧文件数据丢失（历史已修复）

- 公式归属：非计数数据保留后置条件。
- 源码位置：当前转换入口 [`inline.c:733`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/inline.c#L733)，转换及分配失败清理在 [`inline.c:567`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/inline.c#L567)；另一目录转换实现及 block mapping 在 [`inline.c:1215`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/inline.c#L1215)。故障注入点 mode 31 在 [`inline.c:642`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/inline.c#L642)。历史 Oops 位于当时源码 `fs/ext4/inline.c:235`。
- 问题描述：内联数据已销毁、转 extent 分配随后失败时，失败清理未保留原数据，用户写入失败却可能破坏原文件内容。
- 复现方式与结果：历史普通 `pwrite()`/自然 `ENOSPC` 复现记录了旧数据丢失；mode 31 只验证销毁 inline 数据后分配失败的状态转换。独立 syzkaller 输入 `pwrite64(..., len=0x10f3, offset=0x1ffffffffff)` 还曾触发长度截断导致的 Oops。
- 错误判据：失败的扩容/转换不能改写操作前已提交的数据；重新读取原文件应与写前字节完全相同。写调用失败而旧内容不可恢复即证明数据保留后置条件被违反。历史 Oops 的根因是长度参数截断，不是计数错误。
- 状态与边界：上游提交 `227cb4ca5a6502164f850d22aec3104d7888b270`（6.12.y 对应 `5766da2237e539f259aa0e5f3639ae37b44ca458`）将 `ext4_prepare_inline_data()` 长度参数改为 `loff_t`；项目开发树已包含修复，补丁重放、重建和短测通过。原始 Oops/campaign 材料已清理。

## 已修复的非公式历史 Oops

syzkaller 曾以 `pwrite64(fd, buf, 0x10f3, 0x1ffffffffff)` 触发 inline-data 长度溢出。调用经 `ext4_da_write_end()` 进入 `ext4_write_inline_data()`，在旧源码 `fs/ext4/inline.c:235` 命中 `BUG_ON(pos + len > i_inline_size)`。这是 inline 写入问题，与计数探针无关。上游提交 `227cb4ca5a6502164f850d22aec3104d7888b270`（stable 6.12.y 对应 `5766da2237e539f259aa0e5f3639ae37b44ca458`）将 `ext4_prepare_inline_data()` 长度参数从 `unsigned int` 改为 `loff_t`，避免偏移加长度截断，使请求进入已有的 `-ENOSPC` 转换路径。项目开发树包含该修复，补丁重放、重建和修复后短测通过；原始 Oops campaign 和独立调查材料已清理。

## 审计证据索引

- 旧 syscall-only 核心公式结果和各压力路径细节保留在本清单对应条目中。该旧批次的总体验证结论为 `observed_mismatches=0`、`namespace_mismatched=0`、离线 bitmap/descriptor 与 `R-UG`/`R-LINK` 检查通过，`e2fsck -fn` 无诊断；2026-11-01 源码语义注入结果见上一节。
- 自然配额拒绝补测：块和 inode 配额均自然返回 `EDQUOT`，失败出口预期/实际相等，稳定 checker 与离线 fsck 未报错；结果见 inode 分配与 block reservation 对应条目。
- 旧 EA/extent/reservation 附加模型、测试和现存证据继续保留；部分历史运行目录此前已清理，具体边界见各条目。

## 2026-10-26 旧定点错误注入复核（项目既有框架）

### 注入位置与错误语义

这 11 项各自使用一个 guest worker、一个目标 workload PID 和 `count=1`。注入点位于
真实 ext4/JBD2 metadata 错误边界，而非 syscall wrapper：
`ext4_journal_get_write_access_faultable()`、
`ext4_handle_dirty_metadata_faultable()`、
`ext4_fault_inject_handle_error()` 最终都将 `-EIO` 交给 JBD2 abort/handle error
处理；inode dirty hook 在 `__ext4_reserve_inode_write()` 的 inode-table write-access
位置命中。由此 syscall errno 按调用链自然传播，`void` helper 和 eviction 路径仍可能
返回成功。fixture 的返回值来自 syscall trace，不用 hook errno 代替 syscall errno。

对每份结果重新收集后，11 项都 arm/hit 一次，真实 syscall trace 与计划返回相符。
raw `e2fsck -fn` 和独立 raw disk checker 均在未执行修复前通过；最终检查同样通过。
故障后稳定快照的双读内容彼此一致，但 journal abort 会让公式/namespace `valid=false`；
这些是挂载中止后的内存中间态，不能当作 raw 持久化损坏。新的分类为
`injected_journal_abort_no_persistent_damage`，`finding_reproduced=false`。

| hook | syscall 的实测返回 | 故障后运行时观察 | 卸载前 raw 状态 / 结论 |
|---|---|---|---|
| `mballoc-free-mark` | `fallocate=-1/EROFS` | target extent count 为 0、`st_blocks=2048`；稳定公式不可用 | fsck 和 bitmap/descriptor checker clean；仅内存中间态 |
| `inode-evict-truncate-start` | `close=0` | `R-IEVICT` mismatch，inode 仍在内存扫描中 | raw clean；不确认 bitmap 持久泄漏 |
| `inode-evict-final-dirty` | `close=0` | `R-IEVICT` mismatch；descriptor/global free delta unverified | raw clean；本定点注入不确认持久泄漏 |
| `orphan-file-delete-access` | `close=0` | abort 后 orphan 内存视图无效 | raw orphan file 与 fsck clean；不确认 slot 持久泄漏 |
| `xattr-release-access` | `close=0` | detach event 实测 xattr ref delta `0`，期望 `-1` | raw xattr/disk checker clean；运行时 mismatch 未落到镜像 |
| `xattr-release-free-block` | `close=0` | release/detach 的内存采样显示 delta `0`，期望 `-1` | raw bitmap、descriptor 与 fsck clean；运行时 mismatch 未落到镜像 |
| `rmdir-child-dirty` | `rmdir=-1/EIO` | target inode 仍可见，`nlink=0`、size `0` | raw dirent/inode、fsck 和 disk checker clean |
| `rmdir-parent-dirty` | `rmdir=-1/EIO` | target inode 仍可见，`nlink=0`、size `0` | raw dirent/inode、fsck 和 disk checker clean |
| `unlink-dirent-dirty` | `unlink=-1/EIO` | fixture 仍能观察 target inode，`nlink=1` | raw namespace/fsck/disk checker clean |
| `rename-dir-finish` | `rename=-1/EIO` | fixture 观察 source 仍在、destination 未出现；abort 后快照 invalid | raw namespace/checksum/fsck/disk checker clean |
| `rename-delete-old` | `rename=-1/EROFS` | source/destination 均存在于 fixture 观察中，destination inode 的内存 nlink 为 0 | raw namespace、link count、fsck/disk checker clean |

上述 11 个定点运行目录已按历史材料清理要求删除；本表是它们唯一保留的结论摘要，
不再为同一 finding 保留重复的逐目录证据。分类统一为
`injected_journal_abort_no_persistent_damage`，原始 syscall errno、内存 mismatch 和
raw 检查结果只以本表及当前审计报告中的汇总为准。

### 与自然 syscall 证据的边界

本节只描述项目既有框架在 2026-10-26 的 fault-only 结果，不描述前面的源码语义注入或
2026-11-02 块层 EIO 实验。旧 forced-shutdown/close 的探索性运行目录已按清理要求删除；
前面 inode eviction、orphan-file 和共享 xattr refcount 条目只保留结论摘要，不再把旧
运行目录或在线 checker 输出作为当前可复查证据。`rmdir`、`unlink`、`rename` 的历史
forced-shutdown 运行时观察也不作为当前 finding 证据；持久化结论以对应的正常卸载后
源码语义复核和块层 EIO 记录为准。

因此，2026-10-26 这轮旧框架的 11 个真实语义 hook **没有复现 11 项持久化漏洞，也不能单独把它们归并成
8 个已确认漏洞族**。`mballoc-free-mark`、`xattr-release-free-block`、
`rename-delete-old` 以及目录 rename finish 的持久化缺陷结论均未被这些新 raw 结果支持；
它们目前是精确错误路径上的运行时观察或待自然触发验证的候选。自然 syscall finding
应按前文各自保留的原始记录独立评级。没有使用 `e2fsck -fy` 修复上述 11 份镜像，
因为 raw pre-recovery 检查本来就是 clean；旧 fault-only 结果中把高层直接返回错误后
残留状态当成真实 journal 错误持久化损坏的结论作废。

### fast-commit replay 对照

`fast_commit_inode_claim` 使用两次启动的 replay fixture：第一次建立 fast-commit 并停止，
第二次 mount 时预置 `operation=fast_commit_inode_claim, errno=-EIO`。命中后
`ext4_mark_inode_used()` 在读取或修改 inode bitmap 前返回 `-EIO`；trace 记录该作用域以
`result=-5` 结束，失败作用域内没有 bitmap、descriptor 或 global 计数变化。后续 fast-commit
tag 对同一 inode 再次进入 claim 并完成更新。`fc-verify`、重挂载、稳定快照、离线 fsck
和 disk checker 均通过，因此这是可恢复的 replay 错误路径，不是 finding。该次验证目录已
按历史材料清理要求删除；结论仅保留在本节和错误注入计划中，不作为 finding 证据。验证器
也已改为按源码的“bitmap 读取前早退”语义判断；旧检查曾错误要求失败调用产生 descriptor
错误事件。

### 88 个错误注入 hook 的源码语义复核

`experiments/ext4-counter-trace/fault-errno-catalog.json` 中的 88 个操作逐项与 enum、运行时
名称解析和 ext4 C 调用点对应复核。当前落点中需要特别区分的是：inline 写入 hook 覆盖两个
`ext4_block_write_begin()` 调用点，journaled 转换在 hook 前已销毁 inline 表示，delayed-
allocation 转换则保留 inline 表示；两个路径都在 block mapping 准备前返回，清理流程不同。
catalog 和注入计划已按这两个源码分支修订。mkdir child/parent dirty hook 只落在
`ext4_mkdir()` 中对应的显式 inode-dirty 阶段；group-add descriptor-access hook 不再覆盖
superblock buffer。fast-commit hook 则在 `ext4_read_inode_bitmap()` 前返回。其余 hook 均按
catalog 所列的 helper 返回边界、errno 来源和调用者错误处理解释；命中只证明注入的错误
路径被执行，不证明实际发生了 I/O、分配器或 JBD2 资源故障。

逐项对照 pristine 6.12.1 调用流程时又发现两个会改变调用者语义的开发树偏差，并已修正：
`inode_evict_group_desc_access` 不应在 descriptor access 返回错误后额外提前退出；原源码仍会
清 inode bitmap bit、跳过 descriptor/global 计数发布并尝试 dirty bitmap。`migrate_swap_inode_data`
则必须替换原有的无条件 inode-dirty 调用，保留 `free_ind_block()` 的先发错误优先级和原有
journal-credit 配额；不能只在前一步成功时注入，也不能为让 hook 命中而扩大发起事务的
credit 数。对应说明已同步到 hook catalog 和 `FAULT-INJECTION-PLAN.md`。
