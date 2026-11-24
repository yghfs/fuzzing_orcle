# ext4 计数公式测试

当前测试以根目录 [`公式.md`](../公式.md) 的计数关系为准，覆盖 block/cluster、inode、
directory inode、global 与 descriptor 的一致性，以及稳定目录引用和 inode 链接数。
普通一致性回归使用统一内核。已观测问题、触发条件和证据见 [BUG-INVENTORY.md](../BUG-INVENTORY.md)。

统一 checker 同时检查 EA inode 引用、extent reachability / `i_blocks`、reservation
等附加计数模型。
历史运行材料的可用性以 `BUG-INVENTORY.md` 各条目的证据边界为准；附加模型的
统计不代表核心公式覆盖。

## 普通回归

统一内核基于官方 Linux 6.12.1，由 `CONFIG_EXT4_COUNTER_TRACE` 启用。
运行前先按[根目录说明](../README.md)准备宿主和 guest：

```sh
bash experiments/ext4-counter-trace/scripts/build.sh --normal
bash experiments/ext4-counter-trace/scripts/run-smoke.sh
bash experiments/ext4-counter-trace/scripts/run-chain.sh
```

公式追踪的入口与覆盖说明见
[`experiments/ext4-counter-trace/README.md`](../experiments/ext4-counter-trace/README.md)
和 [`COUNTER-TRACE-IMPLEMENTATION.md`](../COUNTER-TRACE-IMPLEMENTATION.md)。

以下定向回归默认使用 `build/linux-counter-trace-normal` 的内核和
`work/debian-normal.img`。缺少默认镜像时，运行 `bash tools/create-vm-image.sh` 准备。

```sh
bash tests/ext4-checker-smoke.sh
bash tests/run-ext4-count-workload.sh
bash tests/run-ext4-natural-pressure.sh
bash tests/run-ext4-dir-link-count-natural.sh
bash tests/run-ext4-dir-nlink-overflow.sh
bash tests/run-ext4-es-tree-count.sh
bash tests/run-ext4-mballoc-buddy-count.sh
bash tests/run-ext4-orphan-file-count-natural.sh
bash tests/run-ext4-orphan-checkpoint.sh
bash tests/run-ext4-enospc-rollback.sh
bash tests/run-ext4-online-resize-probe.sh
bash tests/run-ext4-checker-workload.sh
```

这些脚本保留各自的镜像和内核覆盖参数。统一的路径变量见 [tools/README.md](../tools/README.md)；
使用自定义内核时，可通过 `EXT4_NORMAL_KERNEL_CONFIG` 指定对应配置，供脚本校验普通回归所需的选项。

`run-ext4-count-workload.sh` 在 regular 和 bigalloc 文件系统上执行并发写入、
fallocate、punch-hole、truncate、fsync 和打开状态下的 unlink，收集计数、内核
诊断和干净关机后的 `e2fsck` 结果。

自然压力负载在独立 ext4 盘上执行普通 create/write/fallocate/fsync/unlink，
通过真实 inode 和空间耗尽覆盖 `ENOSPC` 路径。程序接口为：

```text
ext4-natural-pressure-workload [directory] [quota: 0|1|2] [quota_device]
```

读取 `/sys/kernel/debug/ext4/<device>/invariant_summary` 获取统一 checker 的模型完成次数、
违例与 `unverified`；`invariant_check` 提供详细证据。不支持的结构或无法证明稳定视图
只能记为 `unverified`。零违例需要结合非零完成检查次数解释。

## 专项回归

EA inode 引用、extent 增深和传统 orphan 链表的专项回归也使用统一内核。

```sh
bash tests/run-ext4-ea-natural-pressure.sh
bash tests/run-ext4-extent-grow-natural-enomem.sh
bash tests/run-ext4-orphan-recovery-natural.sh
```

`run-ext4-checker-workload.sh` 可通过第一个参数选用 `tests/` 下的 `.syz` 负载。
历史运行的结论和证据范围见 [BUG-INVENTORY.md](../BUG-INVENTORY.md)。

