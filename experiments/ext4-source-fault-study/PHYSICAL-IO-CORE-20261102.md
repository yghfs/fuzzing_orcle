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
| unlink | 父 inode-table READ EIO | 已认可、已修复；主线修复前后及无故障对照见 [最终报告](UNLINK-FINAL.md)。 | 最终证据统一由该报告维护。 |
| 普通 rename | sector 352 READ 返回 EIO，命中 source inode 49 的 inode-table block 44；`ext4_rename:4163` 的 dirty 失败出口执行。 | `rename /p/file /p/to` 返回 `EIO`，正常卸载。目录仍有 `file` 和 `to` 两项指向 inode 49，而 raw inode `Links: 1`；`e2fsck -fn` 报 inode 49 link count 为 1、应为 2。 | **精确块层 EIO 复现并持久化。** |
| group add | sector 192 WRITE 返回 EIO，JBD2 随即记录 `Aborting journal`；ioctl 返回 `EIO`。 | 没有干净的正常卸载后离线证据；日志显示全局 journal abort。 | **不验证 inventory 中的 group-add finding。**该 finding 的必要条件是 `ext4_mb_add_groupinfo()` 分配失败返回 `-ENOMEM`，并非 I/O helper 返回 `-EIO`；已有的 `-ENOMEM` caller-path 语义注入证据仍是相关证据。 |

## unlink 最终结果

该漏洞已获认可并修复。最终主线修复前后对照、JBD2 事务提交证据、v2 补丁及复现工具
统一见 [UNLINK-FINAL.md](UNLINK-FINAL.md)。本文保留其他错误路径的独立块层验证。

## 复现矩阵结论

五项核心容量/命名空间计数 finding（group extend、mkdir、rmdir、unlink、普通 rename）中，group extend、rmdir、unlink、普通 rename 共 **4 项**通过块层 EIO 命中 inventory 描述的精确错误位置，并在正常卸载后的镜像上看到对应不一致。mkdir 的精确 parent-dirty 分支尚未由块层 EIO 命中；不过更早的 `add_dirent_to_buf()` 错误出口已经由块层 EIO 留下可由 `e2fsck` 发现的悬空目录项。group add 的失败原因是内存分配错误，不属于可由块 I/O EIO 等价验证的条件。eviction、orphan-file 和 xattr 条目在 inventory 中已降级或未确认，不列入这五项 finding 的复核计数。

## 原始证据

- group extend：[`group-extend-serial.log`](results/physical-io-core-20261102-verified/group-extend-serial.log)、[`group-extend-disk0-e2fsck.log`](results/physical-io-core-20261102-verified/group-extend-disk0-e2fsck.log)、[QEMU 命令](results/physical-io-core-20261102-verified/group-extend-qemu-command.json)。
- mkdir 相邻出口：[`mkdir-serial.log`](results/physical-io-core-mkdir-real-eio-check-20261102/mkdir-serial.log)、[`mkdir-disk0-e2fsck.log`](results/physical-io-core-mkdir-real-eio-check-20261102/mkdir-disk0-e2fsck.log)。精确分支未命中尝试：[`mkdir-serial.log`](results/physical-io-core-mkdir-real-eio-exact-20261102/mkdir-serial.log)、[`mkdir-disk0-e2fsck.log`](results/physical-io-core-mkdir-real-eio-exact-20261102/mkdir-disk0-e2fsck.log)。
- rmdir：[`rmdir-serial.log`](results/physical-io-core-rmdir-real-eio-repeat-20261102/rmdir-serial.log)、[`rmdir-disk0-e2fsck.log`](results/physical-io-core-rmdir-real-eio-repeat-20261102/rmdir-disk0-e2fsck.log)、[`QEMU 命令`](results/physical-io-core-rmdir-real-eio-repeat-20261102/rmdir-qemu-command.json)。
- rename：[`rename-serial.log`](results/physical-io-core-rename-real-eio-repeat-20261102/rename-serial.log)、[`rename-disk0-e2fsck.log`](results/physical-io-core-rename-real-eio-repeat-20261102/rename-disk0-e2fsck.log)、[`rename-disk0-parent-p.txt`](results/physical-io-core-rename-real-eio-repeat-20261102/rename-disk0-parent-p.txt)、[`rename-disk0-inode-49.txt`](results/physical-io-core-rename-real-eio-repeat-20261102/rename-disk0-inode-49.txt)、[`QEMU 命令`](results/physical-io-core-rename-real-eio-repeat-20261102/rename-qemu-command.json)。
- group add：[`group-add-serial.log`](results/physical-io-core-20261102/group-add-serial.log) 记录 sector 192 写错误及 JBD2 abort；这次运行未生成干净卸载后的 fsck 结果。

通用 runner [`run-physical-io-core.py`](scripts/run-physical-io-core.py) 支持 group extend、mkdir、rmdir、rename 和 group add。unlink 使用单独的 QEMU/blkdebug 运行记录。所有 guest-visible EIO 都由 QEMU 块层注入，故这些结果证明 ext4 对块设备 EIO 的错误路径行为，不证明某个宿主物理盘发生了介质故障。
