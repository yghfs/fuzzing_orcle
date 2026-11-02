# 第二条链：长时采集、分段分析与资源治理

普通活动期的第一条链保持不变，第二条链把有限观测窗口连接成受控长时实验。
内核不需要再次改动，开发/官方树和主线项目保持独立。

## 操作序列来源

campaign 默认使用 `syzgen` 生成的 syzkaller typed 程序。生成器位于
`syzgen/`，使用 upstream API 的 `Generate`/`Mutate`，并通过 `target.SyscallMap`
逐项建立精确白名单，避免 base name 展开到设备或调试 syscall。每个 segment 的
`syz-programs/` 保存 `.syz`、`original.c`、`workload.c`、二进制和 `program.json`。
`program.json` 记录 seed、调用序号、native syscall number 和文件哈希；launcher
按 single/concurrent 模式启动这些程序。`syz-runtime.h` 为每个生成调用发出
`CT_SYSCALL` BEGIN/END，现有 analyzer 再用 raw syscall enter/exit 验证返回值。
第一批使用 `Generate`，同段后续批次用上一批 `.syz` 执行 `Mutate`；
生成 syscall 序列不由手写 C 模板决定。默认 `--syzkaller-calls 32`，可设置 1–256；
预生成的批次不一定全部执行，`manifest.json` 的 batches 才是实际批次数。
每个候选程序必须至少包含一个 `write`、`pwrite64`、`fallocate`、`ftruncate`、
`fsync` 或 `fdatasync`；不满足时从同一 seed 重新生成，`program.json` 保存
`accepted_attempt` 和 `has_data_call`。运行时还会为每个 worker 建立独立的
`w<worker>-<pid>` 工作目录，预置 `file0/file0` 等稳定路径，避免并发 worker
互相删除路径。
syzkaller 变异偶尔会产生无法被 C exporter 接受的复合参数（例如不完整的 xattr
union）；`ct-syzgen` 会从同一父程序重新反序列化并最多重试 64 个 seed，仍失败时
使用新的 `Generate` 程序填充该 slot，并在 `program.json` 标记
`method=GenerateFallback`、`mutation_fallback=true` 和重试次数。这样不会把导出器
错误误报成内核/trace 失败，也不会执行未通过 C 编译的程序；父程序路径和 SHA256
仍保留用于审计。
完整性检查要求每个实际程序的每个 call index 恰好执行一次、调用名称/号码吻合且
原生返回验证通过，包括正常的 ENOENT/EINVAL 等失败返回。超时/程序崩溃拒绝完整结论。
生成程序在所选镜像的 worker 专属 `syz-work/w<worker>-<pid>` 目录执行，
程序结束后删除该目录。若生成的 fd 为空、失效或指向不适合数据操作的对象，
运行时只对数据类调用使用预置普通文件 fd 兜底，并在 `CT_SYSCALL repaired=1`、
`CT_WORKLOAD_SUMMARY data_calls=N fd_repairs=M` 和 `program.json` 的
`fd_repair_policy` 中明确记录。报告中的 `data_success` 统计 native 返回值，
不能把“程序含有数据调用”误读为“该调用原始参数成功”。
完整逐次 CSV/JSONL 和 HTML 样例提供 `program`、从 0 开始的 `call_index`、
`syscall_name`；同段的 `syz-programs/<program>/program.syz` 是对应操作序列。
setup 和内核后台事件没有生成调用坐标时保持为空。

这是可复现的生成/变异链，已接入本子项目自有的 KCOV MVP，但不包含 syzkaller manager 的完整覆盖反馈闭环；因此
报告应把它称为 syzkaller-generated sequence，而不是 coverage-guided fuzzing。

## 运行方式

先做六段矩阵验证：

```sh
bash experiments/ext4-counter-trace/scripts/run-campaign.sh \
  --segments 6 --duration-seconds 180 --segment-seconds 1 \
  --max-batches 2 --syzkaller-calls 16 --batch-timeout 10 --workers 2 --memory-mb 0
```

一小时受控运行：

```sh
bash experiments/ext4-counter-trace/scripts/run-campaign.sh \
  --duration-seconds 3600 --segments 10000 \
  --max-run-mb 4096 --max-trace-mb 64
```

一小时无并发负载（所有段为 single，一个 worker，三种镜像依次轮换）：

```sh
bash experiments/ext4-counter-trace/scripts/run-campaign.sh \
  --mode single --duration-seconds 3600 --segments 10000 \
  --workers 1 --memory-mb 32 --max-run-mb 4096 --min-free-mb 2048
```

`--mode` 默认 matrix，按镜像交替 single/concurrent；single/concurrent 可固定所有段的模式。
single 固定一个负载 worker；ext4 后台 writeback/journal 仍采集，不保证内核内没有并行活动。

终端默认实时显示北京时间（`+08:00`）：campaign 开始、VM 启动/就绪、segment、程序生成、
每个 workload 的实际开始/结束、前后快照、取回证据、KCOV 增益及分析开始/结束。
`WORKLOAD_START` 在 worker 通过启动门、即将 exec 程序时发出，`WORKLOAD_END` 记录 launcher
收到的退出码或信号；生成程序开始生成与开始执行分别显示。每行带 segment/batch/program
坐标，便于定位 `segments/<segment>/syz-programs/<program>/program.syz`。
时间与阶段提示另存为 run 根目录的 `progress.log`；guest 原始提示保存在
`segment-******-guest.txt`，生成程序 stdout/stderr 仍保存在对应段的 `workload.txt`。
被 timeout 或中断杀死的 launcher 可能没有 `WORKLOAD_END`，此时以段退出码及整轮停止理由为准。

十二小时上限示例（已用 1 GiB 配额触发并正常收尾验证）：

```sh
bash experiments/ext4-counter-trace/scripts/run-campaign.sh \
  --mode single --workers 1 --duration-seconds 43200 --segments 10000 \
  --max-batches 1 --syzkaller-calls 32 --scenario all \
  --max-run-mb 40960 --max-trace-mb 64 --min-free-mb 2048
```

长跑默认结果配额为 40 GiB。KCOV 原始样本在完成核验后压缩，逐段编译二进制在保留
`program.syz`、`program.json` 和源文件后清理；完整 trace、生命周期、逐点 CSV/JSONL
和 campaign 汇总仍保留。最终 `episodes.jsonl` 与 `segment-summary.jsonl` 也以 gzip 形式保存。

长期场景在启动时通过 `--scenario` 固定，并写入每个 segment 的场景证据。pressure 逐步分配空间，目标保留 24 MiB 可用空间、硬上限 104 MiB；实际 `statfs`、预留文件字节数都会记录，未达到目标则该段验收失败：

```sh
--scenario stateful   # 跨 segment 保留 32 个文件
--scenario large-dir  # 每个实验文件系统预置 256 个目录项
--scenario pressure   # 目标剩余 24 MiB，上限预留 104 MiB，接近 ENOSPC
--scenario all        # 三类场景同时启用
```

这些场景与 resource-valid 生成策略正交；`program.syz` 仍保存每个实际执行的
syzkaller 操作序列，场景初始化和清理证据保存在 `*-scenario.txt`。fast-commit、
resize、corruption 和 legacy orphan 路径继续由 `run-special.sh`/`run-relations.sh`
独立重放，不混入普通活动期端点。

停止条件是运行时长、段数或资源边界中先到者。时长在批次/分析边界检查，
允许正在进行的有限批次、分析和可靠卸载结束；另有覆盖启动/收尾开销的整轮 wall watchdog。
默认每段一批；`--max-batches` 最多 20，`--segment-seconds` 限制继续启动批次的时间。
所有参数有上下界，不启动无限压力。新 run 使用独立 rootfs overlay 和三张 132 MiB 镜像。

regular、bigalloc、inline 轮流执行 single/concurrent，固定 seed 可重放批次选择。
普通 syscall 覆盖写入、fallocate/hole punch、truncate、fsync、link、tmpfile、
rename overwrite/exchange/whiteout、自然 ENOSPC/EEXIST/ENOENT。
每批试验目录清理；一个 retained 文件保留 1–8 MiB 分配，让后续区间具有持续状态。
并发 worker 最多 8，内存压力实际触页，上限 256 MiB。

## 连续采集与轮转协议

host 的单一 SSH `trace_pipe` reader 持续读取；正常运行期间不停止追踪或清空 ring。
`CT_CAMPAIGN ROTATE id=segment-N` 在流中切换输出文件，seal 前一段；
`CT_CAMPAIGN TERMINAL` seal 最后一段。
因此分析、传输、等待期间的 ext4 后台事件也保存，段间不会人为制造追踪空洞。
原生 syscall 过滤器只选择 C 负载及已登记 worker PID；worker 在过滤器生效后才运行。
ext4 事件不作 PID 过滤，保留 writeback/journal 等后台事件，避免追踪 reader 自身放大 syscall 流量。

每段为三个实验设备分别保存开始/结束序号与前/后稳定快照。
下一段分析带入前段最后有效快照的完整 scope 及后续 trace 尾部，并引用带入 JSON；
由这个锚点检查段间更新和下一次快照。
带入事件只存在于分析输入，原始 `trace.txt.gz` 保持各段互不重复。
跨段完整性只校验原始流，不能把重复的分析锚点当作新事件。

每个 CPU 的 overrun/commit overrun/dropped events 按累计读数的差值校验。
最后一段使用 checkpoint 时的累计统计，涵盖 END 后的收尾采集。
任何非零、缺 CPU 或统计倒退都会拒绝完整覆盖结论。
校验每个实验设备同 boot/dev/epoch 下的 seq 集合、连续区间和重复 seq；
不同 CPU 的记录时间不等于修改顺序，仍不能强行建立跨线程因果关系。

## 分段证据与整轮结论

`results/campaign-*/campaign.json` 保存 run/boot、设备、初末端点、参数、
启动/停止原因、外部 rootfs backing 的路径及 SHA256、构建/脚本内容哈希、关机和磁盘状态。
每个 `segments/segment-N/manifest.json` 保存状态、seed、worker、资源、
批数、时间、原始 trace 字节数、序号域、loss 差值和证据 SHA256。
manifest 使用临时文件、fsync、原子 rename；中断不会把半写 JSON 当成完整 manifest。

状态区分 complete、timeout、guest_error、snapshot_invalid、host_interrupted、
qemu_crash、trace_loss 和 incomplete。
生命周期分析报告仍保存 transient/operation exit/persistent/ambiguous/unverified 五类证据，
包括出现、peak、恢复、出口和稳定快照位置。
整轮输出：

- `campaign-report.json/.md`：覆盖、分类、设备/模式统计、syscall 配对和磁盘结果。
- `point-report.json/.md/.html`：按插桩点 × 公式统计每次命中的比较结果，可筛选镜像和 single/concurrent。
- `point-hits.jsonl.gz`：一个原始 ext4 event 一行，保存该次命中的各比较来源。
- `point-hits.csv.gz`：一个命中 × 比较来源一行，保存结果、actual/expected/差值及 seq、syscall、原始 trace 路径和行号。
- `episodes.jsonl`：逐段 episodes，保留 segment_complete 标记。
- `segment-summary.jsonl`：逐段状态、资源、错误和配对计数。
- `segments/*/trace.txt.gz`：原始流；`analysis/trace.txt.gz`：含带入锚点的分析流。
- `segments/*/analysis/lifecycle/`：压缩逐点时间序列、episodes、JSON 报告及可读报告。

逐点报告以原始 trace 中实验设备的实际事件为分母，带入的锚点不重复计数。
某点命中 100 次，就在各比较来源下分别列出一致、不一致、并发不确定、无法验证、仅记录的数量，
五类数量之和等于该来源的命中数。现场 actual/expected 与重建 descriptor、global、group、namespace
各自统计；同一命中在不同来源可以有不同结果，来源之间的数量不能相加。
status=0 的转换/边界事件没有现场 expected，归为仅记录；缺少独立锚点或派生值归为无法验证。
并发重建保留差值数值，但不能据此声明确定一致或不一致；global 派生量是发布累计量。
不一致命中数和差值生命周期 episode 数分别保存，出现点不是 bug 因果认定。
HTML/JSON 汇总保存前 100 次和各结果的少量样例，完整逐次数据在压缩 CSV/JSONL；
raw_trace 相对原 run 路径，raw_line 是对应原始段内从 1 开始的行号。
原始 trace 有缺失时只能报告已采集命中，不能证明实际执行的全部命中都已计入。

`coverage_complete` 要求所有已启动段连续、完整、无 loss、快照及重建有效；
`passed` 另要求可靠卸载/正常 QEMU 退出、三张镜像 fsck 和独立 P-DISK/R-UG/R-LINK，
以及最终稳定内存总量与磁盘总量一致。
活动期覆盖有效但磁盘 checker 不支持某布局时，不能报告整轮通过。
inline 离线扫描解析 i_block 和 system.data 两个独立区域、合成点目录引用，并检查 xattr 边界。
R-UG 按 group 比较，避免相反差值抵消；extent index 使用磁盘格式的 leaf_lo/leaf_hi。

## 资源边界与故障恢复

默认 QEMU 2 CPU/2 GiB；trace buffer 每 CPU 64 MiB；单段原始 trace 64 MiB。
超限 reader 保留已写前缀并停止实验，不覆盖旧段。
guest 批次限制 CPU 秒数、256 FD、512 MiB 地址空间，timeout 附带 kill deadline 并覆盖 fork worker。
host 命令、启动、轮转和分析都有超时，分析子进程上限 1 GiB 地址空间和 256 FD。
host 检查 QEMU 存活、reader EOF/错误、运行目录字节量和宿主剩余磁盘空间；
启动前预检三张必要镜像的预算，聚合导出也受剩余目录预算约束，原始分段 episodes 始终保留。
运行目录接近 `--max-run-mb` 时会设置优雅停止标记：让正在执行的 bounded segment 完成，
不再启动下一段，然后执行 checkpoint、CPU-loss/生命周期分析、正常关机、fsck 和离线磁盘比较。
触发前按单段 trace 上限及报告导出量预留空间；`stop_reason=max_run_bytes` 表示受控容量停止，
若上述证据均齐全，campaign 仍可通过。目录统计按稀疏镜像逻辑长度计算，每秒采样。
原始 trace 字节上限在写入前精确检查。压缩结束后才替换原文件；
guest 仅在 host 保存/分析后删除已转存的段 spool，避免长时占满 guest 根盘。

SIGINT/SIGTERM、异常 EOF、guest 超时或 QEMU 异常时尝试取回当前段，
保存中断原因和仍可分析的轨迹；所有 run 目录及失败段保留。
正常 checkpoint 在强制 sync 之前读取最终稳定快照，停止活动期采集后 sync/卸载。
只在卸载成功、请求关机且 QEMU 正常退出后进行 offline fsck；
异常 kill 路径没有可靠关机证据，拒绝离线通过结论。
SIGKILL/宿主断电不能执行 trap，已写 raw 前缀和原子 manifest 仍可用于恢复审计，
running 状态始终按不完整处理。

重建整轮报告，不需要 VM：

```sh
python3 experiments/ext4-counter-trace/scripts/analyze-campaign.py <run> --output <新目录>
python3 experiments/ext4-counter-trace/scripts/analyze-campaign.py <run> \
  --reanalyze --output <新目录>
```

第二条命令逐段解压并用当前分析器重新分析，聚合使用重新生成的结果；
不修改归档证据。跨重启启动新的 campaign、保存新 boot/dev/epoch 和新稳定锚点：

```sh
bash experiments/ext4-counter-trace/scripts/run-campaign.sh --predecessor <旧run路径>
```

旧 run 的失败或缺失区间不会被新 run 覆盖。跨 boot/epoch 的段不能直接拼接为连续证明；
当前不实现自动恢复旧 VM 的未完成 syscall 或跨重启轨迹。

## 适用范围

长时运行基础设施可用，不意味着已经完成数小时/数天 soak 或证明不存在长期异常。
有效稳定快照才支持 persistent_mismatch，运行时长、EOF 或超时均不构成持续异常证明。
并发/后台归属仍按第一条链保留 ambiguous；BLOCK_UNINIT 布局、
通用跨线程 parent 和 special 域统一时间序列仍是独立的后续工作。

## 本轮证据

当前六段短验收目录已按历史材料清理要求删除，结论只保留在本文件的摘要中。此前受控长跑
目录也已清理：2262/2262 段、
72384/72384 syscall 配对、KCOV 和三种镜像磁盘检查通过；实际运行约 9.26 小时，
因 20 GiB 旧配额停止，尚不是完整 12 小时结果。

随后 40 GiB 配额的 12 小时尝试也已清理，仅保留其历史摘要：3460 段完整，
110720/110720 syscall 配对，KCOV 3460/3460 样本完整；在下一段取证时 `scp` 超过原
45 秒超时，导致最后 segment、campaign checkpoint、干净关机和离线磁盘校验缺失，整轮失败。
runner 已把 scp 限时提高到 300 秒，并增加有限超时重试；停止采集后会关闭 collection wall watchdog，
允许 checkpoint、取证和离线磁盘校验完成；需要重新运行才能取得完整 12 小时证据。

2026-10-18 修复还让逐点汇总优先使用本段时间线，仅在存在未匹配原始事件时读取下一段 carry 时间线，
降低长跑聚合的重复扫描。现有失败结果仍保持失败状态；这些修改要通过下一轮完整 checkpoint、关机、
fsck 和磁盘比较后才算新的 12 小时证据。
