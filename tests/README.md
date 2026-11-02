# ext4 计数公式测试

当前测试以根目录 [`公式.md`](../公式.md) 的计数关系为准，覆盖 block/cluster、inode、
directory inode、global 与 descriptor 的一致性，以及稳定目录引用和 inode 链接数。
普通 syscall 发现使用关闭 `CONFIG_EXT4_FAULT_INJECTION` 的内核；fault 注入仅用于
确认指定错误窗口。已观测问题的分类、置信度和 syscall/fault 条件统一保存在
[`BUG-INVENTORY.md`](../BUG-INVENTORY.md)。

旧 checker 的 EA inode 引用、extent reachability / `i_blocks`、reservation 等附加
计数模型，以及现存的相关测试入口和复现证据均保留，不属于本次清理范围。
历史运行材料的可用性以 `BUG-INVENTORY.md` 各条目的证据边界为准；附加模型的
统计不代表核心公式覆盖。

## 新公式追踪器

新工程使用独立官方 Linux 6.12.1 源码，入口与覆盖说明见
[`experiments/ext4-counter-trace/README.md`](../experiments/ext4-counter-trace/README.md)
和 [`COUNTER-TRACE-IMPLEMENTATION.md`](../COUNTER-TRACE-IMPLEMENTATION.md)。

```sh
bash experiments/ext4-counter-trace/scripts/build.sh
bash experiments/ext4-counter-trace/scripts/run-chain.sh
bash experiments/ext4-counter-trace/scripts/run-campaign.sh --duration-seconds 3600 --segments 10000
```

## 旧 checker 的公式测试入口

普通检查内核配置：

```text
CONFIG_EXT4_INVARIANT_CHECKER=y
# CONFIG_EXT4_FAULT_INJECTION is not set
```

```sh
bash tests/ext4-checker-smoke.sh
bash tests/run-ext4-checker-workload.sh
bash tests/run-ext4-count-workload.sh
bash tests/run-ext4-natural-pressure.sh
bash tests/run-ext4-dir-link-count-natural.sh
bash tests/run-ext4-syscall-candidate-scan.sh
```

`run-ext4-count-workload.sh` 在 regular 和 bigalloc 文件系统上执行并发写入、
fallocate、punch-hole、truncate、fsync 和打开状态下的 unlink，收集计数、内核
诊断和干净关机后的 `e2fsck` 结果。

自然压力负载在独立 ext4 盘上执行普通 create/write/fallocate/fsync/unlink，
通过真实 inode 和空间耗尽覆盖 `ENOSPC` 路径。程序接口为：

```text
ext4-natural-pressure-workload [directory] [quota: 0|1|2] [quota_device]
```

全 syscall feedback 保留 inode bitmap 种子，监控 inode/block bitmap、目录链接数
和 mkdir 计数边界：

```sh
COUNTER_FEEDBACK_DURATION=3600 bash tests/run-ext4-syscall-counter-feedback.sh
```

设置 `COUNTER_FEEDBACK_NATURAL_PRESSURE_DISK=1` 使用独立压力盘；
`COUNTER_FEEDBACK_NATURAL_PRESSURE_SIZE` 控制盘大小，
`COUNTER_FEEDBACK_NATURAL_PRESSURE=0` 关闭附加压力负载。
设置 `COUNTER_FEEDBACK_NATURAL_QUOTA=1` 和
`COUNTER_FEEDBACK_NATURAL_QUOTA_ONLY=1` 可在该独立盘上用 UID 4242 的真实 quota
触发 `EDQUOT`，覆盖写入和 inode 创建的错误回滚。

读取 `/sys/kernel/debug/ext4/<device>/invariant_summary` 获取模型完成次数、违例与
`unverified`；`invariant_check` 提供详细证据。不支持的结构或无法证明稳定视图
只能记为 `unverified`。零违例需要结合非零完成检查次数解释。

## 错误窗口确认

验证内核需要同时开启 checker 和 fault 注入：

```text
CONFIG_EXT4_INVARIANT_CHECKER=y
CONFIG_EXT4_FAULT_INJECTION=y
```

```sh
bash tests/run-ext4-mballoc-free-failure.sh
bash tests/run-ext4-fast-commit-inode-replay.sh
```

这些入口只保留可复核的释放路径和 fast-commit replay 对照。普通 syscall 的
resize、mkdir、rmdir、unlink、rename、inode eviction、orphan 与 xattr 证据见
`BUG-INVENTORY.md` 中对应的自然运行记录；不再提供只有源码推断或 fault-only
中间态的专用 runner。

历史 baseline archive 保留原样，用于核对来源和当时的工程状态。

