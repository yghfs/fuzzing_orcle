# Probe API 与稳定内存快照协议

日期：2026-10-10。实现基于独立官方 Linux 6.12.1 开发副本
`dev/linux-6.12.1/`；`kernel/linux-6.12.1/` 继续作为原始参照。

## 矩阵 review 结论

1. `ext4_mb_mark_context()` 的上游 `changed` 在普通路径默认等于请求长度。
   观察器必须另外在 group lock 内统计原 bitmap 中真正会转换的 bit，不能直接
   把上游 `changed` 当作独立 expected。观察器不修改上游 `changed`、更新量或错误处理。
2. `EXT4_MB_SYNC_UPDATE` 只等待 bitmap/descriptor 写入，不更新 global counter。
   FC replay 的 block 和 inode 特殊 helper 先修改局部元数据；正常挂载随后在
   `ext4_percpu_param_init()` 从恢复后的 descriptor 初始化 global。
   replay bit transition 带独立标签，活动期增量分析不混入恢复事件。
3. `ext4_free_inode()` 的目录 global decrement 位于 group lock 内；free-inodes
   increment 在锁外。这两个发布点不能被合并成一个“锁外 global update”。
4. 普通 allocation/free helper 的活动窗口必须包含局部转换和 global 发布。
   bigalloc 的 `ext4_ext_remove_space()` 外层窗口还包含延迟 rereserve 发布；
   journal pending-free/buddy 复用不等于 bitmap 的释放量。
5. resize begin/end、corruption exclusion 和 inode replay 单列活动窗口。
   bitmap lazy 初始化的计数字段写入已位于 block/inode 外层活动窗口内；
   后台 inode-table zeroing 本身不写本版扫描比较的 C/I/U 或有效 bitmap 位。
6. 完整 `R-UG`、`R-LINK` 扫描使用独立 inode/namespace 实现；稳定快照中逐 inode
   比较目录 inode 总量和 dirent 引用。无法证明扫描期间稳定时明确返回未验证。

## API

见 `fs/ext4/counter_trace.h`、`counter_trace.c` 和
`include/trace/events/ext4_counter.h`，配置项 `CONFIG_EXT4_COUNTER_TRACE` 默认关闭。

| API | 用途与约束 |
| --- | --- |
| `ext4_ct_init(sb)` | sbi 分配后初始化每个 superblock 的 observer 状态，早于恢复及计数写入 |
| `ext4_ct_enter(sb, scope, point)` | 写入开始之前注册活动窗口；栈上的 scope 贯穿整个逻辑更新 |
| `ext4_ct_exit(sb, scope, result)` | 成功、失败、回滚后的所有出口成对注销；END 使用 scope 自身 ID |
| `ext4_ct_record(...)` | 有界、不分配内存、不扫描的事件记录；可以在 group spinlock 内调用 |
| `ext4_ct_snapshot(sb, buf)` | sysfs 进程上下文的全量扫描；扫描期间不持 observer lock |
| `ext4_ct_state_show(sb, buf)` | 导出 epoch/seq/version/active，用作有限 trace 区间的完整性端点 |

同一线程嵌套 scope 使用唯一 `operation_id`、最近注册的 `parent_operation_id`。
后台任务自己产生操作 ID；不跨线程猜测 parent。PID、comm、CPU、时间由 ftrace
公共头提供；关联标识由事件提供。END 是 ext4 内部返回，不是 syscall 返回，
也不是 journal commit/checkpoint。namespace VFS wrappers 覆盖 create/mknod/tmpfile/
mkdir/symlink/link/unlink/rmdir/rename2，包含 rename-exchange/whiteout 的外层边界。

事件使用单独 trace system `ext4_counter`。每个 superblock 有 mount epoch 和单调 seq；
并发 CPU 的文本输出顺序可能不同，分析必须按 `(dev, epoch, seq)` 排序。
trace 没有启用时仍维护 seq/version，因此启停 trace 必须明确选取区间端点。

状态编码：0=raw、1=consistent、2=mismatch、3=unverified。
`comparison_valid=1` 仅用于有比较依据的记录；raw/unverified 中 0/0 不表示相等。
未验证记录的 expected 是占位，分析时视为缺失。raw 的 actual 可表示局部值或
发布请求量，具体由 point/reason 决定，不能当作完整 percpu 快照。
`update_delta` 表示该 point 的实际转换量/发布量；`delta` 仅在比较有效时表示
`actual-expected`。不同层级记录不得重复累加：bitmap transition 是 independent，
descriptor/global publication 是另一类证据。

## 非阻塞快照

本版不 freeze、不 sync，也不拿新锁阻止 ext4 writer。接受条件是有完整覆盖的
更新窗口均不活跃，且扫描首尾没有任何更新进入或退出。

1. writer 开始修改之前，持 observer spinlock 将 active 加一并递增 version；
   writer 更新完全部相关局部/global 发布后，持同一锁递增 version 并将 active 减一。
2. scanner 在 observer lock 下取得 `active == 0` 和起始 version，随后释放锁。
3. scanner 分配最多 65,536 个 group 的临时行，逐组用上游 helper 读取逻辑 bitmap。
   UNINIT helper 根据布局合成位图；有效范围只计 group 的实际 cluster 位、有效 inode 位，
   最后一个短 group、尾字节及 bigalloc 单位独立处理。
4. 每组在 group lock 内只复制 bitmap 和 descriptor；释放锁后数 bit。
   I/O、内存分配、睡眠与全量统计均不在 group spinlock 内。
5. scanner 聚合完整 `percpu_counter_sum()`，再在 observer lock 下检查 active 为零且
   version 与起点相等；版本采样前的内存屏障约束先前数据读取不越过校验。
   只有完整扫描、同一版本、无 resize/replay/error/shutdown/
   journal abort/corrupt group 才接受 `R-BG/R-IG/R-BSUM/R-ISUM/R-USUM`。
6. 新 writer 在末次校验后开始不影响已完成的数据副本。输出属于已验证的历史窗口，
   不是声称输出时 filesystem 仍静止。任何并发/不完整读取都会输出 unverified；
   不自动重试、不伪造一致，也不把发生差值直接称作漏洞。

observer spinlock 只保护 observer；顺序始终是原有 ext4 锁 -> 短暂 observer lock。
scanner 从不持 observer lock 再获取 ext4/group/buffer lock，因此不增加反向锁序。
多 writer 使用 active 计数与单调 version，不能用普通单 writer seqcount 的奇偶假设。
快照本身使用只读 scope，拥有独立 snapshot ID，但不递增 writer version。

依赖：新增计数写入路径必须补活动窗口；仅补一个事后事件不能保持该证明。
resize 生命周期的持久 scope 在清除 resize 标记前移除，由独立 END scope 覆盖剩余收尾，
避免新 resize 覆盖仍在使用的 scope。两次 read 相等不替代上述条件。

## 发布与持久化

`P-SB` 捕获 `ext4_update_super()` 本次复制实际使用的正值 percpu 聚合，
分别记录 block 和 inode 字段。buffer lock 内字段复制完成后比较，不第二次读取
global，也不声称两项计数来自同一时刻。负数被上游 clamp 的异常不由本式单独排除；
稳定总量比较仍使用未 clamp 的 `percpu_counter_sum()`。

sysfs 入口（root 可读）：

```text
/sys/fs/ext4/<device>/counter_snapshot
/sys/fs/ext4/<device>/counter_trace_state
```

本版内存快照检查 counter 总量及运行时 `R-UG/R-LINK`；namespace 在覆盖范围内独立扫描比较，
并发、格式和规模限制明确输出未验证理由。inline 目录及 zero-link inode 的扫描已修复。
离线 `scripts/check-disk.py` 已独立解析 descriptor/bitmap/inode table/extent directory，
实现持久状态的 `P-DISK` 以及离线 `R-UG/R-LINK` 交叉检查；`e2fsck -fn` 仍作为独立辅助证据。

## 有限轨迹验证

`scripts/run-smoke.sh` 使用独立 rootfs overlay 和数据镜像；原主线镜像仅作只读 backing。
普通、bigalloc、无 journal、lazy 初始化分别执行四 worker 负载、并发快照、
保留分配后的非零总量采样、释放、自然 ENOSPC 和 link/rename/unlink/rmdir。
最后稳定内存检查安排在 sync 前，卸载后单独执行 fsck。

`scripts/analyze-trace.py` 校验端点 epoch/active/seq、区间序号完整性、BEGIN/END 配对，
bitmap 转换与 global 发布增量及稳定快照之间的实际总量差；保留 events.jsonl。
读取 per-CPU overrun/commit-overrun/dropped 统计，缺失统计或缺序号均失败。
检查原始 trace 中间态，不仅导出 violation。trace clock 设为 global，缓冲按 CPU 配置。

`tests/test-analyzer.py <run>/ct-results` 在临时副本中删事件、模拟 overrun、修改稳定总量，
要求分析器拒绝；原始证据不被修改。有限端点只能证明选定区间，不声称整个 boot trace
或关闭 tracing 期间完整。端点采集须在 writer 静止时进行，否则应重新选取区间。

## 剩余覆盖

- 运行时 `R-UG/R-LINK` 完整稳定扫描、namespace dirent/nlink 细粒度记录：implemented，并在普通 smoke 与 special snapshots 中验证；
  离线持久镜像扫描已实现并接入 smoke。
- 逐点 independent/descriptor/global 和 group/namespace 差值、外层操作出口、稳定态分类：implemented，
  见 [FIRST-CHAIN.md](FIRST-CHAIN.md)。有限负载的准确原生 syscall enter/exit 与 ret/errno 已关联；
  通用跨线程 parent/后台因果传播仍未实现，明确 `concurrent_or_ambiguous`。
- resize/corruption/FC replay 的独立 adjustment expected：implemented，并由 special analyzer
  强制检查全部点位；普通增量分析仍将这些事件分类隔离。
- 错误/恢复镜像、online resize 和 bigalloc rereserve 专用定向验证：implemented；special
  runner 保留双启动 FC replay、resize、corruption 和 bigalloc trace/snapshot 证据。
- `P-DISK` 自定义离线解析器：implemented；`BLOCK_UNINIT` 的 flex_bg metadata layout
  仍标记 unverified，不以磁盘 bitmap 零值冒充逻辑空闲量。

## Schema 2 生命周期链

当前字段语义、锁内 descriptor 增量与独立转换、namespace applied ref/birth/death、
原生 syscall 配对、分类和有限验证入口见 [FIRST-CHAIN.md](FIRST-CHAIN.md)。
`result` 与 `actual` 分离；`dir_ino/ref_delta/generation/is_dir` 独立导出。
同一目录替换的旧/新引用在 inode 字段修改后分别记录，二者是一个转换的两个分量，
不是两次现场完整目录扫描。跨 CPU seq 是 observer 记录顺序，不证明实际修改先后。
