# 逐点差值、操作出口、稳定态链

本轮实现范围是普通活动期 `R-BDELTA/R-IDELTA/R-UDELTA/R-LINK` 的完整有限观测链。
独立官方 Linux 6.12.1、原始参照树和主线项目保持独立。

## 事件与独立来源

schema 2 保留原字段并追加 `dir_ino/ref_delta/result/generation/is_dir`。
`actual/expected/delta` 只在 `comparison_valid=1` 时构成现场比较。
raw global 事件仍保留上游发布量，分析器将其标为
`publication_delta_not_global_sample`，从 `update_delta` 重建派生总量。
raw namespace 的 helper 结果在 `result`；只有 step 16 的 `ref_delta` 代表已应用引用转换。

- cluster expected 来自 group lock 内统计的真实 bitmap bit 转换，单位 cluster。
- inode expected 来自实际 bitmap claim/release，单位 inode。
- directory expected 来自目录 inode claim/release，单位 directory inode。
- R-LINK expected 来自实际 dirent 转换和稳定目录扫描，包括 `.`/`..`。
- descriptor 和 global 各自维护独立累计流；block LOCAL 在锁内携带 descriptor 更新量，
  稍后的 PUBLISH 是 dirty/helper 结果，不重复累加。目录 descriptor 也有独立发布点。
- 所有派生流从有效稳定快照建立基线，检查下一次稳定快照是否逐流吻合。
  global 轨迹是已记录发布量的累计结果，不能当作每一时刻真实 percpu 聚合采样。

namespace 在低层 insert/delete/setent/dotdot 写入时记录；rename 覆盖目标替换、exchange、
whiteout、跨目录 `..`，以及 mkdir/rmdir 和被替换目录的 live dot 引用移除。
link rollback、失败 clear、tmpfile VFS nlink 转换与 inode birth/death 也独立记录。
转换发生后 dirty/helper 返回失败不会删除证据。
生成号、birth seq 与 death 防止 inode 号码重用串联为同一生命周期。

稳定 namespace 扫描解析 inline 目录两个独立 rec_len 区域及合成 `.`/`..`，
不再因为 inline 而漏掉子 inode 的引用。
zero-link inode 仍比较是否确实没有引用；zero-link directory 不贡献 live dot 引用。
扫描首尾 active/version 不满足稳定条件则明确 unverified。

## 分析与分类

`trace_lifecycle.py` 为共用分析实现；`analyze-lifecycle.py` 为命令行入口。
新版 `analyze-trace.py` 在 schema 2 smoke 上同时调用生命周期分析。
每个 timeline 行保存原事件、值语义、单位映射、锚点、三条派生计数流、group 差值、
inode 生命周期以及操作/syscall 关联。

差值按 `actual-derived_expected` 方向计算。报告保存首次出现、peak、恢复 seq/point、
时间长度、涉及 operation/syscall、出口和稳定证据；区间净零也不会抹掉中途差值。
每个 group 单独跟踪，两个 group 的相反差值不能靠全局求和抵消。

| 分类 | 证据条件 |
| --- | --- |
| `transient_mismatch` | 完整、锚定且无外来 writer 的轨迹中出现并恢复，未跨越有效外层出口 |
| `operation_exit_mismatch` | 有关发布已应完成的外层 ext4 END 或原生 syscall 返回仍有差值 |
| `persistent_mismatch` | 有效独立稳定快照确实确认差值；运行时间长或 EOF 单独不构成证明 |
| `concurrent_or_ambiguous` | 同窗口其它线程 writer、未知后台原因或无法证明的跨线程归属 |
| `unsupported_or_unverified` | 丢事件、无稳定基线、未建模域、覆盖断点、格式/规模等限制 |

内层 `BLOCK.MARK_CONTEXT` 等 helper 返回后的 global 待发布标为
`pending_parent_publication`，不把父操作尚未完成的步骤误报为出口异常。
外层 END 的差值由完整 scope 树和独立流确定；同范围存在外来 writer 则降级为不确定。
后续稳定样本可独立确认异常，但不会追溯地把并发轨迹变成精确因果证明。

运行身份由 `chain-manifest.json` 保存 run/boot/dev/epoch/seq。
校验端点 active=0、连续 seq、重复 seq、scope parent/BEGIN/END、每个 CPU loss stats、
稳定 JSON 与 trace 采样的一致性，任何覆盖或重建断点保留报告并使相应验收失败。

## 准确 syscall 返回

有限 C 负载每次 syscall 用 trace_marker 包围，同时开启
`raw_syscalls:sys_enter/sys_exit`。分析器要求同 PID/NR、唯一且位于两个 marker 之间的原生
enter/exit 配对，采用内核 raw return 求 errno，再核对用户态 ret/errno。
marker write 的 sys_enter/sys_exit 排除在 envelope 外，不会把 marker 的 write 当成被测 write。

ext4 END 仍是 helper 返回。只有位于原生 syscall pair 内且同线程的事件才精确关联。
writeback 等后台事件自行拥有 scope，`no_cross_thread_cause` 明示尚未传播跨线程因果。

## 运行与产物

```sh
bash experiments/ext4-counter-trace/scripts/build.sh
python3 experiments/ext4-counter-trace/scripts/export-patch.py
bash experiments/ext4-counter-trace/scripts/run-chain.sh
python3 experiments/ext4-counter-trace/tests/test-lifecycle.py
```

runner 保存本次 kernel/config/patch/analyzer/workload 内容，使用独立 rootfs overlay，
启动 QEMU TCG，2 CPU、2 GiB。普通、64 KiB cluster bigalloc、inline_data 三种镜像各运行
single/concurrent 两个有限区间。捕获普通写入、hole punch、truncate、fsync、link、tmpfile、
rename overwrite/exchange/whiteout、自然 ENOSPC/EEXIST/ENOENT。

single 在 open-unlinked、tmpfile-unlinked、inline-live、inline-converted 时再读稳定快照。
inline 专用镜像用 inode flags 实测 live 目录仍 inline、扩张后已转换；避免仅因 feature
开启就声称覆盖。最后稳定内存扫描在 sync/卸载前，确认关机后 fsck 和独立磁盘检查。
磁盘总量与最后稳定内存总量再次比较。

输出位于 `<run>/ct-results/lifecycle/`：

- `timeline.jsonl`：完整逐点原始/派生值及有效性。
- `episodes.json`：差值出现到恢复的生命周期。
- `report.json`：点位命中/状态/差值分布、操作/syscall 出口、稳定重建、loss 与磁盘证据。
- `report.md`：可读结果与限制。

已有 trace 可分析，原始证据只读，输出另指定目录：

```sh
python3 experiments/ext4-counter-trace/scripts/analyze-lifecycle.py <run>/ct-results --output /独立输出目录
```

schema 1 缺少实际 namespace 转换和锁内 descriptor 顺序，不能升级为完整本链证据。

## 有意保留的适用范围

本链验证完整普通活动期窗口；resize/FC replay/corruption 是独立调整域，普通轨迹遇到
这些变化时需要新基线并明确 unverified。special 定向回归仍检查这些路径自己的 expected。
当前 special 的 per-stage loss/端点证明尚弱于 chain，不用于声称完整普通生命周期。

通用跨线程因果传播仍未实现。并发/后台轨迹按记录顺序保留，不能声称 observer seq
代表跨 CPU 实际修改的先后。目录 dir_nlink sentinel、EA/保留 inode、快照规模上限等
保留明确覆盖限制；`BLOCK_UNINIT` 磁盘布局仍未建模。长时轮转、持续收集与资源治理现已
由 [SECOND-CHAIN.md](SECOND-CHAIN.md) 中的受控运行链接入，本篇描述有限活动期链。
