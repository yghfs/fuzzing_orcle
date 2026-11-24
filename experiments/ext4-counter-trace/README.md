# ext4 公式检查与运行时追踪实验

更新时间：2026-11-24（Asia/Shanghai）

2026-11-24 已清空 `results/` 中的历史结果。短测和历史验证的结论见
[实现状态](IMPLEMENTATION-STATUS.md)。

依据根目录 [`公式.md`](../../公式.md) 和 [实施文档](../../COUNTER-TRACE-IMPLEMENTATION.md)，
在校验过的官方 Linux 6.12.1 上实现统一 checker，由 `CONFIG_EXT4_COUNTER_TRACE` 启用。
位图、描述符、目录链接、orphan 槽和共享 xattr 使用新版稳定扫描；旧版独有的
extent/间接块可达性、extent status、reservation、EA inode、flex、buddy、延迟释放和
事务/操作后置条件作为补充模型。

## 普通构建和回归

在[根目录](../../README.md)完成宿主和 guest 准备后运行：

```sh
bash experiments/ext4-counter-trace/scripts/build.sh --normal
bash experiments/ext4-counter-trace/scripts/run-smoke.sh
bash experiments/ext4-counter-trace/scripts/run-chain.sh
```

`build.sh` 使用 `dev/linux-6.12.1/` 源码构建统一 checker，输出到
`build/linux-counter-trace-normal/`。`--normal` 可省略。普通回归、专项测试和
覆盖对照都默认使用这套构建。路径和环境变量见 [tools/README.md](../../tools/README.md)。

两项回归默认使用 `work/debian-normal.img`。该镜像由 `tools/create-vm-image.sh` 从
Debian 基础镜像生成，在副本上关闭 ext4 的 `orphan_file` 特性并做一致性校验。
smoke 检查 trace、稳定快照和模型统计。两个读取入口使用同一套稳定扫描：
`/sys/fs/ext4/<device>/counter_snapshot` 输出 JSON；
`/sys/kernel/debug/ext4/<device>/invariant_summary` 追加较重的 EA、flex 和分配器等模型，
并汇总每项的检查、违例和未验证次数。`invariant_check` 同时输出事务及操作诊断。

## 追踪与采集

当前第一条“逐点差值 → 操作出口 → 稳定态”链已实现并完成有限 VM 验证。
实现和适用边界见 [FIRST-CHAIN.md](FIRST-CHAIN.md)。第二条长时采集、轮转和整轮分析链
见 [SECOND-CHAIN.md](SECOND-CHAIN.md)。原始运行均已清理，重新验证需运行下方命令。

根目录 `公式.md` 后续新增的 `R-IH`、`R-IEVICT`、`R-IDELTA-EVICT`、
`O-LIFE`、`O-LIFE-DELTA`、`O-LIFE-FREE`、`XATTR-REF` 和
`XATTR-REF-DELTA` 已接入运行时 checker。实现边界与定向证据见
[新增公式实现](FORMULA-RELATIONS-IMPLEMENTATION.md)；文中关系短测的原始运行目录
已清理；当前实现与历史证据边界见 `IMPLEMENTATION-STATUS.md` 和根目录 `BUG-INVENTORY.md`。

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
原始覆盖在每次新运行中单独保存作诊断。模板父本按模板选择，每个模板有独立 seed
刷新份额；目标 edge 的准入、rarity 和子代收益遵循配置的 ext4/JBD2 范围。

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
PC-only 和 full-feedback 自定义单 worker。两条自定义臂使用相同的初始镜像和负载配置。
native seed 用私有 seed-only syzkaller view，seed 内嵌 raw ext4 镜像与另外两臂的 regular
image 做 SHA-256 对照。只有 seed 执行证据、非空目标 rawcover、完整自定义 KCOV 和目标文件
系统操作证据均有效，比较报告才会标为完整；executor `SYZFAIL` 会阻止完整判定。

上游 syzkaller 源码位于本机忽略目录 `tools/src/`。raw KCOV 饱和程序可能超过 executor
原先 6 MiB 输出区；本机副本已应用 14 MiB 补丁。它没有 Git 元数据，安装脚本只能核对
项目的版本声明，不能确认本地副本的上游提交。手动准备未修改的 syzkaller 源码时，可在构建前执行：

```sh
cd tools/src/syzkaller-master
patch -p1 < ../../../experiments/ext4-counter-trace/patches/0002-syzkaller-rawcover-output-buffer.patch
make TARGETOS=linux TARGETARCH=amd64 manager executor
```

补丁把 coverage 输出区扩大到 14 MiB，匹配 executor comparison 输出容量。当前副本的
`kMaxOutputCoverage` 已核对为 `14 << 20`；60 秒验证未再
出现 `SYZFAIL: can't reallocate`；长跑仍需持续检查 executor failure 计数。

KCOV 反馈实现和数据口径见 [KCOV-FEEDBACK.md](KCOV-FEEDBACK.md)。逐点报告由
`scripts/point_report.py` 生成，CSV/JSONL 保存差值及原始 trace 位置；现场和重建比较
分别统计，带入锚点不重复计数。当前没有保留的原始 campaign 或覆盖对照结果。

```sh
python3 experiments/ext4-counter-trace/scripts/export-patch.py
python3 experiments/ext4-counter-trace/tests/test-lifecycle.py
python3 experiments/ext4-counter-trace/tests/test-campaign.py
python3 experiments/ext4-counter-trace/tests/test-disk-layouts.py
python3 experiments/ext4-counter-trace/tests/test-points.py
python3 experiments/ext4-counter-trace/tests/test-syzkaller.py
```

## 本次工作范围与状态

- 实施文档已更新为六类检查阶段：Mount、Runtime 中间点、操作出口、稳定内存快照、内存 superblock 汇总发布、可靠持久化/恢复后的磁盘。
- 主线项目快照已保存并完成归档内容校验。
- 官方源码下载、官方 SHA256 比对、解压及逐文件校验已完成；结果见 `source/provenance.json` 和 `source/verification.json`。
- 初版公式—源码覆盖矩阵已完成：`FORMULA-SOURCE-COVERAGE.md`。它按六类检查阶段列出权威字段、独立 expected、写入/发布点、锁和事务边界，以及 lazy init、corrupt group、resize、bigalloc、fast-commit replay 和 link-count 的特殊语义。
- 矩阵 review、probe API 和稳定快照协议已实现第一版，见 [PROBE-API-SNAPSHOT.md](PROBE-API-SNAPSHOT.md)。开发树在 `dev/linux-6.12.1/`，官方原始树仍通过 SHA256 清单校验。
- 公式追踪使用 `CONFIG_EXT4_COUNTER_TRACE`。当前统一 normal 构建采用 GCC 14，并已通过构建绑定、源码审计，以及普通/bigalloc/无 journal/lazy init 的 VM smoke。对应构建的校验方式见 [源码审计](audit/SOURCE-AUDIT.md)，历史验证的证据状态见 [实现状态](IMPLEMENTATION-STATUS.md)。
- 运行时全 filesystem `R-UG/R-LINK` 扫描已实现：`counter_snapshot` 会遍历 inode bitmap、
  `ext4_iget()` 读取每个 inode、统计目录 inode 并扫描目录数据块中的 dirent 引用，再与
  `i_nlink` 比较；扫描期间出现任何 writer（epoch/version/active 变化）时结果记为
  `namespace_valid=false` 而不是误报。
- namespace 已按实际 dirent 引用转换与 nlink 子步骤插桩：`NAMESPACE.DIRENT.ADD/DELETE`、
  `NAMESPACE.NLINK.INC/DEC/CLEAR/ROLLBACK`，tracepoint 新增 `nlink_before/after`、
  `dir_nlink_before/after` 和 `step` 字段，schema 2 另有 applied `ref_delta`、`dir_ino`、
  独立 `result`、inode generation/birth/death。
- 特殊路径各自带独立 expected，并由 `scripts/run-special.sh` 做定向命中验证：
  bigalloc rereserve、online resize 和 fast-commit replay。
  历史命中范围见 [FORMULA-SOURCE-COVERAGE.md](FORMULA-SOURCE-COVERAGE.md) 和 [FORMULA-RELATIONS-IMPLEMENTATION.md](FORMULA-RELATIONS-IMPLEMENTATION.md)；已清理的原始运行不作为本轮重验。

## 源码、快照和专项记录

`build.sh` 在独立输出目录使用保存的配置，优先选择 GCC 14。原始源码位于 `kernel/`，开发副本位于 `dev/`。
补丁与逐文件 SHA256 位于 `patches/`，重放验证在独立临时目录完成。
快照读取 `/sys/fs/ext4/<device>/counter_snapshot`，序号端点读取 `counter_trace_state`；
原始事件通过 tracefs 的 `ext4_counter:ext4_counter_event` 导出。

special 路径的原始运行目录已清理；其 fast-commit replay `R-UG` 持久差异由 [finding report](FAST-COMMIT-DIR-COUNT-FINDING.md) 单独保留。报告覆盖该路径的触发条件、离线 checker 和 `e2fsck` 结论。

长跑和覆盖对照的原始 campaign 目录已清理，历史指标不再作为当前证据；后续重新运行时，
应以新的 campaign 报告记录每种反馈配置的覆盖收益与完整性。

## 2026-10-18 收尾修复

runner 现在在采集停止后设置 `finalizing=true`，checkpoint、最后一次取回、逐点分析、正常关机和
离线校验不再被 collection wall watchdog 截断；`scp` 单次超时为 300 秒，仍保留 4 次有限重试。
逐点汇总也先使用当前段的生命周期时间线，只有当前段存在未匹配原始事件时才读取下一段的 carry 时间线，
减少长跑中的重复扫描。42 个 campaign 回归测试通过；需要重新运行长跑才能取得新的完整 12 小时证据。

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

本轮验证通过：版本 6.12.1，共 92,438 个条目、86,618 个普通文件；全部文件内容、权限和 symlink 目标与官方 tarball 一致，无额外文件，官方树无 `checker.c`、`.config` 或 `bzImage`。

准备完成后，从仓库根目录重新校验源码：

```sh
(cd experiments/ext4-counter-trace/kernel && sha256sum --quiet -c ../source/pristine-files.sha256)
```

`environment.json` 记录本地 GCC 14/15、QEMU、e2fsck、tar、Python 的路径与版本。当前没有 `/dev/kvm`，本轮 VM 使用 TCG。配置已保存到 `config/counter-trace.config`，构建输出与官方树分离；当前验证采用 GCC 14。
