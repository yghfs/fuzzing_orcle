# __ext4_unlink()：最终修复与验证证据

状态：漏洞已获认可，修复已完成（2026-11-17 项目状态更新）。
本报告是该漏洞的唯一说明入口。

## 问题与修复

`__ext4_unlink()` 先删除目录项，再更新父 inode，最后递减目标 inode 的 `nlink`。
若父 inode-table 读取返回 `EIO`，旧错误出口会在递减前退出。
在 `errors=continue` 下，该读取错误可以不触发 journal abort，目录项删除仍被提交，
正常卸载后目标 inode 因 `nlink=1` 却无目录项引用而成为 unattached inode。

最终 v2 在删除目录项前为父 inode 和目标 inode 调用 `ext4_reserve_inode_write()`，
预读 inode-table buffer 并取得 journal write access；失败时提前返回，保留名字。
所有退出路径释放预留引用；fast-commit replay 跳过父目录更新时只预留目标 inode。
引入错误提前返回行为的提交为 `4209ae12b122`（`ext4: handle ext4_mark_inode_dirty errors`）。

- [最终邮件补丁](submissions/unlink-v2-20261117.patch)。
- [主线 v2 补丁](patches/mainline-aebb7274-ext4-unlink-reserve-inode-write-access-v2.patch)。
- [Linux 6.12.1 v2 补丁](patches/0002-ext4-unlink-reserve-inode-write-access-v2.patch)。

## 修复前后对照

共同基线为 torvalds/linux `aebb7274476403040d582c61478df8f4a7bf9cd3`
（Linux 7.4-rc3）。未修复内核未启用 checker 或源码 fault hook；修复内核只加入 v2 修改。
输入由 `mke2fs`/`debugfs` 从零生成且通过 `e2fsck -fn`。
父 `/p` 是 inode 13，目标 `/p/file` 是 inode 2064；父 inode-table 位于 block 289 / sector 2312。
QEMU `blkdebug` 在目标 syscall 窗口让一次 guest READ 返回 `EIO`。

| 检查 | 未修复内核 | v2 修复内核 |
| --- | --- | --- |
| 故障组 unlink | 返回 `EIO` | 返回 `EIO` |
| 正常卸载后 `/p/file` | 已删除 | 保留 |
| 目标 inode | `Links: 1` | `Links: 1` |
| 故障组 `e2fsck -fn` | `Unattached inode 2064` | 通过 |
| 无故障对照 | unlink 成功、fsck 通过 | unlink 成功、fsck 通过 |

未修复故障组的 JBD2 trace 记录 `JBD2_ABORT_HITS=0`；unlink handle 属于
TID 3、`T_RUNNING`、`h_err=0`、`h_aborted=0`，stop 返回 0，随后 TID 3 完成 commit。
因此最终证据是 journal write access 之前的实际 inode-table READ EIO；
合成的 write-access 返回注入不能替代这条持久化证据。
修复版内核完整构建及 `fs/ext4/namei.o` 的 `W=1` 构建通过。

## 最终原始证据

未修复组：[完整目录](results/unlink-mainline-fresh-clean-20261116/)。

- [干净输入检查](results/unlink-mainline-fresh-clean-20261116/seed-e2fsck.log)、[源码与构建身份](results/unlink-mainline-fresh-clean-20261116/kernel-source.txt)。
- [guest/JBD2 trace](results/unlink-mainline-fresh-clean-20261116/serial.log)、[QMP 记录](results/unlink-mainline-fresh-clean-20261116/qmp-transcript.jsonl)、[块请求统计](results/unlink-mainline-fresh-clean-20261116/qmp-blockstats.json)。
- [离线目录](results/unlink-mainline-fresh-clean-20261116/disk0-parent-p.txt)、[目标 inode](results/unlink-mainline-fresh-clean-20261116/disk0-target-inode-2064.txt)、[故障组 fsck](results/unlink-mainline-fresh-clean-20261116/disk0-e2fsck.log)。
- [无故障 guest](results/unlink-mainline-fresh-clean-20261116/control-serial.log)、[无故障 fsck](results/unlink-mainline-fresh-clean-20261116/control-e2fsck.log)、[产物哈希](results/unlink-mainline-fresh-clean-20261116/artifact-hashes.sha256)。

修复组：[完整目录](results/unlink-mainline-fixed-20261117/)。

- [guest trace](results/unlink-mainline-fixed-20261117/serial.log)、[QMP 记录](results/unlink-mainline-fixed-20261117/qmp-transcript.jsonl)。
- [离线目录](results/unlink-mainline-fixed-20261117/disk0-parent-p.txt)、[目标 inode](results/unlink-mainline-fixed-20261117/disk0-target-inode-2064.txt)、[故障组 fsck](results/unlink-mainline-fixed-20261117/disk0-e2fsck.log)。
- [无故障 guest](results/unlink-mainline-fixed-20261117/control-serial.log)、[无故障 fsck](results/unlink-mainline-fixed-20261117/control-e2fsck.log)。
- [完整构建](results/unlink-mainline-fixed-20261117/patched-kernel-build.log)、[W=1 构建](results/unlink-mainline-fixed-20261117/patched-namei-w1-build.log)、[产物哈希](results/unlink-mainline-fixed-20261117/artifact-hashes.sha256)。

修复组哈希清单中的邮件补丁记录于添加 `Signed-off-by` 之前；当前邮件仅多出该签署行，
移除该行后与原 SHA256 完全一致，三份主线 v2 补丁的代码 diff 相同。其余 13 项产物哈希均匹配。

上述原始证据由 Git 忽略，保存在本机。复现工具保留为
[输入构建脚本](scripts/build-unlink-mainline-seed.sh)、
[故障运行器](scripts/run-unlink-mainline.py)（修复组使用 `--expect-fixed`）、
[无故障对照](scripts/run-unlink-mainline-control.py) 和 [guest init](initramfs/unlink-mainline-init)。
复跑应使用新的 `--results` 目录以保留最终证据。
