# 逐个错误点插桩与运行时追踪

观测层在 Linux 6.12.1 的 counter-trace 基础上，对复核表中所有 `realistic=1`
记录添加源级调用表达式探针；未启用故障会话时保持原始调用及返回值。
本说明描述观测接口；程序级故障会话见 [故障注入说明](FAULT-INJECTION.md)。
不满足位置、表达式或源码哈希核验时，生成器直接报错，不跳过调用点。

## 逐点对应关系

[输入复核表](../../realistic_review.csv) 有 587 条候选记录，其中 265 条标为真实。
265 条真实记录全部出现在 [sites.csv](sites.csv) 和 [sites.json](sites.json) 中；
其余 322 条不生成探针 ID。源码修改保存在
[追踪补丁](patches/0001-reviewed-error-site-trace.patch) 中，由
[生成器](scripts/instrument-sites.py) 逐点生成并校验补丁重放。

CSV 的坐标属于官方原始源码。生成器先重放现有 counter-trace 补丁，再按 C token 和
函数定义定位同一调用表达式；不会把原始行号直接当成已修改内核的行号。
`sites.csv` 的 `line/column` 是原始坐标，`mapped_line` 是添加新探针前的位置，
`instrumented_line` 是应用追踪层 `0001` 后的位置。故障层 `0002` 会继续包装表达式并可能
改变后续行号；原始坐标、表达式、TU 选择器和最终源码哈希才是跨层定位依据。

特殊位置的处理如下：

| 记录 | 处理方式 |
| --- | --- |
| `187/447/550` | 同一个 `ext4.h` inline 表达式分别来自 inode、super、symlink 翻译单元；用各目标文件的编译标识选择原始 ID。 |
| `124` | `fs.h` 中的 `down_read_trylock` 仅在 file 翻译单元记录。 |
| `176` | `slab.h` 中 kzalloc 内部的 kmalloc 仅在 inline 翻译单元记录，可与外层 `175` 形成嵌套事件。 |
| `513` | `slab.h` 中数组分配 helper 仅在 super 翻译单元记录。 |
| `584` | `quotaops.h` 中配额 helper 仅在 xattr 翻译单元记录。 |
| `353/498` | CSV 坐标指向 `unlikely`、`rcu_assign_pointer` 宏；实际包裹其中的 buffer 检查、数组分配表达式。 |
| `504` | checker 已将 `ext4_count_dirs()` 提前算为局部变量；生成器明确核验这一处等价重定位。 |

`source_spelling` 是源码里实际包裹的函数/宏名，`callee` 保留静态提取后的函数名。
对 `kcalloc`、`alloc_percpu` 等宏，事件边界是源级表达式；宏内部的溢出检查等
可能阻止底层 `callee` 真正被调用。不能把一个宏表达式的 enter 解释为底层函数入口。
共享头文件中的表达式保留该翻译单元内 helper 的调用范围，而不是扩大到整个内核。

## 生成、构建与验证

在仓库根目录运行：

```sh
python3 experiments/ext4-counter-trace-fault-injection/scripts/instrument-sites.py
python3 experiments/ext4-counter-trace-fault-injection/scripts/configure-injection.py
python3 experiments/ext4-counter-trace-fault-injection/tests/test-error-sites.py
EXT4_EFI_JOBS=4 bash experiments/ext4-counter-trace-fault-injection/scripts/build.sh
bash experiments/ext4-counter-trace-fault-injection/scripts/run-smoke.sh
```

构建需要原实验已下载和校验的 Linux 6.12.1 源码及宿主工具。VM 使用现有的
`work/debian-normal.img` 和 `work/debian.id_rsa`。可用 `EXT4_EFI_CC`、`EXT4_EFI_JOBS`、
`EXT4_EFI_PORT`、`EXT4_EFI_ROOTFS`、`EXT4_EFI_SSH_KEY` 覆盖默认编译器、并行度、
SSH 端口及 guest 环境。

联合源码位于 `dev/linux-6.12.1/`，输出位于 `build/linux-error-site-trace/`。
生成器从官方源码重放原 checker 补丁和新探针补丁；不会修改原实验的开发树。
已有联合源码必须通过哈希核验才能复用。内核包含 `CONFIG_EXT4_COUNTER_TRACE=y`、
`CONFIG_EXT4_ERROR_SITE_TRACE=y`，并以 `build-binding.json` 绑定实际镜像、配置、
映射表、注入策略及三份补丁（原 checker、观测、故障层）。VM 入口先核对该绑定。
故障层还负责恢复基础补丁夹带的非插桩改动。最终源码对官方基线的完整检查和恢复项见
[源码基线](SOURCE-BASELINE.md)；源码检查报告同样纳入构建绑定。

VM 使用临时根盘 overlay 和两块新 ext4 数据盘，分别验证普通 ext4 和带 512 字节 inode 的
`inline_data` 文件系统；较大的 inode 留出 inline 扩容所需的 xattr 空间。
测试包含创建、写入、目录操作、xattr、truncate、fallocate、fsync，以及非法用户指针引发的
自然 `copy_from_user` 失败。还检查关闭探针后事件和计数均停止增长、稳定快照以及关机后的
`e2fsck -fn`。每次运行的原始 trace、统计和核验结果保存在 `results/error-sites-*/`。

## 在 guest 里查看经过的错误点

使用本实验内核启动 guest 后，以 root 执行：

```sh
mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null || true
mount -t tracefs tracefs /sys/kernel/tracing 2>/dev/null || true
echo 0 > /sys/kernel/tracing/tracing_on
echo global > /sys/kernel/tracing/trace_clock
echo 16384 > /sys/kernel/tracing/buffer_size_kb
echo > /sys/kernel/tracing/trace
echo 1 > /sys/kernel/debug/ext4_error_sites/enabled
echo 1 > /sys/kernel/tracing/events/ext4_error/error_site/enable
echo 1 > /sys/kernel/tracing/events/ext4_counter/ext4_counter_event/enable
echo 1 > /sys/kernel/tracing/tracing_on

# 在这里执行 ext4 工作负载。

echo 0 > /sys/kernel/tracing/tracing_on
echo 0 > /sys/kernel/debug/ext4_error_sites/enabled
cat /sys/kernel/tracing/trace > /tmp/error-sites.trace
awk -F '\t' 'NR == 1 || $2 > 0' /sys/kernel/debug/ext4_error_sites/stats
```

`stats` 为每个真实 `site_id` 输出一行，包含 hits、returns、翻译单元、原始位置、caller 和
callee。计数自启动以来累计，未提供 reset；比较某段负载时应取前后差值。正在执行的调用
可能只有 hits，待工作负载结束后再读取 returns。探针默认关闭，也可以通过内核命令行
`ext4.trace_error_sites=1` 从启动时开始累计，以观测仅在 ext4 初始化阶段运行的点。

实时查看可以在另一终端执行 `cat /sys/kernel/tracing/trace_pipe`。
`trace_pipe` 会消费事件；需要生成完整报告时，使用上面的停止后导出方式，或直接保存流。

事件包含 `site_id`、本次调用的 `invocation`、`phase=enter/return` 和返回值种类。
同一次表达式求值的两个事件使用相同 invocation；嵌套和并发调用有不同编号。
成功的指针结果归一为 1，NULL 为 0，ERR_PTR 保留负错误码；整数按内核 long 输出。
其中 `get_zeroed_page` 的返回类型是 unsigned long 地址，成功结果转成 long 后也可能为负。
enter 的 `result=0 kind=pending` 只是占位值。

函数失败约定不同：负整数、NULL、正的未复制字节数、trylock 返回 0、错误标志返回 1
都可能代表失败。因此，追踪器只记录到达和返回，不把整数 0 统一解释成成功或失败。
如果调用没有返回，例如崩溃或卡死，trace 中可以保留对应的 enter。

## 报告和调用栈

将 trace 取回宿主后运行：

```sh
python3 experiments/ext4-counter-trace-fault-injection/scripts/report-sites.py \
  /tmp/error-sites.trace --output /tmp/error-site-report
```

报告输出 `summary.json` 和按 trace 原始顺序排列的 `events.csv`，将每个事件映射回
源码位置和调用函数。CSV 保留 trace 前缀中的任务、CPU、时间戳信息。
未知 ID、重复 invocation、缺失 enter 或 return 均记录为不完整配对。
配对完整只描述这份 trace，还需检查 tracefs 每 CPU stats 的 overrun、dropped events
和任务过滤条件，才能判断采集范围内的事件完整性；VM smoke 会执行这些检查。

需要查看特定点的调用栈时，可在开始采集前启用 tracefs 的 stacktrace trigger，例如：

```sh
echo 'stacktrace if site_id == 175 && returned == 0' \
  > /sys/kernel/tracing/events/ext4_error/error_site/trigger
```

这一全局观测接口的计数覆盖本 guest 的所有 ext4 实例，包括根盘和 checker 引发的读取。
事件自身不包含设备号；单个目标进程可以用 event 的 `common_pid` 过滤，设备及操作归属
仍需结合负载、原 checker 的 `dev/op` 事件和稳定快照判断。
故障会话另外使用有 owner/原生 syscall/设备门控的 `ext4_efi:*` 事件，记录栈指纹并驱动
联合轨迹反馈；不要用全局观测计数代替合格候选数或实际注入数。

全部记录均已静态插桩，不意味着一次短测会经过全部记录。初始化专用路径、配额、加密等
配置和操作各有可达条件；命中 0 应首先检查启动时机、构建配置和负载是否覆盖该路径。
