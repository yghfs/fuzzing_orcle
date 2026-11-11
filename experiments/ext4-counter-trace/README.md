# ext4 公式检查与运行时追踪实验

更新时间：2026-11-11（Asia/Shanghai）

依据根目录 `公式.md` 和 `COUNTER-TRACE-IMPLEMENTATION.md`，在独立官方 Linux 6.12.1 上开发新的子项目 checker。主线项目中的历史 checker 仅作选择性复用参考，不要求完整移植或先运行历史 checker 回归。

当前第一条“逐点差值 → 操作出口 → 稳定态”链已实现并完成有限 VM 验证。
实现和适用边界见 [FIRST-CHAIN.md](FIRST-CHAIN.md)。原始 chain、短测和三小时单 worker
`campaign-lhze43mu` 目录已清理；其历史结论见根目录 `汇报.md` 和
[候选复核摘要](research/2026-10-31-single-random-fault/lhze-candidates/bug-adjudication.md)。

第二条“长时采集 → 轮转 → 分段/整轮分析 → 可靠持久化”运行链也已实现，
说明见 [SECOND-CHAIN.md](SECOND-CHAIN.md)。历史双 worker 的
`campaign-k3gxmeex`、其 postreport 和 `campaign-fmxs5w3e` 原始目录已清理。
当前保留的一小时三臂对照和十二小时两条自定义臂审计见
[覆盖审计](research/2026-11-11-target-coverage/README.md)；存在结果目录不等于整轮通过。

根目录 `公式.md` 后续新增的 `R-IH`、`R-IEVICT`、`R-IDELTA-EVICT`、
`O-LIFE`、`O-LIFE-DELTA`、`O-LIFE-FREE`、`XATTR-REF` 和
`XATTR-REF-DELTA` 已接入运行时 checker。实现边界与定向证据见
[新增公式实现](FORMULA-RELATIONS-IMPLEMENTATION.md)；文中关系短测的原始运行目录
已清理，当前保留运行以 `IMPLEMENTATION-STATUS.md` 和根目录 `BUG-INVENTORY.md` 为准。

当前 campaign 的批次操作序列由仓库内固定负载替换为 syzkaller 生成：
`syzgen` 使用 upstream syzkaller 的 typed `Generate`/`Mutate` API，精确限制在
文件系统相关 syscall 及其显式变体；每个批次保存 `program.syz`、原始/插桩 C、
二进制、seed、调用号、调用来源和 SHA256。`resource-valid` 槽首次从 typed seed 开始；
resource parent 入库后，后续 resource-valid 槽通过 syzkaller 的 `Prog.Mutate` 继续变异
父程序的资源图，不再把固定模板与独立生成的 tail 拼接。需要补足精确调用预算时，元数据将
`typed_mutation` 与 `independent_fill` 分开记录；模板名从父程序 sidecar 继承。

KCOV 与 native syscall 生命周期按 `(program, call_index)` 关联。默认 effect-aware admission
区分目标设备上的成功/失败、修复/改写调用、非目标设备调用及未核验调用；只有目标设备上
未修复/未改写调用的 PC、edge 和语义特征可作为 corpus 准入和父代收益。可启用
`--require-target-success`，进一步要求至少有一个成功且未改写的目标设备调用。所有完整样本的
原始覆盖仍单独保留作诊断。历史两段、32 syscall 的 VM 验证（`campaign-0_6qrnow`，原始目录已清理）记录为通过：
2/2 KCOV 样本完整，0 个无效/缺失/饱和样本；32/32 个 native syscall 已核验。两个程序均满足
effect 准入。第一段由 resource-valid typed seed 经 `ResourceSeedMutate` 产生，包含 5 个
typed mutation 调用和 11 个保留 seed 调用；第二段从已准入资源父程序执行 `ResourceMutate`，
资源父程序被选择 1 次，包含 3 个 typed mutation 调用和 13 个保留的父调用，连续未变 seed
前缀分别为 11 和 1 个调用。32 个调用中，23 个为目标设备成功且未改写、1 个为目标设备失败且
未改写、2 个为目标设备成功但经过改写、6 个为非目标；没有未核验调用。ext4/JBD2 原始、
效果限定和已准入 corpus 目标覆盖均为 2,391 PC（ext4 2,115，JBD2 276）；全内核原始覆盖为
6,550 PC，效果限定/准入 corpus 覆盖为 6,535 PC。此短测证明逐调用反馈和 typed mutation
路径接通，不构成长时间覆盖收益结论。

这条路径仍不是 syzkaller manager 的多 VM 反馈服务。长时间运行前应继续检查逐调用 syscall
结果、目标设备身份、资源关系和 KCOV 累计完整性证据。

`concurrent` campaign 段现在让多个 worker 共用一个预置工作目录；所有 worker 在 `ct_init`
完成后经 barrier 同时开始。它们使用相同的 namespace seed 前缀，在共享目录中竞争 `source`、
rename/link 目标，并操作同一目录 inode。worker 全部退出后先采集 workload 稳定快照，再由
launcher 清理共享目录并采集 post-cleanup 快照。生命周期报告记录交错 operation、worker、
native syscall 和共同 inode/目录 inode；同 inode 状态遇到并发写入后会隔离，直到有效稳定扫描
重新锚定。该路径已完成十小时双 worker VM 长跑，全局交错事件归因仍有待完善。

## Native syz-manager ext4 覆盖对照

三臂对照由 `scripts/run-target-coverage-comparison.sh` 并行运行 native `syz-manager`、
PC-only 和 full-feedback 自定义单 worker。两条自定义臂启用相同的随机错误策略，默认
`1000 ppm` 并在命中后隔离 workload；`--random-fault-probability-ppm` 可在短测中提高概率。
native seed 用私有 seed-only syzkaller view，seed 内嵌 raw ext4 镜像与另外两臂的 regular
image 做 SHA-256 对照。只有 seed 执行证据、非空目标 rawcover、完整自定义 KCOV 和目标文件
系统操作证据均有效，比较报告才会标为完整；executor `SYZFAIL` 会阻止完整判定。

上游 syzkaller 源码位于本机忽略目录 `tools/src/`。raw KCOV 饱和程序可能超过 executor
原先 6 MiB 输出区；使用前先应用本仓库跟踪的补丁并同步重建 manager/executor：

```sh
cd tools/src/syzkaller-master
patch -p1 < ../../../experiments/ext4-counter-trace/patches/0002-syzkaller-rawcover-output-buffer.patch
make TARGETOS=linux TARGETARCH=amd64 manager executor
```

补丁把 coverage 输出区扩大到 14 MiB，匹配 executor comparison 输出容量。60 秒验证未再
出现 `SYZFAIL: can't reallocate`；长跑仍需持续检查 executor failure 计数。

KCOV 反馈闭环的实现说明见 [KCOV-FEEDBACK.md](KCOV-FEEDBACK.md)。旧的 E2/E3 协议、结果和历史
运行记录已清理；后续的一小时三臂对照和十二小时自定义臂审计另见
[覆盖审计](research/2026-11-11-target-coverage/README.md)。不同实验不得合并为一次有效性结论。
反馈和 syzkaller 短测的历史运行目录已清理；当前保留的公式链和 campaign 报告只用于追踪器回归，
不作为 bug 证据。

逐点报告由 `scripts/point_report.py` 生成，CSV/JSONL 保存差值及原始 trace 位置；
现场和重建比较分别统计，带入锚点不重复计数。旧 `campaign-lhze43mu` 的
point-report.md/html 已清理，当前可用结果以覆盖审计及实际存在的目录为准。

```sh
bash experiments/ext4-counter-trace/scripts/build.sh
python3 experiments/ext4-counter-trace/scripts/export-patch.py
bash experiments/ext4-counter-trace/scripts/run-chain.sh
python3 experiments/ext4-counter-trace/tests/test-lifecycle.py
bash experiments/ext4-counter-trace/scripts/run-campaign.sh --duration-seconds 3600 --segments 10000
python3 experiments/ext4-counter-trace/tests/test-campaign.py
python3 experiments/ext4-counter-trace/tests/test-disk-layouts.py
python3 experiments/ext4-counter-trace/tests/test-points.py
python3 experiments/ext4-counter-trace/tests/test-syzkaller.py
```

## 单 worker 随机错误注入

长跑可启用按种子复现的运行时错误注入。默认只抽样已经完成单 worker 恢复闭环的
`mballoc_mark`、`unlink_inode_dirty` 和 `inode_evict_final_dirty` hook；概率按每次经过
候选 hook 的调用计算，默认 `1000 ppm`（0.1%）。默认 errno 策略是 `native`，也可以按 hook 白名单指定 errno；
`native` 表示命中时从当前 hook 的错误集合中按种子选择。错误只影响命中的那次 ext4 调用，
不代表发现漏洞。单 worker 随机注入默认启用命中隔离：触发 syscall 返回后立即截断当前
workload，执行正常同步、卸载和该 segment 的离线 `e2fsck -fn`，再把受影响 filesystem
标记为 tainted 并在下一个 segment 前重置。这样单次 fault 的持久化结果不会被后续 syscall 覆盖。
运行 `--no-fault-isolation` 可保留连续 workload 形态，但这类结果只作为探索材料。
隔离检查结束后 runner 会恢复全局 `ext4_counter_event`，确保后续 segment 仍有计数器事件。
如果前一段分析失败且没有 bounded carry trace，runner 会记录 `missing_carry_trace` 并让后续段
只使用自己的原始 trace；该证据缺口会在最终报告中保留，不会因 carry 文件缺失直接中止长跑。
无注入 workload 正常结束后，runner 检查稳定快照并把不一致记为候选，然后继续下一个 workload；
整轮运行仍受时长、segment 上限和基础设施错误约束。结束时再对镜像做离线 fsck 与磁盘检查，
命中的 segment、稳定点、lifecycle 和离线磁盘异常会汇总到 campaign 报告中供复核。
每个 segment 只运行一个带注入策略的 workload batch；策略由 worker 在准备好测试目录后、
执行 syzkaller workload 前启用，并限制到该 worker 的 PID，workload 结束后立即关闭。
前置和后置稳定快照不参与随机抽样。

```sh
bash experiments/ext4-counter-trace/scripts/run-campaign.sh \
  --mode single \
  --duration-seconds 36000 \
  --segments 100000 \
  --segment-seconds 10 \
  --max-batches 1 \
  --batch-timeout 60 \
  --workers 1 \
  --cpus 2 \
  --random-fault-injection \
  --random-fault-probability-ppm 1000 \
  --seed 20261025 \
  --port 11884
```

这项长跑是“最多运行 10 小时，或达到其他运行上限/基础设施停止条件时结束”。campaign
报告分别统计 hook 命中、隔离状态、每次命中的离线检查和待复核候选；仅命中不会被当作漏洞发现。
提高概率可用
`--random-fault-probability-ppm 10000`；指定候选 hook 用
`--random-fault-operations mballoc_mark,inode_evict_final_dirty`。`fast_commit_inode_claim` 需要
crash/replay mount，不在这个普通 workload 随机池中。

各运行时 hook 的可选 errno 和来源见 [fault-errno-catalog.json](fault-errno-catalog.json)。
`mballoc_mark` 支持 `EIO, ENOMEM, EINVAL, ENOSPC, EROFS, EFSCORRUPTED, EFSBADCRC`；
`inode_evict_final_dirty` 支持 `EIO, ENOMEM, EINVAL, ENOSPC, EROFS, EFSCORRUPTED`。
其中 `EUCLEAN` 是 `EFSCORRUPTED` 的内核别名，`EBADMSG` 是 `EFSBADCRC` 的内核别名，
每个候选数值只登记一次，避免同一列表被内核按重复 errno 拒绝。
用 `--random-fault-errnos native` 可从当前 hook 的集合抽取；`--random-fault-errnos EIO,ENOSPC`
会对所选 hooks 共用这组错误。不属于 hook 集合的 errno 会在 arm 前拒绝。修改目录后运行
`python3 experiments/ext4-counter-trace/scripts/generate-fault-errno-header.py` 更新内核查表。
名称也可以写成 catalog 中的负数值，例如 `-5,-28,-117,-74`；host 会先归一化为
`EIO,ENOSPC,EFSCORRUPTED,EFSBADCRC`。
`EFSCORRUPTED` 和 `EFSBADCRC` 是 ext4/jbd2 使用的内核别名，trace/status 中记录其实际数值
`-117`（`EUCLEAN`）和 `-74`（`EBADMSG`）；它们不一定有 libc 符号名。
例如，只让 `mballoc_mark` 从自己的完整集合抽取：`--random-fault-operations mballoc_mark --random-fault-errnos native`。若只想注入 checksum 错误，可改成 `--random-fault-errnos EFSBADCRC`；同一个选项用于 inode hook 会在启动前被拒绝。
同一轮需要给不同 hook 使用不同子集时，用分号分隔的按 hook 形式，例如
`--random-fault-errnos 'mballoc_mark=EFSBADCRC;inode_evict_final_dirty=native'`。
按 hook 形式必须覆盖 `--random-fault-operations` 中的每个 hook，且每个列表都会按该 hook
自己的 catalog 白名单校验；命中后的 status 会带 `errno_operation` 和该 hook 实际候选列表。
同一 catalog 同时驱动定向 hook 的内核白名单、随机 errno 选择和证据校验；新增 hook 时需要登记每个 errno 的原生来源、传播路径和错误出口，生成头文件后由 runner 拒绝不支持的组合。命中证据还会核对实际 errno、choice index/draw 与 status 回读，缺少这些字段会判为未验证。`inode_evict_final_dirty` 返回的是 `ext4_mark_inode_dirty()` 的内部错误；其外层 `ext4_evict_inode()` 为 `void`，所以该错误不会成为 `close()` 或 `unlink()` 的 syscall errno。

## 本次工作范围与状态

- 实施文档已更新为六类检查阶段：Mount、Runtime 中间点、操作出口、稳定内存快照、内存 superblock 汇总发布、可靠持久化/恢复后的磁盘。
- 主线项目快照已保存并完成归档内容校验。
- 官方源码下载、官方 SHA256 比对、解压及逐文件校验已完成；结果见 `source/provenance.json` 和 `source/verification.json`。
- 初版公式—源码覆盖矩阵已完成：`FORMULA-SOURCE-COVERAGE.md`。它按六类检查阶段列出权威字段、独立 expected、写入/发布点、锁和事务边界，以及 lazy init、corrupt group、resize、bigalloc、fast-commit replay 和 link-count 的特殊语义。
- 矩阵 review、probe API 和稳定快照协议已实现第一版，见 [PROBE-API-SNAPSHOT.md](PROBE-API-SNAPSHOT.md)。开发树在 `dev/linux-6.12.1/`，官方原始树仍通过 SHA256 清单校验。
- 新 checker 使用 `CONFIG_EXT4_COUNTER_TRACE`，已完成开启配置的完整 GCC 14 构建、关闭配置的 ext4 构建，以及普通/bigalloc/无 journal/lazy init 的 VM smoke。这是历史构建/VM 验证摘要；当前源码与构建绑定以 `scripts/source_audit.py --require-build` 为准，证据状态见 [IMPLEMENTATION-STATUS.md](IMPLEMENTATION-STATUS.md)。
- 运行时全 filesystem `R-UG/R-LINK` 扫描已实现：`counter_snapshot` 会遍历 inode bitmap、
  `ext4_iget()` 读取每个 inode、统计目录 inode 并扫描目录数据块中的 dirent 引用，再与
  `i_nlink` 比较；扫描期间出现任何 writer（epoch/version/active 变化）时结果记为
  `namespace_valid=false` 而不是误报。
- namespace 已按实际 dirent 引用转换与 nlink 子步骤插桩：`NAMESPACE.DIRENT.ADD/DELETE`、
  `NAMESPACE.NLINK.INC/DEC/CLEAR/ROLLBACK`，tracepoint 新增 `nlink_before/after`、
  `dir_nlink_before/after` 和 `step` 字段，schema 2 另有 applied `ref_delta`、`dir_ino`、
  独立 `result`、inode generation/birth/death。
- 特殊路径各自带独立 expected，并由 `scripts/run-special.sh` 做定向命中验证：
  bigalloc rereserve、online resize、fast-commit replay 和 corruption exclusion。
  历史命中范围见 [FORMULA-SOURCE-COVERAGE.md](FORMULA-SOURCE-COVERAGE.md) 和 [FORMULA-RELATIONS-IMPLEMENTATION.md](FORMULA-RELATIONS-IMPLEMENTATION.md)；已清理的原始运行不作为本轮重验。

## 第一版构建与验证

```sh
bash experiments/ext4-counter-trace/scripts/build.sh
bash experiments/ext4-counter-trace/scripts/run-smoke.sh
bash experiments/ext4-counter-trace/scripts/run-special.sh
python3 experiments/ext4-counter-trace/scripts/export-patch.py
```

`build.sh` 在独立输出目录使用保存的配置和 GCC 14。原始源码位于 `kernel/`，开发副本位于 `dev/`；新增 tracepoint、API 与快照代码不复制主线项目的历史 checker。
补丁与逐文件 SHA256 位于 `patches/`，重放验证在独立临时目录完成。
快照读取 `/sys/fs/ext4/<device>/counter_snapshot`，序号端点读取 `counter_trace_state`；
原始事件通过 tracefs 的 `ext4_counter:ext4_counter_event` 导出。

special 路径的原始运行目录已清理；其 fast-commit replay `R-UG` 持久差异由 [finding report](FAST-COMMIT-DIR-COUNT-FINDING.md) 单独保留。报告覆盖该路径的触发条件、离线 checker 和 `e2fsck` 结论。

此前两次长跑产物 `campaign-pim9b6u4` 与 `campaign-3cp9_siz` 已按要求清理，长跑历史指标不再在此维护。
`campaign-0mwk51uk` 的 12 小时历史结果也已清理，仅保留调度审计摘要。后续将重新运行
PC-only、PC+edge、PC+语义和联合反馈长跑，并以新 campaign 报告记录每种反馈配置的覆盖收益与完整性。

## 2026-10-18 收尾修复

runner 现在在采集停止后设置 `finalizing=true`，checkpoint、最后一次取回、逐点分析、正常关机和
离线校验不再被 collection wall watchdog 截断；`scp` 单次超时为 300 秒，仍保留 4 次有限重试。
逐点汇总也先使用当前段的生命周期时间线，只有当前段存在未匹配原始事件时才读取下一段的 carry 时间线，
减少长跑中的重复扫描。42 个 campaign 回归测试通过；需要重新运行长跑才能取得新的完整 12 小时证据。

## Source and generated files

The historical multi-gigabyte project snapshot, source trees, VM images,
credentials, and campaign output are machine-local and are not part of this
Git repository. A clean checkout rebuilds Linux 6.12.1 from the verified
kernel.org archive using the tracked patches and configuration. Use
`tools/bootstrap.sh` from the repository root to prepare a new host.

## 官方源码准备

官方来源：`https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.1.tar.xz`。

官方校验列表：`https://cdn.kernel.org/pub/linux/kernel/v6.x/sha256sums.asc`。

该 release 的官方 SHA256：

```text
0193b1d86dd372ec891bae799f6da20deef16fc199f30080a4ea9de8cef0c619
```

`download-official.py` 使用 HTTPS 分段下载、验证 Content-Range 后合并，并与官方校验列表比对完整 tarball。下载时间、URL、大小和结果保存在 `source/provenance.json`。已获取的校验列表保留供核对；PGP 签名未验签，不将 HTTPS/SHA256 比对描述成 PGP 验签。

`prepare-source.py` 拒绝覆盖现有源码树，检查 archive 路径后解压至：

```text
experiments/ext4-counter-trace/kernel/linux-6.12.1/
```

若源码目录已经存在，脚本只校验、不覆盖文件。它确认 `Makefile` 版本为 6.12.1，逐个比较解压后内容与官方 tarball，检查无额外文件，并生成 `source/pristine-manifest.jsonl`、`source/pristine-files.sha256` 和 `source/verification.json`。新树应无旧 checker.c、.config 或 bzImage；原有内核位于根目录 `kernel/`，路径互不覆盖。

准备命令（下载可续传；已有 tarball 会校验后复用，已有源码树只校验、不覆盖）：

```sh
python3 experiments/ext4-counter-trace/scripts/download-official.py
python3 experiments/ext4-counter-trace/scripts/prepare-source.py
```

本轮验证通过：版本 6.12.1，共 92,438 个条目、86,618 个普通文件；全部文件内容、权限和 symlink 目标与官方 tarball 一致，无额外文件，新树无 checker.c、.config、bzImage。

准备完成后重新校验源码：

```sh
cd kernel
sha256sum -c ../source/pristine-files.sha256
```

`environment.json` 记录本地 GCC 14/15、QEMU、e2fsck、tar、Python 的路径与版本。当前没有 `/dev/kvm`，本轮 VM 使用 TCG。配置已保存到 `config/counter-trace.config`，构建输出与官方树分离；当前验证采用 GCC 14。
