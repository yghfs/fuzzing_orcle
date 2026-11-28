# ext4-counter-trace + 错误注入

本实验将 Linux 6.12.1 ext4 的公式 checker、逐个错误点追踪和故障序列变异接在一起。
以一个 syzkaller program（一段 syscall 序列）为执行和变异单位，在同一个持续挂载的
文件系统上固定程序、变异故障配置、重新执行。

被测源码为**官方 Linux 6.12.1 加必要插桩**。基础 checker 补丁夹带的功能修复、
额外拒绝条件和操作顺序变化已在本实验第三层补丁中撤销；构建和启动前对整个源码树
重新核对官方清单与补丁哈希。具体恢复项、允许的差异和校验方法见
[源码基线](SOURCE-BASELINE.md)。

## 已实现的行为

- [realistic_review.csv](../../realistic_review.csv) 中全部 **265 条 `realistic=1`**
  记录均已插桩，覆盖 263 个源码位置、31 个文件、28 个翻译单元，保留原始 `site_id`。
- 每次程序执行开始时清空资格。只有在目标 syscall 内**先实际经过公式点**，
  后续实际经过的错误点才可成为故障变异候选。资格跨该程序的 syscall 保留，下一次执行清零；
  公式之前的错误点不会被追溯纳入候选。
- 准备操作、运行时辅助调用、checker 快照、其他任务和其他设备的公式事件不能打开门控。
  原生 syscall 进入/退出边界、事件顺序和内核会话计数独立核对。
- 错误点记录到达、返回、调用上下文指纹、重复经过次数及实际注入。
  **229 条记录支持故障返回**，另外 36 条涉及状态或初始化协议，目前只观测；
  [injection.json](injection.json) 逐条记录具体策略。
  其中 `buffer_uptodate` 是**对象状态**谓词：注入方式是清掉检查所读的那个标志
  （`clear_buffer_uptodate(bh)`，与上游 `ext4_simulate_fail_bh()` 同构），而不是伪造返回值。
  `buffer_write_io_error` / `bh2jh` / `buffer_verified` 没有等价的单标志突变，仍只观测。
- 每个新输入先无故障执行；按实际合格点翻转故障位，继续探索组合故障及错误路径中新出现的点。
  用实际联合轨迹去重，区分计划、实际注入、被阻止和未命中。
- 重放之间保留文件系统变化。每个新输入的目录只准备一次；变体之间不恢复磁盘、
  清理目录、格式化或重新挂载。程序自身的创建、删除、写入正常发生。
- 逐点公式、操作出口和整个程序结束后的稳定快照检查一起运行；正常的故障返回本身不是缺陷。
  结束 campaign 后才卸载并执行 `e2fsck -fn`。
- `mkdir` / `rmdir` / `unlink` / `rename` 的出错出口有运行期模型：以**这次退出实际提交了什么**
  为基准（不是"成功时应有的值"），只读内存状态与内存中的目录块，因此"返回错误但目录项已发布 /
  名字已消失而链接数未减"能在 syscall 当场报 `EXT4 invariant violation`，不必等卸载后的 e2fsck。
  关键判据是"一次失败的 `ext4_mark_inode_dirty()` 不算已持久化"：调用方记录每步更新是否被
  journal 接受，模型据此把内存值折回磁盘上实际留下的值再比对。`group add` 还没有出口模型。
  这三个模型的判定逻辑放在共享基座补丁
  [0001-counter-trace-core-and-probes.patch](../ext4-counter-trace/patches/0001-counter-trace-core-and-probes.patch)
  里，与只做 checker 的 `ext4-counter-trace` 实验共用一份实现；本实验的注入层只做 EFI 门控
  和上游语义还原（`restore_upstream_semantics()` 把基座补丁收紧过的 unlink 顺序改回去，
  缺陷才在这里成立）。

## 运行

需要原实验已校验的 Linux 6.12.1 源码、GCC 14、QEMU、静态 C 编译环境，以及
`work/debian-normal.img`、`work/debian.id_rsa`。正常 fuzzing 还需要现有的
`experiments/ext4-counter-trace/build/ct-syzgen`，构建方法见 [tools/README.md](../../tools/README.md)。
无 KVM 时脚本使用 TCG。在仓库根目录执行：

```sh
python3 experiments/ext4-counter-trace-fault-injection/tests/test-error-sites.py
python3 experiments/ext4-counter-trace-fault-injection/tests/test-injection.py
EXT4_EFI_JOBS=4 bash experiments/ext4-counter-trace-fault-injection/scripts/build.sh

# 门控、跨 syscall、程序重置、组合故障、blocked/not_reached 回归。
python3 experiments/ext4-counter-trace-fault-injection/scripts/run-campaign.py \
  --regression --replays-per-program 6 --duration-seconds 900

# 真正通过 syzkaller 的类型和资源系统生成/变异程序，再探索故障配置。
python3 experiments/ext4-counter-trace-fault-injection/scripts/run-campaign.py \
  --programs 4 --calls 20 --replays-per-program 16 --duration-seconds 3600

# inline_data、512 字节 inode、数据操作模板。
python3 experiments/ext4-counter-trace-fault-injection/scripts/run-campaign.py \
  --inline --template file-state --programs 2 --calls 20 --replays-per-program 4
```

`--replays-per-program` 是无故障基线之外的最大重放次数；队列耗尽时可提前结束。
时限包括启动和生成时间。可用 `--seed`、`--port`、`--program-timeout`、`--rootfs`、`--key`
配置输入和 VM。每个 campaign 新建一次目标盘，整次 campaign 只挂载一次。
`--regression` 使用确定性 C 程序，报告明确标为 fixture，不计为 syzkaller 生成。

## 查看经过的点和实际注入

入口打印 `run_dir=...`。结果位于忽略的 `results/campaign-*/`：

| 文件 | 含义 |
| --- | --- |
| `report.json`、`progress.json` | campaign 结论、进度和输入语料池 |
| `program-NNNN/program.syz`、`original.c`、`workload.c` | 固定输入、原始导出和带运行时的导出，含哈希 |
| `program-NNNN/round-NNNN/plan.json` | 计划注入的 `(call, site_id, occurrence, context)` |
| `joint.jsonl`、`joint.csv` | 实际公式/错误事件顺序；CSV 含源码、caller、callee |
| `session.txt`、`feedback.json` | 实际注入、blocked、not_reached、变异候选及证据完整性 |
| `trace.txt`、`cpu*-stats.txt`、`dmesg*.txt` | 原始事件、丢失统计和内核诊断 |
| `checker/report.json`、`checker/point-report.html` | 生命周期与逐公式报告 |
| `target-before.json`、`target-after.json` | 程序前后的稳定快照 |
| `e2fsck.txt`、`offline.json` | 结束并卸载后的磁盘检查 |

例如 `call=3 eligible=1 allowed=1 injected=1` 表示第 4 个 syscall 内的该次到达
已经获得资格、满足失败条件，并且实际跳过了原调用。计划中有某点不代表注入成功。
已有结果可离线重新生成联合 CSV 和 checker 报告：

```sh
python3 experiments/ext4-counter-trace-fault-injection/scripts/efi_feedback.py \
  experiments/ext4-counter-trace-fault-injection/results/campaign-XXXX/program-0000/round-0000
```

重新探索同一个保存的输入使用 `--program-dir .../program-0000`。它会核对输入哈希、
重新导出并采集当前启动的基线与故障计划。新 campaign 使用新盘，不保证复现旧 campaign
已经变化的磁盘状态；同一次 campaign 内的变体始终共用原挂载状态。

## 文档与边界

[故障注入协议和设计](FAULT-INJECTION.md) 说明资格、失败语义、反馈和会话协议；
[错误点映射与观测](ERROR-SITE-TRACING.md) 说明特殊宏和共享头文件；
[源码基线](SOURCE-BASELINE.md) 说明官方源码及插桩之外的差异约束；
[验证记录](VALIDATION.md) 给出实际构建、QEMU 与覆盖范围。

当前执行器为单 worker、顺序 syscall；上下文采用最多 24 个内核栈帧的 64 位指纹，
用于同次启动内匹配。挂载专用点、配额、加密、verity 等路径需要各自的操作与配置，
短测不代表全部 265 个点都有动态覆盖。

本实验参考 [EH-Fuzz 论文](../../文献/Testing_Error_Handling_Code_With_Software_Fault_Injection_and_Error-Coverage-Guided_Fuzzing.pdf)
的上下文相关错误点与实际错误序列反馈，并增加公式前置门控和持续文件系统状态策略。
联合轨迹反馈并非论文完整的 LLVM 插桩及“排除错误相关基本块后的代码覆盖率”实现。
