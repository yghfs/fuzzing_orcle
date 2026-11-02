# 无注入 rename / JBD2 `-ENOSPC` 补测（2026-10-31）

## 目的

验证普通 ext4 `rename()` 是否能在不写 fault 控制文件、不调用
`ioctl(EXT4_IOC_SHUTDOWN)` 的情况下，自然走到
`jbd2_journal_dirty_metadata()` 的 credit exhaustion 分支并返回 `-ENOSPC`。

## 负载

guest 程序为 [`natural-rename-stress.c`](tests/natural-rename-stress.c)，只使用
普通文件系统操作和 syscall：`mkdir`、`openat`、`write`、`fsync`、
`renameat2(flags=0)`、`renameat2(RENAME_EXCHANGE)`、`syncfs`、`close`，外加
正常 `mount`/`umount`。每个 worker 预先填充两个目录，然后循环执行：

- 跨目录目标已存在的替换 rename；
- 跨目录 `RENAME_EXCHANGE`；
- `RENAME_WHITEOUT`；
- 非空目录 exchange 和跨目录目录替换；
- 同目录目标已存在的替换 rename；
- 重新创建被替换的源名，并周期性 `fsync` 两个目录。

没有调用 `ioctl(EXT4_IOC_SHUTDOWN)`，也没有向
`/sys/fs/ext4/vdb/fault_inject` 写入配置。压力运行结束后正常 `sync`、卸载、重挂载、
再次卸载，host 对原始镜像执行 `e2fsck -fn` 和 raw disk 检查。

## 结果

| 运行 | 挂载模式 | 时长 / worker | renameat2 | `ENOSPC` | JBD2 `errcode -28` | fsck/remount |
|---|---|---:|---:|---:|---:|---|
| [`final-20s`](results/natural-rename-jbd2-20261031/final-20s) | ordered | 20 s / 4 | 11,622 普通 + 3,874 exchange | 0 | 0 | clean / pass |
| [`ordered-60s`](results/natural-rename-jbd2-20261031/ordered-60s) | ordered | 60 s / 4 | 33,834 普通 + 11,278 exchange | 0 | 0 | clean / pass |
| [`data-journal-30s`](results/natural-rename-jbd2-20261031/data-journal-30s) | `data=journal` | 30 s / 4 | 17,856 普通 + 5,952 exchange | 0 | 0 | clean / pass |

随后根据 JBD2 handle trace 继续调节了 workload：

| 运行 | 新增路径 | 时长 / worker | whiteout / 目录路径 | 用户态 `ENOSPC` | `jbd2_handle_stats` 最高 dirty |
|---|---|---:|---:|---:|---:|
| rich rename | `RENAME_WHITEOUT`、非空目录 exchange | 10 s / 4 | 265 / 247 | 0 | 10 / 87 credits |
| directory replace | 非空目录跨父目录替换并反向重建 | 20 s / 4 | 300 / 600 | 0 | 7 / 63 credits |
| 1 KiB no-extents | `-b 1024 -O ^extent,^64bit,orphan_file` | 30 s / 4 | 737 / 1,474 | 0 | 7 / 39 credits |

`1 KiB no-extents` 这轮的 `e2fsck -fn` 通过；项目通用 raw disk checker 因目录 inode
没有 extent tree 而返回 `unverified`，所以不把它作为完整 disk-check 通过证据，只用来
观察 credit 距离。

完整校验的 workload 的 `other_errors` 均为 0，trace buffer 丢失均为 0。serial 中只看到
压力开始/结束标记，没有出现：

```text
EXT4: jbd2_journal_dirty_metadata failed: ... errcode -28
WARN_ON ...
Aborting journal
```

JBD2 trace 还显示普通文件替换 rename 的 handle 是 `requested_blocks=63`，最高只
`dirtied_blocks=7`；whiteout 是 `87/10`。因此增加 rename 次数只增加独立 transaction，
不会让同一个 handle 的 credits 继续累积。

## 判定

这次补测**没有自然触发** `jbd2_journal_dirty_metadata()` 的 credit exhaustion。
另需更正与此压力测试相邻的 `rename-setent-dirty-enospc` 注入说明：该实验在
faultable ext4 dirty-metadata wrapper 处替代合法 `-ENOSPC` 返回，并让 ext4 通用错误
处理与原 setent/rename caller 分支运行；wrapper 在调用 JBD2 前返回，跳过
`jbd2_journal_dirty_metadata()`，也没有将 handle credits 置零。因此该注入验证的是
“dirty-metadata helper 返回 `-ENOSPC` 时 caller 的错误路径及持久化结果”，不是 JBD2
内部 credit exhaustion 本身。源码中 JBD2 确有在剩余 buffer credits 不大于零时返回
`-ENOSPC` 的分支，但本轮既未执行该分支，也未在普通压力中自然触发它。

因此该注入结果应表述为：

> 当 dirty-metadata helper 返回其合法错误 `-ENOSPC` 时，rename 的 caller 错误路径会留下持久化 namespace/link-count 不一致。

现有注入证明该条件性 caller 错误路径；在当前 Linux 6.12.1、当前
journal 配置和上述普通 syscall workload 下，不能声称普通 rename 会自然耗尽 credits。
