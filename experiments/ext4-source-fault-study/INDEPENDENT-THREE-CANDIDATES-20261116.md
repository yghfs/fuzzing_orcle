# 独立 ext4 三候选审计结果

本轮审计针对本地 `work/ext4-independent-audit` 中的 Linux ext4 快照，使用
QEMU `blkdebug` 在 guest 真实块读取上返回一次 `EIO`。故障注入在 guest 进入
目标 syscall 后才启用；每组都有无故障控制组、在线 checker、正常卸载以及
离线 `e2fsck -fn` 证据。

## 结果

| 候选                                | 代码边界                                                                                                                                          | 故障组在线结果                                                         | 离线结果                                                       | 控制组                     |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- | -------------------------------------------------------------- | -------------------------- |
| `linkat`                          | `fs/ext4/namei.c:3476-3479`，目录项插入后 `ext4_mark_inode_dirty(inode)` 出错没有撤回目录项                                                   | `linkat` 返回 `EIO`；inode 93 的两个目录项引用对应 raw `nlink=1` | `Inode 93 ref count is 1, should be 2`，`e2fsck` rc=4      | checker `2/2`，rc=0      |
| `FALLOC_FL_PUNCH_HOLE`            | `fs/ext4/inode.c:5327` 已完成 `ext4_ext_remove_space()` 后，`inode.c:5343` 的 inode dirty 持久化失败；释放的区间仍被 inode extent tree 认领 | syscall 返回 `EIO`；checker 看到可达数据块 256、应为 128             | `Block bitmap differences: +(2306--2433)`，`e2fsck` rc=4   | checker `actual=0`，rc=0 |
| 碎片化 `FALLOC_FL_COLLAPSE_RANGE` | `fs/ext4/extents.c:5482` 的 extent shift dirty 失败；此前的区间移除/块释放没有随错误回滚，导致磁盘 extent 与 inode accounting 分裂              | syscall 返回 `EIO`；raw `i_blocks=1032`，extent 可达值 776 sectors | `Inode 94, i_blocks is 1032, should be 776`，`e2fsck` rc=4 | raw `776/776`，rc=0      |

`RENAME_EXCHANGE` 的父目录计数候选没有计入三项：真实 EIO 先命中既有的
cross-rename 部分更新路径，ENOMEM 语义注入组的 raw nlink 与 fsck 均正确，
因此它被排除，避免重复计数。

## 可重复核验

只读运行证据校验器：

```bash
python3 experiments/ext4-source-fault-study/scripts/verify-independent-ext4-audit.py
```

预期输出为 3 行 `PASS` 和 `VERIFIED 3/3 independent ext4 cases`。校验器只
读取 `serial.log`，并执行 `e2fsck -fn`；不会修改镜像。

关键证据目录：

- `work/ext4-independent-audit/runs/linkat-eio-precise/`
- `work/ext4-independent-audit/runs/punch-eio-verified3/`
- `work/ext4-independent-audit/runs/collapse-fragmented-eio/`
- 对应的 `*-control` 目录保存无故障对照。

源码快照位于 `work/ext4-independent-audit/linux-23c62304dde29abe92013d85910d03a77dada35d/`，运行日志记录的 guest kernel 为 `7.4.0-rc2`。本报告把三项视为不同的 mutation/持久化边界；此前已存在的 extent-grow `i_blocks` 问题不作为本轮第三项的依据。
