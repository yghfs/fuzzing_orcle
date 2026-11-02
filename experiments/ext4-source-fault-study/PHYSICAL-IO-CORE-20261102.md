# 核心计数路径的块层 EIO 复核

日期：2026-11-02。范围是 [`BUG-INVENTORY.md`](../../BUG-INVENTORY.md) 的“核心计数公式”部分，排除 fast-commit。这里的“真实 EIO”指 QEMU `blkdebug` 对实际 guest 块请求注入 `EIO`，guest 内核记录磁盘 `I/O error` 并由文件系统读到该错误；它不是宿主物理磁盘介质或控制器故障。

判定要求：错误需发生在目标操作路径，syscall/ioctl 返回错误，文件系统正常卸载，然后对卸载后的镜像做 `e2fsck -fn` 或 raw inode/dirent 检查。单独看到内核在线计数告警、事务 abort 或未恢复镜像不算持久化复现。

## 结果

| 路径 | Guest 块请求与错误位置 | syscall、卸载和离线结果 | 结论 |
| --- | --- | --- | --- |
| group extend | sector 72 READ 返回 EIO；ext4 报 group 0 block bitmap 读取失败，`ext4_group_add_blocks()` 返回 I/O failure。 | `EXT4_IOC_GROUP_EXTEND` 返回 `EIO`，正常卸载。`e2fsck -fn` 报 blocks 16384–19999 的 bitmap 差异和 free-block count 错误；运行时 checker 也看到 superblock `blocks_count=20000`，而操作前为 16384。 | **精确块层 EIO 复现并持久化。** |
| mkdir（相邻错误出口） | sector 344 READ 返回 EIO，读取 parent inode 48 的 inode-table block 43 失败；实际错误在 `add_dirent_to_buf()` 的 parent dirty 操作。 | `mkdir /p/newdir` 返回 `EIO`，正常卸载。镜像仍有 `newdir` 目录项但 inode 50 已删除/未分配；`e2fsck -fn` 报 `Entry 'newdir' ... has deleted/unused inode 50`。 | **真实 EIO 留下持久化目录项/分配不一致，但未命中 inventory 所述“parent nlink 已递增后，显式 parent inode dirty 失败”分支。**这是更早的 mkdir 错误出口，证据不应冒充精确分支复现。 |
| mkdir（精确分支尝试） | 在 `ext4_checker_namespace_pause()` 暂停点清缓存后，对 sector 344 配置 READ EIO；本次操作没有产生 guest `I/O error`。 | `mkdir` 成功，正常卸载，`e2fsck -fn` 通过。 | **未验证精确分支。**已确认的源码语义 `-EIO` 调用结果替代证据仍适用；块层 EIO 尝试没有制造该 inode-table reread。 |
| rmdir | sector 352 READ 返回 EIO，命中 inode 50 的 inode-table block 44 读取；重复注入使 child dirty 清理后再次 reserve/write 仍失败，最终命中 `ext4_rmdir:3391`。 | `rmdir /p/subdir` 返回 `EIO`，正常卸载。`e2fsck -fn` 报 inode 50 是 unconnected directory、`..` 仍指向 parent、link count 为 2 而应为 1；parent inode 48 的 link count 仍为 3。 | **精确块层 EIO 复现并持久化。** |
| unlink | sector 2312 READ 返回 EIO，命中 parent inode 13 所在 inode-table block 289；`__ext4_unlink:3469` 的 parent inode dirty 失败出口执行。 | `unlink /p/file` 返回 `EIO`，正常卸载。raw inode 2064 保留 `Links: 1`，parent 目录已无 `file`；`e2fsck -fn` 报 `Unattached inode 2064`。相同干净镜像/错误位置的修复版对照保留名字且检查通过。 | **精确块层 EIO 复现并持久化，且有修复版对照。**详细预置状态、QMP transcript、raw inode 和修复对照见下方 unlink 证据。 |
| 普通 rename | sector 352 READ 返回 EIO，命中 source inode 49 的 inode-table block 44；`ext4_rename:4163` 的 dirty 失败出口执行。 | `rename /p/file /p/to` 返回 `EIO`，正常卸载。目录仍有 `file` 和 `to` 两项指向 inode 49，而 raw inode `Links: 1`；`e2fsck -fn` 报 inode 49 link count 为 1、应为 2。 | **精确块层 EIO 复现并持久化。** |
| group add | sector 192 WRITE 返回 EIO，JBD2 随即记录 `Aborting journal`；ioctl 返回 `EIO`。 | 没有干净的正常卸载后离线证据；日志显示全局 journal abort。 | **不验证 inventory 中的 group-add finding。**该 finding 的必要条件是 `ext4_mb_add_groupinfo()` 分配失败返回 `-ENOMEM`，并非 I/O helper 返回 `-EIO`；已有的 `-ENOMEM` caller-path 语义注入证据仍是相关证据。 |

## unlink 的 JBD2 abort 与事务状态跟踪

为回答 `jbd2_journal_abort()` 是否在 unlink 的块层 EIO 路径被调用，使用相同 clean template、sector 2312 READ EIO 和未修复 6.12.1 内核另跑一次；tracefs 在 `unlink()` 开始前启用 `jbd2_journal_abort()` kprobe、`jbd2_journal_stop()` 入口/返回探针，以及 JBD2 start/end-commit tracepoint，并持续记录到正常卸载。QMP 再次报告 sector 2312 的 guest READ `EIO`；unlink 返回 `EIO`，正常卸载后 `e2fsck -fn` 仍报 inode 2064 unattached，确认跟踪复现了同一个错误出口和磁盘差异。

跟踪结果：

- `jbd2_journal_abort()` kprobe 在覆盖 unlink、后续 `sync` 和正常卸载的窗口内 **0 次命中**；没有调用记录。`jbd2_journal_stop()` 的 `fault-workload` 入口采样到 handle 所属 TID **7**、事务状态数值 **0 (`T_RUNNING`)**、`h_err=0`、`h_aborted=0`、`j_flags=0xb0`、`j_errno=0`。`JBD2_ABORT` 是 `j_flags` 的 `0x2` 位，故采样时全局 journal 未 abort。unlink cleanup 的 `jbd2_journal_stop()` 返回 **0**。
- 后续 JBD2 tracepoint 明确记录 `jbd2_start_commit` 与 `jbd2_end_commit` 都针对 **transaction 7**；`sync` 后 `/proc/fs/jbd2/vda-8/info` 的已完成 transaction 统计从 1 增至 2。故 TID 7 不是只停留在运行态，之后确实完成提交；这与正常卸载后目录项已删除、inode 2064 仍为 `Links: 1` 的离线结果一致。
- 这次 READ 在 `ext4_get_inode_loc()` 读取 parent inode table 时失败。`ext4_reserve_inode_write()` 仅在该读取成功时才会调用 journal write-access helper；当前错误沿 `ext4_std_error()` 处理。挂载参数是 `errors=continue`，`ext4_handle_error()` 在此模式下不调用 `jbd2_journal_abort()`，而是排队更新 superblock error 状态。因此块层 READ EIO 不等价于 JBD2 本身的 write-access/writeback-error abort 路径。该结果只描述此 unlink 错误路径，不泛化为所有 unlink 错误都会避免 journal abort。
- 这不要与旧的 `unlink_parent_global_abort` 源码语义注入混为一谈：后者专门设置 writeback-error 状态并触发 JBD2 原生 abort 分支，日志确有 `Aborting journal`，但它不是这次父 inode-table READ 块层 EIO 的执行路径，也没有本次的调用计数探针记录。

完整 kprobe/tracepoint、事务统计、QMP 和离线镜像证据见 [`unlink-jbd2-serial.log`](results/unlink-jbd2-state-20261102/unlink-jbd2-serial.log)、[`unlink-jbd2-qmp-transcript.jsonl`](results/unlink-jbd2-state-20261102/unlink-jbd2-qmp-transcript.jsonl)、[`unlink-jbd2-qmp-blockstats.json`](results/unlink-jbd2-state-20261102/unlink-jbd2-qmp-blockstats.json)、[`unlink-jbd2-qemu-command.json`](results/unlink-jbd2-state-20261102/unlink-jbd2-qemu-command.json)、[`unlink-jbd2-input-template.sha256`](results/unlink-jbd2-state-20261102/unlink-jbd2-input-template.sha256)、[`unlink-jbd2-disk2-e2fsck.log`](results/unlink-jbd2-state-20261102/unlink-jbd2-disk2-e2fsck.log)、[`unlink-jbd2-disk2-inode-2064.txt`](results/unlink-jbd2-state-20261102/unlink-jbd2-disk2-inode-2064.txt) 和 [`unlink-jbd2-disk2-parent-p.txt`](results/unlink-jbd2-state-20261102/unlink-jbd2-disk2-parent-p.txt)。复现实验由 [`run-unlink-jbd2-trace.py`](scripts/run-unlink-jbd2-trace.py) 和 [`unlink-jbd2-trace-init`](initramfs/unlink-jbd2-trace-init) 完成。

## 复现矩阵结论

五项核心容量/命名空间计数 finding（group extend、mkdir、rmdir、unlink、普通 rename）中，group extend、rmdir、unlink、普通 rename 共 **4 项**通过块层 EIO 命中 inventory 描述的精确错误位置，并在正常卸载后的镜像上看到对应不一致。mkdir 的精确 parent-dirty 分支尚未由块层 EIO 命中；不过更早的 `add_dirent_to_buf()` 错误出口已经由块层 EIO 留下可由 `e2fsck` 发现的悬空目录项。group add 的失败原因是内存分配错误，不属于可由块 I/O EIO 等价验证的条件。eviction、orphan-file 和 xattr 条目在 inventory 中已降级或未确认，不列入这五项 finding 的复核计数。

## 原始证据

- group extend：[`group-extend-serial.log`](results/physical-io-core-20261102-verified/group-extend-serial.log)、[`group-extend-disk0-e2fsck.log`](results/physical-io-core-20261102-verified/group-extend-disk0-e2fsck.log)、[QEMU 命令](results/physical-io-core-20261102-verified/group-extend-qemu-command.json)。
- mkdir 相邻出口：[`mkdir-serial.log`](results/physical-io-core-mkdir-real-eio-check-20261102/mkdir-serial.log)、[`mkdir-disk0-e2fsck.log`](results/physical-io-core-mkdir-real-eio-check-20261102/mkdir-disk0-e2fsck.log)。精确分支未命中尝试：[`mkdir-serial.log`](results/physical-io-core-mkdir-real-eio-exact-20261102/mkdir-serial.log)、[`mkdir-disk0-e2fsck.log`](results/physical-io-core-mkdir-real-eio-exact-20261102/mkdir-disk0-e2fsck.log)。
- rmdir：[`rmdir-serial.log`](results/physical-io-core-rmdir-real-eio-repeat-20261102/rmdir-serial.log)、[`rmdir-disk0-e2fsck.log`](results/physical-io-core-rmdir-real-eio-repeat-20261102/rmdir-disk0-e2fsck.log)、[`QEMU 命令`](results/physical-io-core-rmdir-real-eio-repeat-20261102/rmdir-qemu-command.json)。
- rename：[`rename-serial.log`](results/physical-io-core-rename-real-eio-repeat-20261102/rename-serial.log)、[`rename-disk0-e2fsck.log`](results/physical-io-core-rename-real-eio-repeat-20261102/rename-disk0-e2fsck.log)、[`rename-disk0-parent-p.txt`](results/physical-io-core-rename-real-eio-repeat-20261102/rename-disk0-parent-p.txt)、[`rename-disk0-inode-49.txt`](results/physical-io-core-rename-real-eio-repeat-20261102/rename-disk0-inode-49.txt)、[`QEMU 命令`](results/physical-io-core-rename-real-eio-repeat-20261102/rename-qemu-command.json)。
- unlink：[`unfixed-serial.log`](results/physical-io-20261102/unfixed-serial.log)、[`unfixed-qmp-transcript.jsonl`](results/physical-io-20261102/unfixed-qmp-transcript.jsonl)、[`unfixed-qmp-blockstats.json`](results/physical-io-20261102/unfixed-qmp-blockstats.json)、[`unfixed-disk0-parent-p.txt`](results/physical-io-20261102/unfixed-disk0-parent-p.txt)、[`unfixed-disk0-inode-2064.txt`](results/physical-io-20261102/unfixed-disk0-inode-2064.txt)、[`unfixed-disk0-e2fsck.log`](results/physical-io-20261102/unfixed-disk0-e2fsck.log)。修复对照：[`fixed-serial.log`](results/physical-io-20261102/fixed-serial.log)、[`fixed-disk0-parent-p.txt`](results/physical-io-20261102/fixed-disk0-parent-p.txt)、[`fixed-disk0-inode-2064.txt`](results/physical-io-20261102/fixed-disk0-inode-2064.txt)、[`fixed-disk0-e2fsck.log`](results/physical-io-20261102/fixed-disk0-e2fsck.log)。JBD2 abort/事务状态复核见 [`unlink-jbd2-serial.log`](results/unlink-jbd2-state-20261102/unlink-jbd2-serial.log) 和本节的其他链接。
- group add：[`group-add-serial.log`](results/physical-io-core-20261102/group-add-serial.log) 记录 sector 192 写错误及 JBD2 abort；这次运行未生成干净卸载后的 fsck 结果。

通用 runner [`run-physical-io-core.py`](scripts/run-physical-io-core.py) 支持 group extend、mkdir、rmdir、rename 和 group add。unlink 使用单独的 QEMU/blkdebug 运行记录。所有 guest-visible EIO 都由 QEMU 块层注入，故这些结果证明 ext4 对块设备 EIO 的错误路径行为，不证明某个宿主物理盘发生了介质故障。
