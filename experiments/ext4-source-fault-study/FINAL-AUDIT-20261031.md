# ext4 错误出口最终审计（2026-11-01 复核）

本报告的源码语义注入结论在 2026-11-02 由块层 EIO 实验补充；最新覆盖矩阵和原始证据
以 [`PHYSICAL-IO-CORE-20261102.md`](PHYSICAL-IO-CORE-20261102.md) 为准。两份报告的目标不同：
源码语义注入精确验证指定 caller 错误出口，QEMU `blkdebug` 则验证实际 guest 块请求失败
后 ext4 的错误路径和持久化结果。

本报告针对“只能使用正常 syscall 或符合真实 helper 语义的错误注入，不能使用
`ioctl(EXT4_IOC_SHUTDOWN)`”这一约束，复核
`experiments/ext4-source-fault-study/results/qemu-source-replay-20261101/`、
`experiments/ext4-source-fault-study/results/qemu-source-replay-20261101-native-jbd2/`
以及既有的无注入 rename stress 结果。审计没有修改 ext4 修复代码；工作区中已有的
unlink 修复只用于独立验证。

## 结论

当前证据支持 **6 类明确的持久化代码缺陷**：group extend、group add、`mkdir()`、
`rmdir()`、`unlink()` 和普通 `rename()`。此外，rename 缺陷也通过 `-ENOSPC` 调用边界
注入复现；这是同一链接计数不一致的另一条 caller-error path，不是独立根因：

1. group extend 在 `ext4_mb_load_buddy()` 失败后保留 superblock 容量增量；
2. group add 在 `ext4_mb_add_groupinfo()` 返回 `-ENOMEM` 后提交 resize metadata，造成 resize inode 指向未发布容量之外的块；
3. `mkdir()` 在 parent inode dirty 失败后保留 child dirent 和 parent nlink 增量；
4. `rmdir()` 在 child inode dirty 失败后保留 parent nlink 增量；
5. `unlink()` 在 parent inode 更新失败后删除名字但不递减 target nlink；
6. 普通 `rename()` 在 source inode dirty 失败后先发布新名字，旧名字仍在。

`-ENOSPC` 是 `jbd2_journal_dirty_metadata()` 可以返回的合法错误，但这次注入由
faultable ext4 wrapper 在调用边界替代该结果，并跳过 JBD2 函数内部的 credit check；没有
实验把 handle credits 置零，也没有执行真实 credit exhaustion。无注入压力测试尚未证明普通
rename 在没有测试注入时会自然耗尽 reserved credits。提交上游时应明确写成“条件于
dirty-metadata helper 返回 `-ENOSPC` 的 rename 错误回滚缺陷”。

这条 `-ENOSPC` 注入是普通 rename 缺陷的另一种 caller-error-path 复现，不是第七类
独立缺陷；它验证合法 helper 错误返回时原 setent/rename 错误路径及其持久化后果。

本轮已补做无注入 syscall 压力验证：默认 ordered 模式运行 20 秒和 60 秒，另以
`data=journal` 运行 30 秒；四个 worker 共执行普通替换 rename、跨目录
`RENAME_EXCHANGE`、同目录替换及 `fsync`/`syncfs`。三轮均未观察到用户态
`ENOSPC`、`EXT4: jbd2_journal_dirty_metadata ... errcode -28`、JBD2 abort 或
fsck/remount 错误。完整负载、命令和结果见
[`NATURAL-RENAME-JBD2-ENOSPC-20261031.md`](../ext4-counter-trace/NATURAL-RENAME-JBD2-ENOSPC-20261031.md)。
这把该路径的结论收紧为“当前配置和 workload 下未自然触发”，不改变已验证的条件性
caller-error-path，也不把它另计成独立根因。

随后按 JBD2 handle trace 继续增加 `RENAME_WHITEOUT`、非空目录 exchange、跨父目录
目录替换，并尝试 1 KiB 无 extents 镜像；普通 rename handle 的实际 dirty buffer
峰值仍为 7/63 credits，whiteout 为 10/87，所有 syscall 仍未返回 `ENOSPC`。

inode eviction 的 `evict-final-ENOMEM` 不再列为明确 patch finding；orphan-file 和
xattr-release 也不作为独立核心计数 finding。

## 逐项证据

| Case | 真实错误语义 | journal 状态 | 持久化结果 | 判定 |
|---|---|---|---|---|
| `group-extend-EIO` | `ext4_mb_load_buddy()` 的真实 `-EIO` 集合 | 无全局 abort | `statfs` 的 `f_blocks` 从 14,325 变为 17,941；raw/e2fsck 显示容量已覆盖 16,384–19,999，但 bitmap 仍未释放该范围，`e2fsck`: `Block bitmap differences: -(16384--19999)` | **明确** |
| `group-add-ENOMEM` | 在 `ext4_mb_add_groupinfo()` 调用结果边界替代其真实分配失败 `-ENOMEM` | 无全局 abort；原错误出口 stop 当前 transaction | superblock 仍只暴露 1 group；descriptor 和 resize-inode metadata 已 dirty。`e2fsck` 报 resize inode 7 引用旧 block count 之外的块，并报 `i_blocks` mismatch。group-1 descriptor 只是佐证，未发布 descriptor 本身不作为计数不变量错误 | **明确** |
| `mkdir-parent-EIO` | 在 `ext4_mark_inode_dirty()` 调用结果边界替代其可传播的 `-EIO`，执行原 caller 错误分支 | 无全局 abort | `e2fsck`: inode 13 ref count 3 应为 2；目录项仍指向 deleted/unused inode 14 | **明确** |
| `rmdir-child-ENOMEM` | 在 `ext4_mark_inode_dirty()` 调用结果边界替代其可传播的 `-ENOMEM`，执行原 caller 错误分支 | 无全局 abort | `e2fsck`: inode 13 ref count 3 应为 2 | **明确** |
| `unlink-parent-EIO` | inode-table/access 路径真实可返回 `-EIO`；另有 reserve/access 和 inode-read 深层复核 | 无全局 abort | inode 13 `Links: 1` 且无目录项；`e2fsck`: `Unattached inode 13` | **明确** |
| `rename-source-EIO` | 在 `ext4_mark_inode_dirty()` 调用结果边界替代其可传播的 `-EIO`，执行原 caller 错误分支 | 无全局 abort | inode 13 有两个目录项引用但 `nlink=1`；`e2fsck`: ref count 1 应为 2 | **明确** |
| `rename-setent-dirty-enospc` | faultable ext4 dirty-metadata wrapper 在调用边界替代合法 `-ENOSPC` 返回，并路由到 ext4 通用 dirty-error handler；本次 hook 跳过 `jbd2_journal_dirty_metadata()`，未执行其 credit check | 仅当前 handle abort，无全局 `Aborting journal` | inode 14 ref count 1 应为 2，inode 15 unattached | **确认同一 rename 缺陷的条件性 caller path；不证明实际 credit exhaustion** |

六类明确缺陷测试的 guest 都正常执行 `sync`、卸载和关机；结果目录中没有
`EXT4_IOC_SHUTDOWN` 调用，也没有新增 `ext4_force_shutdown()`。`rename-setent` 的
注入 errno 属于 `jbd2_journal_dirty_metadata()` 的合法返回集合，且 ext4 通用 dirty-error
handler 被执行；但该 faultable wrapper 在调用 JBD2 前返回，所以本轮没有直接走到
`fs/jbd2/transaction.c:1556` 的 credit check。无注入压力实验也未自然触发该分支。

## Eviction 的降级理由

`kernel/linux-mainline/fs/ext4/inode.c:379-386` 明确写着：最终 inode dirty 失败时
“we can't free the inode if the mark_dirty fails”，错误出口只调用
`ext4_clear_inode()`。`ext4_clear_inode()` 的注释又说明，必须先从 inode hash 中移除，
不能在 inode-table 更新失败时提前清除 bitmap，否则可能让 inode number 被复用而旧的
磁盘 inode 内容仍未安全清除。`ext4_free_inode()` 也遵循“先 clear inode，再读/修改
bitmap”的保守顺序。

因此 `evict-final-ENOMEM` 的 `Inode bitmap differences: -13` 是故障后的安全性/可
回收性代价，需要 fsck 清理，但它正是源码注释描述的保守恢复结果，不能仅凭 bitmap
未释放认定为错误。它不计入上面的 6+1。

## Orphan 和 xattr 的降级理由

本轮已撤销 orphan/xattr 的直接 helper 返回注入，改为先设置 filesystem-device
writeback-error sequence，再调用真实 JBD2 write-access helper。两项日志都确认 JBD2
原生 `Aborting journal`，之后 workload 正常结束并成功卸载。离线 `e2fsck -fn` 明确
跳过 journal recovery：orphan case 报未清 orphan slot、inode bitmap 和 free-block
等待恢复；xattr case 未报告 `h_refcount` mismatch，但有未恢复 transaction 导致的
free-block count 差异。由于镜像尚未经过 journal recovery，前者不能证明 orphan slot
已持久化为独立不一致，后者也不能把 free-block 差异归因于 xattr。故两项本轮均不计入
finding。日志见 [`orphan-access-EIO.log`](results/qemu-source-replay-20261101-native-jbd2/orphan-access-EIO.log)、
[orphan `.e2fsck.log`](results/qemu-source-replay-20261101-native-jbd2/orphan-access-EIO.e2fsck.log)、
[`xattr-release-EIO.log`](results/qemu-source-replay-20261101-native-jbd2/xattr-release-EIO.log)
和 [xattr `.e2fsck.log`](results/qemu-source-replay-20261101-native-jbd2/xattr-release-EIO.e2fsck.log)。

## Patch 标准边界

- group extend、group add 的注入点位于真实 helper 返回边界，错误码来自 helper 的
  实际返回集合；结论应写成“条件于该 helper 错误返回”。
- mkdir/rmdir/rename 的主矩阵在 `ext4_mark_inode_dirty()` 调用结果边界替代该 helper
  可传播的 errno，目的是检验原 caller error branch；这不表示 helper 内部清理已执行。
  unlink 另有 reserve/access 和 inode-read 两条更深复核，分别执行 ext4 的 abort-handle
  清理和 inode-table `read_error` 清理；reserve hook 本身仍替代 JBD2 write-access 结果。
  inode-read case 命中 uptodate cache，不表示 physical read I/O 失败。rename `-ENOSPC`
  复核则由 dirty-metadata wrapper 替代 helper 返回值，经 ext4 通用错误处理执行，不代表
  `jbd2_journal_dirty_metadata()` 的 credit-exhaustion 分支实际运行。
- 这些 finding 证明错误条件成立时的持久化一致性缺陷，不证明底层磁盘或内存错误的
  自然发生频率。提交上游时应分别说明错误条件、mutation 顺序和正常卸载后的 fsck/raw
  证据。

