# ext4 运行时不变量检查器 MVP

## 1. 目标与决策

### 1.1 MVP 目标

使用 syscall fuzzing 触发 ext4 的文件和目录操作，在内核当前运行期间检查 metadata 计数不变量，优先发现：

- free cluster 计数下溢、上溢和错误增量；
- free inode 计数下溢、上溢和错误增量；
- 全局计数与各 block group 计数不一致；
- 目录计数错误；
- 分配/释放路径中的重复更新、漏更新和错误回滚。

MVP 不负责证明文件内容正确，也不覆盖 crash recovery、POSIX 语义、data race、use-after-free 和死锁。

### 1.2 fuzzing 工具决策

MVP 使用成熟的 syscall fuzzing 工具，不重新实现 fuzzing 引擎。推荐使用 syzkaller，原因是它已经提供：

- Linux syscall 序列生成和变异；
- KCOV 覆盖率反馈；
- 内核崩溃检测和复现程序；
- 虚拟机运行、重启和 corpus 管理。

本项目自己实现的是 ext4 内核 checker、shadow state 和故障报告，不是新的 fuzzing 调度器。

AFL++、libFuzzer 等文件格式 fuzzing 工具不适合作为第一版主驱动，因为它们主要变异输入字节，不能直接表达长生命周期的 mount、open、write、rename、link、unlink、truncate 等 syscall 状态序列。

当 syzkaller 无法表达某类特定 workload 时，再增加一个小型 workload 生成器或 syzkaller syscall 描述，不应先重写整个 fuzzing 框架。

## 2. 范围约束

第一版固定以下条件：

```text
文件系统：ext4
block size：固定
bigalloc：关闭，rho = 1
并发：单个 fuzz 程序先按单线程执行
online resize：关闭
journaling：固定一种模式，例如 data=ordered
复杂特性：暂不启用 quota、DAX、inline data、加密
```

完成基础版本后，再逐步增加 bigalloc、flex_bg、lazy initialization、多线程和 quota。

## 3. 总体架构

```text
syzkaller
    |
    v
ext4 syscall workload
    |
    v
ext4 metadata update hooks
    |
    +--> 操作级检查
    +--> shadow state
    +--> 一致快照检查
    +--> 事件 ring buffer
    |
    v
WARN / panic / debugfs 报告
```

checker 编译为内核中的 debug 功能，建议使用独立配置项：

```text
CONFIG_EXT4_INVARIANT_CHECKER=y
```

生产内核默认关闭。fuzzing 内核可以提供两种动作：

```text
report：记录错误并继续运行
panic：发现错误后立即触发 kernel panic，交给 syzkaller 收集
```

## 4. 运行时状态模型

只检查 live state，不把磁盘 superblock 的持久化计数作为普通运行时 oracle：

```text
Cg_live       第 g 组当前内存中的 free cluster 数
Ig_live       第 g 组当前内存中的 free inode 数
RB_live       全局 free cluster counter
RI_live       全局 free inode counter
Dg_live       第 g 组当前使用的目录数
Dirs_live     全局目录计数
```

MVP 的核心不变量为：

```text
0 <= Cg_live <= Ng
0 <= Ig_live <= Ng_inode

RB_live == sum(Cg_live)
RI_live == sum(Ig_live)
Dirs_live == sum(Dg_live)
```

这些聚合等式只能在一致快照点检查，不能在任意并发更新中间点检查。

## 5. 检查规则

### 5.1 block/cluster 分配和释放

对于一次成功的操作：

```text
expected_delta = -allocated_clusters + freed_clusters

Cg_new - Cg_old == expected_delta
RB_new - RB_old == expected_delta
```

`allocated_clusters` 和 `freed_clusters` 必须来自独立事件或 allocator 的实际 extent 结果，不能通过被检查的 counter 反推。

检查以下错误：

- 分配成功但 counter 没有减少；
- 释放成功但 counter 没有增加；
- 同一释放路径重复增加；
- 失败回滚时重复恢复；
- counter 下溢或上溢；
- group counter 与全局 counter 更新不一致。

### 5.2 inode 分配和释放

```text
expected_delta = -allocated_inodes + freed_inodes
Ig_new - Ig_old == expected_delta
RI_new - RI_old == expected_delta
```

第一版只检查 free inode 计数，不立即把所有 link count 语义纳入强制 oracle。

### 5.3 目录计数

对 mkdir/rmdir 和对应的内部目录更新记录：

```text
Dirs_new - Dirs_old == created_dirs - removed_dirs
```

同时在一致快照点检查：

```text
Dirs_live == sum(Dg_live)
```

### 5.4 一致快照

第一版使用 debug checker barrier：

- 所有被插桩的 metadata 更新进入 checker 读侧；
- 快照检查进入 checker 写侧；
- 写侧获得后，相关更新全部停止；
- 读取所有 group counter 和全局 counter；
- 检查全局聚合等式。

如果后续性能不满足，再换成全局 sequence counter 和重试快照。

## 6. 内核插桩位置

围绕目标 Linux 版本中实际使用的 ext4 helper 插桩，不依赖固定函数名。至少覆盖以下逻辑点：

```text
block/cluster allocation
block/cluster free
inode allocation
inode free
directory create/remove
link/unlink
extent insert/remove
journal transaction commit/abort
```

每个事件记录：

```text
event_type
group number
inode number（如果适用）
physical range（如果适用）
delta
transaction id
operation id
current task/cpu
```

事件必须在对应状态修改成功后记录；对于可能回滚的事务，需要记录 begin、commit、abort，不能只把 syscall 返回成功当作 metadata 已提交。

## 7. Shadow state

checker 维护一份与内核 counter 独立的 shadow state：

```text
shadow_free_clusters
shadow_free_inodes
shadow_dirs
shadow_group_free_clusters[g]
shadow_group_free_inodes[g]
```

事件到达时更新 shadow；周期性快照时比较：

```text
shadow_free_clusters == RB_live
shadow_free_inodes   == RI_live
shadow_dirs          == Dirs_live
```

shadow 的目的不是复制 ext4 的实现，而是用事件语义检查“实际 counter 是否按照预期改变”。如果 shadow 与实际 counter 都从同一个错误字段计算，检查就失去独立性。

## 8. 故障报告

每次发现错误时输出：

```text
invariant id
filesystem/device
group/inode/extent
old value
new value
expected value
operation id
transaction id
最近 N 个 metadata event
task、pid、cpu
```

内核中保留固定大小的 event ring buffer。`panic` 模式下先把 ring buffer 输出到 kernel log，再触发 panic，便于 syzkaller 自动保存复现程序和日志。

## 9. 实施阶段

### 阶段 0：基线环境

- 编译带 `CONFIG_EXT4_FS`、`CONFIG_KCOV`、`CONFIG_DEBUG_INFO`、`CONFIG_DEBUG_FS` 的 fuzzing 内核；
- 准备 syzkaller 和 QEMU；
- 确认可以独立挂载 ext4、执行 syscall 程序并捕获 kernel panic；
- 记录未加入 checker 时的覆盖率和吞吐量。

### 阶段 1：checker 核心

- 添加配置项和 checker 数据结构；
- 添加 event ring buffer；
- 实现 `report/panic` 两种模式；
- 实现 checker barrier；
- 实现范围检查和整数溢出检查。

### 阶段 2：block/inode 计数

- 插入 block/cluster 分配和释放事件；
- 插入 inode 分配和释放事件；
- 实现 group delta 检查；
- 实现全局与 group 聚合检查；
- 编写确定性测试：创建、写入、truncate、unlink、fallocate、重复失败和回滚。

### 阶段 3：目录计数和 workload

- 加入 mkdir/rmdir 事件；
- 加入 rename、link、unlink 等目录操作；
- 配置 syzkaller 重点生成 ext4 相关 syscall 序列；
- 先单线程运行，再逐步增加并发。

### 阶段 4：验证 checker 本身

checker 本身只观测真实运行时状态，验收使用正常 workload、已知历史回归和独立参考结果。不得通过修改 checker 的 shadow 或 delta 来制造告警。

源码审计或运行检查得到候选 bug 后，可在单独的验证内核中启用 `CONFIG_EXT4_FAULT_INJECTION`，对候选错误路径做 control/fault 对照。注入实现独立于 checker，验证结果与正常运行发现结果分别记录。

## 10. 验收标准

MVP 达到以下条件即可进入长期 fuzzing：

1. 正常 ext4 workload 连续运行不会产生误报；
2. 能用已知历史回归或独立参考结果验证 checker 的判断；
3. 能输出包含 operation、group、旧值、新值和事件历史的报告；
4. syzkaller 可以自动保存触发错误的 syscall 程序；
5. 与未插桩内核相比，吞吐量下降和内存开销已经被记录；
6. 至少有一个真实计数 bug 或历史回归能够被复现或验证。

建议同时记录：

```text
每秒执行 syscall 数
每秒执行 fuzz program 数
覆盖率
checker 事件数
检查耗时
误报数
发现的违反次数
```

## 11. MVP 暂不实现的内容

- 磁盘 superblock 与 live counter 的实时比较；
- crash/recovery 一致性；
- bitmap 全量扫描；
- 全文件系统 extent ownership 图；
- 普通文件和目录所有 link count 的统一公式；
- bigalloc、lazy initialization、online resize；
- 多线程下无额外同步的强一致判断。

这些内容适合在 MVP 通过后单独增加，否则很容易把 journal、并发和 feature 语义混入第一版，导致无法判断错误来自 checker 还是文件系统。

## 12. 最终建议

不要重新写一个完整 fuzzing 工具。第一版采用：

```text
syzkaller：生成和执行 syscall 序列
ext4 checker：检查内核 live metadata
shadow state：提供独立增量 oracle
checker barrier：提供一致快照
```

syzkaller 与 checker 负责正常运行时问题的发现；故障注入只用于后续候选 bug 验证，不能把注入实验归入正常运行发现结果。当前构建和执行方式见 [tests/README.md](tests/README.md)。
