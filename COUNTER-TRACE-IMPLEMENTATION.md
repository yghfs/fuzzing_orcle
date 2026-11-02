# ext4 公式检查与密集运行时追踪实施文档

更新时间：2026-10-09

## 目标与实施依据

以当前主线项目为参照，在新的子项目 `experiments/ext4-counter-trace/` 使用重新下载并校验的官方 Linux 6.12.1，依据 `公式.md` 建立新的公式 checker 和时间序列追踪。运行普通 syscall，观察各写入点的 expected、actual 和差值，并区分正常中间态、操作出口差值与静止态持续差值。

**不要求完整复现、移植或启用旧 checker。** 新 checker 的依据是公式的独立计算、ext4 更新语义及适用观测阶段。旧 checker 的输出相同不是正确性的唯一标准。主线项目中的历史 checker 和已有实验结果保留为研究证据与可选对照，不能把它们全部插桩复制到新内核作为必需前提。

本文件引用 `公式.md` 的公式 ID，不重新定义 EA、extent reachability、reservation 等候选专用公式。旧交接文档中“Runtime 只有 block/cluster 增量”与“Mount、目录、持久化均排除”的范围约束已撤销，改为下面六类阶段。

## 六类检查阶段与公式范围

| 阶段 | 公式来源 | 观察与判定 |
| --- | --- | --- |
| 挂载初始化完成 | `M-B/M-I/M-U` | recovery、descriptor 准备及 percpu 初始化完成后，记录初始化来源与结果；独立 bitmap 检查另行计算 |
| Runtime 写入、回滚、发布中间点 | `R-BG/R-BSUM/R-IG/R-ISUM/R-USUM` 与 `R-BDELTA/R-IDELTA/R-UDELTA`，目录引用涉及 `R-LINK` | 在锁安全的位置同步计算可计算的关系，保留一致与不一致；无法完整读取时保留 raw counters 与 unverified reason |
| 操作返回边界 | 对应 Runtime 关系、完整窗口增量与操作状态 | 记录 begin/end、返回值、errno 与异步任务关联，判断相关更新/回滚是否完成 |
| 稳定内存快照 | Runtime 总量、范围、`R-LINK`；`R-UG` 需独立完整 inode 扫描 | 不要求 sync，必须证明观测过程稳定；仅逐组加锁或重复读取不能保证全局同一时刻 |
| superblock 汇总发布完成 | `P-SB` | 对应字段复制完成且计数未变化时检查内存 superblock；不等于磁盘已更新 |
| 可靠持久化/恢复后的磁盘 | `P-DISK` 与适用的离线引用检查 | 采集干净卸载、正确冻结或成功 replay 后的镜像副本，比较 disk superblock/descriptor/逻辑 bitmap |

cluster、inode、目录计数及普通目录引用按新版公式纳入覆盖矩阵。EA inode、extent `i_blocks`、delayed reservation 等历史模型不作为新 checker 的必需组成部分。

每个公式必须标明当前状态为 implemented、pending 或 unsupported，并给出实现或不支持原因。不能因为旧 checker 没有实现某关系，就把它当作已覆盖或不需要。

## 旧实现的选择性复用

| 内容 | 处理方式 |
| --- | --- |
| bitmap 解释、descriptor getter、单位转换 | 对照官方 6.12.1 核对语义后采用；优先使用上游 helper |
| debugfs、事件缓冲、dropped 统计 | 可参考或复用，补齐稳定 ID、时间、操作上下文和明确的未验证事件 |
| global/group 与 bitmap 比较 | 按新版公式重新审查独立依据、并发和初始化处理 |
| shadow、过滤、操作关联与 reset | 根据密集追踪重新设计；不直接沿用中间态过滤或 `op_id=event_seq` |
| EA、extent、reservation、历史后置条件与 JBD2 telemetry | 保留在主线项目快照中；仅在新公式关联需要且审查通过时选择性采用辅助代码 |

每项复用需记录原文件/函数、原基线 hash、修改内容和为何满足新公式。可单独做旧 checker 对照实验，但它不阻塞新 checker 的实现与验收。

## 目录与基线保护

```text
experiments/ext4-counter-trace/
  README.md
  baselines/<时间>/            # 主线项目快照、manifest、hash 与验证结果
  source/                     # 官方 tarball、校验列表、来源与时间
  kernel/linux-6.12.1/         # 独立官方源码；当前步骤保持原始状态
  patches/                    # 后续新实现的分层补丁
  config/                     # 配置副本，不能覆盖旧配置
  scripts/                    # 基线保存、源码准备及后续构建/运行工具
  tests/
  traces/
  results/
```

主线项目的 `kernel/`、`tests/`、`tools/` 和 `work/` 不删除、不移动、不覆盖。项目没有可用 Git 元数据，用 manifest、逐文件 SHA256 和独立归档保存。大镜像必须归档/复制或明确记录独立保存位置，不能只记录一个日后会被覆盖的原路径。

主线项目继续独立维护；快照只固定某个时间点的参照状态，不表示主线项目停用或被新子项目替代。本次已保存的完整范围归档保留一份，后续无需在每轮实验重复归档整个主线项目。

后续按实际复用范围保存源码或补丁、配置、测试脚本、文档和关键证据，并记录来源路径与 SHA256。新实验可重复构建的中间产物无需额外归档；已保存的大型镜像和日志引用不可变快照及其 hash，实验使用独立副本或 overlay，不能写入快照。只记录主线项目中可变文件的路径或 hash 不等于保存其内容。包含 SSH key 的快照目录限定权限，日志不输出 key 内容。不能读取的 staging rootfs 内容要明确列为外部依赖，不能声称已完整备份。

保存后验证归档条目、归档 hash、每个归档文件与 manifest 的 hash，并确认原文件在归档过程中没有变化。恢复只能在独立空目录进行。

## 官方源码与补丁可复现性

1. 从 kernel.org 官方发布目录下载 `linux-6.12.1.tar.xz`，保留确切 URL、下载时间、官方校验列表及 SHA256 比对结果。
2. 原始 tarball 保留在 `source/`；在独立路径解压，确认 `Makefile` 的版本为 6.12.1，并保存源码逐文件 manifest/hash。
3. 盘点旧实现供选择性复用参考；如果比较旧与官方源码，应包含 ext4、JBD2、头文件和构建环境改动，不能假定只改过 checker.c。
4. 完成公式覆盖矩阵后，在干净源码上开发新 checker。建议按 `0001-formula-checker-core.patch`、`0002-write-and-boundary-probes.patch`、`0003-snapshot-and-summary-checks.patch` 等功能层拆分；实际拆分由实现决定。
5. 每份补丁审查上下文、锁序及 ext4 原有语义，并验证在官方源码上的可应用性。不要求生成 `0001-replay-current-checker.patch`。
6. 配置可以从主线项目已有的可工作配置作为参考起点，但重新核对选项与工具链；发现负载禁止编译 ext4 fault injection。独立构建目录和 config 不污染原始源码或主线项目内核。

官方 SHA256 列表通过 HTTPS 下载比对属于来源与传输完整性校验；如果另做 PGP 验签，要记录可信公钥来源与验签结果，不能把“下载了签名文件”写成“已验签”。

## 公式覆盖矩阵先于插桩

覆盖矩阵至少包含：

- 公式 ID、actual 变量与单位、expected 的独立来源；
- 所有直接/间接写入、bitmap bit 转换、helper、初始化、回滚、resize、损坏 group 排除和后台路径；
- 写入前后源码位置、相邻发布步骤、锁、JBD2 handle/tid、调用路径；
- point ID、对应检查阶段、能否当场完整计算；
- operation/syscall 返回边界、异步任务关联、快照有效性条件；
- 插桩状态与明确的 unsupported/unverified 原因。

用代码搜索找到赋值、++/--、atomic/percpu 操作、批量 bitmap 更新、memset/memcpy 和 helper 封装，然后人工复核。不能只改最显眼的 setter，也不能合并 bitmap、descriptor 与 global 为一个“allocation 完成”点。

增量 expected 从真实资源转换独立推导，不从被测计数的差值反推。请求长度、syscall 成功次数不是实际分配量；全局增量窗口必须包含全部相关并发更新。初始化、容量增加和 corruption adjustment 需使用各自明确的转换模型。

## Probe 与边界记录

统一 API 的具体签名由新实现决定。每条事件至少包含：

```text
event_seq timestamp_ns pid comm cpu operation_id parent_operation_id
jbd2_tid formula_id point_id stage inode group expected actual delta
update_delta locks_held transaction_state result syscall_errno
unverified_reason snapshot_validity
```

`delta=actual-expected`；`update_delta` 单独表示计数更新量。记录一致、不一致及未验证事件，不得只保存 violation。无法计算 expected 时明确缺失，不能用 0/0 伪装一致。

事件序号在记录范围内单调，reset/分段导出必须有 run/epoch 标识，不能产生无法区分的重复序列。operation ID 串联同一操作，PID、inode 或 JBD2 tid 单独都不是唯一操作标识。后台 writeback 需明确关联或标为异步/无法归属。

挂载与 superblock 发布使用独立阶段事件；操作 begin/end 记录返回结果。syscall 返回与 ext4 operation end 有不同范围，必须说明哪些路径已精确关联，不能把 journal stop 当作 syscall 返回。

导出原始机器可读记录及每点 hit/consistent/mismatch/unverified 统计。缓冲区覆盖、导出失败或其他丢失都需可检测，并保留 dropped 计数；有丢失的运行不能宣称轨迹完整。读取 trace 应尽量与执行全量快照分开，明确观测行为本身产生的事件。

## 锁与快照正确性

持有 group lock、inode/ES/reservation lock 或 journal 临界区时，不能重复获取同一锁、反向加锁或调用需要睡眠的全量扫描。

可以安全读取时直接计算；不能完整计算时记录 raw counters，并在紧接着的安全位置补采样。局部检查、全局扫描分别记录有效性，不能把不同时间点的组读数伪装成同一快照。

活动计数、序列校验或其他稳定性机制必须覆盖实际写入和发布窗口，checker 自己的锁不能替代 ext4 同步。扫描失败、并发、格式不支持不静默丢弃。checker 不改变 ext4 计数更新、错误处理、journal 顺序和用户可见语义。

## Workload 与时间序列分析

使用普通 `open/create/unlink`、`mkdir/rmdir/link/rename`、`write/pwrite`、`fallocate`、hole punch、`truncate/ftruncate`、`fsync/syncfs`，覆盖新版 Runtime 公式的 cluster、inode、目录与引用变化。普通与 bigalloc 分开运行。EA feature 镜像仅按是否帮助覆盖本阶段公式决定，不为旧 EA 模型验收而强制加入。

先单 worker 验证点位与阶段，再多 worker 和持续压力。自然 ENOSPC/EDQUOT/ENOMEM 是普通错误路径覆盖；ext4 fault injection 和 dm-error 属于独立错误验证，不混入健康存储发现统计。

每轮保存配置、源码/补丁 hash、syscall 返回、trace、kernel log、原始镜像及 baseline。停止相关更新后采集稳定内存快照，再持久化、卸载或可靠冻结；离线检查采用镜像副本，需 replay 时保存前后证据。一次 sync/fsync 返回不单独证明全部 home blocks 与内存同一状态；fsck 同时检查诊断文本与退出码。

分析按 `(formula_id, point_id)` 聚合，再按 operation/inode/group/event_seq 关联，输出命中率、差值分布、时间窗、一致性突变与 mismatch 出现到恢复的点序列。分别标注 `transient_mismatch`、`operation_exit_mismatch`、`persistent_mismatch`、`concurrent_or_ambiguous`、`unsupported_or_unverified`。

瞬时 mismatch 不直接称漏洞；持续 mismatch 也需源码审计、普通 syscall 证据、独立检查及上游版本复核。

## 实施阶段与验收

### Phase 0：主线项目快照

固定实验参照的时间点和范围，保存 manifest、hash、必要内容及恢复说明。本次已有快照可直接引用，不需重新完整备份。主线项目继续维护，快照内容保持不变；不能读取的外部依赖单独列明。

### Phase 1：干净官方源码准备

下载并与官方 SHA256 比对，在独立目录解压，验证版本与源码 manifest。**此阶段无需旧 checker 移植、构建或回归，也不需要新 checker 已实现。**

### Phase 2：覆盖矩阵与复用审查

初版矩阵已落在 `experiments/ext4-counter-trace/FORMULA-SOURCE-COVERAGE.md`：逐项列出公式、权威字段、独立 expected、写入/发布路径、锁与事务、操作边界和限制。它已单列 lazy bitmap 初始化、corrupt-group 排除、online resize、bigalloc rereserve、fast-commit replay 与 `R-LINK` 的特殊语义。

矩阵 review 已完成，结果、API、稳定快照证明条件与剩余覆盖见 `experiments/ext4-counter-trace/PROBE-API-SNAPSHOT.md`。复核已纠正 FC replay 的 `EXT4_MB_SYNC_UPDATE` 语义以及 inode-free 目录 global 更新的持锁位置；独立 expected 使用实际 bitmap bit 转换，不直接复用普通路径默认等于请求长度的 changed。不要求为每条字段赋值或每个上层调用者分别插桩，也不要求完整复现主线项目的历史 checker。

### Phase 3：新 checker 与 probe

实现公式计算、点位、begin/end、稳定快照、superblock 发布与可审计导出；保存分层补丁。构建并先验证小负载、解码及 loss detection。

第一版核心、普通 block/inode 更新窗口、namespace 外层边界、五条稳定内存总量公式、M-* 来源比较和 P-SB 已实现。独立开发树位于 `experiments/ext4-counter-trace/dev/linux-6.12.1/`，第一版补丁保存在 `patches/0001-counter-trace-core-and-probes.patch`，已在隔离目录重放并逐文件校验。开启配置的完整 GCC 14 构建和关闭配置的 ext4 构建均通过；GCC 15 对上游 ACPI 的新字符串诊断记录在失败日志中。

### Phase 4：运行、分析与磁盘对照

覆盖普通/bigalloc、命名空间和正常/资源失败路径；完成轨迹、出口/静止态分类及持久化检查报告。

VM 验证已覆盖普通、bigalloc、无 journal、lazy init、四 worker、并发扫描、非零保留分配、自然 ENOSPC 和命名空间操作。sync 前稳定快照、独立增量、轨迹完整性及卸载后 fsck 均通过；关机确认后由 `scripts/check-disk.py` 完成 `P-DISK` 与离线 `R-UG/R-LINK` 检查，四种镜像通过。运行时 `R-UG/R-LINK` 全 filesystem 稳定扫描、dirent/nlink 子步骤事件，以及 corruption、FC replay、resize、bigalloc rereserve 的独立 expected 和定向命中验证已实现并通过，详见 `experiments/ext4-counter-trace/VALIDATION.md`。跨线程 operation parent、准确 syscall 返回关联和 `BLOCK_UNINIT` 离线布局仍 pending；并发扫描不能证明稳定时按协议返回 `unverified`。

最终验收：

1. 主线项目快照可恢复，快照校验通过；不能读取的依赖明确说明。
2. 新内核源自官方校验通过的干净 6.12.1，新 checker 的补丁与配置独立管理。
3. 新版公式及六类阶段均有覆盖状态，每个相关写入点有 probe 或明确理由。
4. expected 来源独立，单位正确，观测有效性、操作边界、异步关联与丢失可审计。
5. 一致、不一致、未验证样本均保存；普通 syscall 真实命中点位并生成序列分析。
6. 中间态、出口和静止态分开判读；磁盘比较满足同一持久状态要求。
7. 发现关闭 fault injection；不以“旧 checker 完整复现”或“输出相同”作为验收条件。

## 本次准备结果

本轮主线项目快照保存在 `experiments/ext4-counter-trace/baselines/20261009-130905+0800/`，归档全部条目和文件内容已校验。`work/debian/` 的 root 所有 staging tree 未完整归档，作为原位置外部依赖记录；可启动 rootfs 镜像已包含在归档。官方来源、SHA256、源码验证记录、恢复命令与当前未实施事项见 `experiments/ext4-counter-trace/README.md`。

主线项目 build #179 的 ordinary/bigalloc/EA count workload 适用模型零违例、fsck 干净，但旧事件 ring 有覆盖；只作为历史回归参考。自然内存压力 extent/EA 候选证据仍保存在主线项目 `work/` 与快照，不作为新版公式 checker 必须复现的验收项。

## 后续任务说明

```text
先阅读新版公式.md 与本实施文档。确认主线项目快照和官方源码校验结果已保存，在独立实验目录继续工作，不覆盖主线项目。新 checker 按新版公式及六类检查阶段设计；旧 checker 仅选择性复用，不要求完整移植。先完成公式—变量—独立 expected—写入点—锁/事务—返回边界覆盖矩阵，再添加密集 probe。记录一致、不一致、未验证事件，明确 global 并发、bigalloc 延迟发布与后台路径，禁止死锁、改变 ext4 语义或静默丢 trace。使用关闭 fault injection 的普通 syscall，完成构建、运行、时间序列与持久化对照，并按中间态/出口/静止态分类。
```
