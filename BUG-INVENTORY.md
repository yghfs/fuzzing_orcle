# ext4 Bug Inventory

本清单记录普通 syscall、自然资源压力和恢复测试中观察到的问题，以及历史修复状态。
证据包含稳定快照或离线一致性检查。运行范围和原始材料的可用性见
[实现状态](experiments/ext4-counter-trace/IMPLEMENTATION-STATUS.md)。源码引用均指向当时核对的版本。

## 核心计数公式

### fast-commit replay 新目录后 group directory count 未增加（已确认）

- 公式归属：`R-UG`，稳定运行时的 group `bg_used_dirs_count` 必须等于该 group 中已分配目录 inode 数；该关系也在离线磁盘检查中独立验证。
- 源码路径：`ext4_fc_replay_create()` 重放 directory `CREAT` 时调用 `ext4_mark_inode_used()`、初始化 `.`/`..` 并链接目录，见 [`fast_commit.c:1652`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/fast_commit.c#L1652) 和 [`fast_commit.c:1666`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/fast_commit.c#L1666)。[`ialloc.c:807`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/ialloc.c#L807) 的 `ext4_mark_inode_used()` 设置 inode bitmap 并递减 `bg_free_inodes_count`，但没有检查恢复 inode 的 mode 或增加 `bg_used_dirs_count`。普通 inode 分配路径则在 [`ialloc.c:1291`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/ialloc.c#L1291) 对目录增加 group 目录数。
- 问题描述：崩溃恢复通过 fast commit 将新目录 inode 和目录项 replay 到文件系统后，该 inode 成为已分配目录，但所属 group descriptor 的 `bg_used_dirs_count` 没有相应增加。随后 mount 初始化的 `s_dirs_counter` 也以少计后的 group descriptor 求和为来源；稳定 `R-UG` 检查会发现它与 inode mode 扫描不符。
- 自然触发与证据：负载先建立全提交基线，再执行 `mkdir child`，在新目录内创建文件并 `fsync()`，host 随即硬停止 VM；第二次 mount 执行 fast-commit replay。ext4 标准 trace 明确记录 `tag 3 (EXT4_FC_TAG_CREAT), ino 14`，checker trace 显示目录 inode 14 被 replay claim 并初始化。重启后的 syscall 验证目录及文件可见。稳定内存快照报告 group 0 `bg_used_dirs_count=3`、inode mode scan `=4`；同一镜像离线 checker 得到 `descriptor_dirs=3`、`inode_dirs=4`，`e2fsck -fn` 报 `Directories count wrong for group #0 (3, counted=4)`，退出码 4。
- 主线状态：审计时从 torvalds/linux `master` 获取的 HEAD 为 `0fcb05b72344f40806cbe810de8872f1698bf053`（2026-10-14）。该版本 `fs/ext4/ialloc.c:761` 的 `ext4_mark_inode_used()` 仍只减少 free inode 计数，没有增加 used directory 计数；`fs/ext4/fast_commit.c:2031-2078` 的 `ext4_fc_replay_create()` 也没有补做该更新，故该具体问题在检查的主线 HEAD 中尚未修复。
- 证据：2026-11-24 复核返回 `finding_reproduced=true, issues=[]`。该次 replay 也观察到 directory link-count 差异，本条按 `R-UG` 计为一个 finding。原始 trace、镜像和离线报告已清理，观测摘要见 [finding report](experiments/ext4-counter-trace/FAST-COMMIT-DIR-COUNT-FINDING.md)。

### 自然配额拒绝对照

普通 syscall 压力测试分别用尽块配额和 inode 配额并收到 `EDQUOT`。失败出口的
预期与实际计数一致，稳定快照和离线 `e2fsck` 未发现不一致。

## 附加计数模型

### extent tree 增深后 `i_blocks` 多计不可达元数据块

- 公式归属为 `extent_blocks`，检查 inode 的可达块与 `i_blocks`。
- `ext4_ext_grow_indepth()` 先分配并计入新的元数据块。如果获取该块的 buffer 失败，Linux 6.12.1 的错误出口在新块接入树前返回 `ENOMEM`，没有撤销此前的分配。参见 [extents.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/extents.c#L1329)。
- 历史自然内存压力测试观察到 7 个 inode 的 `i_blocks` 为 40 sectors，各自的可达块只占 32 sectors。正常卸载后，`e2fsck -fn` 报告这些计数差异和 7 个块泄漏，退出码为 4。该结论来自历史报告。

### EA inode 引用在容器释放失败路径中未递减

- 公式归属为 `ea_references`，检查 EA inode 引用数。
- xattr 容器释放时需要逐条读取 EA inode 并递减引用。部分读取或分配错误可以让容器释放继续而对应引用数未减少。参见 [xattr.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/xattr.c#L1262)。
- 历史自然内存压力测试中，旧 checker 完成 1,410 次检查，报告 150 次违例。典型事件为 `xattr_container_release_iget` 返回 `ENOMEM`，预期引用增量为 `-1`，实际为 `0`。数字来自历史汇总，原始运行已清理。

## 非计数后置条件问题

历史普通写入测试观察到 inline data 转 extent 时，分配返回 `ENOSPC` 后旧文件数据未保留。
原始运行已清理。转换和分配失败清理参见 [inline.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/inline.c#L567)。

## 已修复的非公式历史 Oops

syzkaller 曾以 `pwrite64(fd, buf, 0x10f3, 0x1ffffffffff)` 触发 inline-data 长度溢出。调用经 `ext4_da_write_end()` 进入 `ext4_write_inline_data()`，在旧源码 `fs/ext4/inline.c:235` 命中 `BUG_ON(pos + len > i_inline_size)`。这是 inline 写入问题，与计数探针无关。上游提交 `227cb4ca5a6502164f850d22aec3104d7888b270`（stable 6.12.y 对应 `5766da2237e539f259aa0e5f3639ae37b44ca458`）将 `ext4_prepare_inline_data()` 长度参数从 `unsigned int` 改为 `loff_t`，避免偏移加长度截断，使请求进入已有的 `-ENOSPC` 转换路径。项目开发树包含该修复，补丁重放、重建和修复后短测通过；原始 Oops campaign 和独立调查材料已清理。

## 历史修复

2026-11-17 项目记录确认 unlink 错误处理修复已获认可。修复在删除目录项前为父 inode
和目标 inode 预留 journal 写入权限，避免后续 inode-table I/O 错误导致目录项与链接数不同步。
