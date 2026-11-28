# 实现状态

更新时间：2026-11-24（Asia/Shanghai）。

## 实现入口

- [README.md](README.md)：构建、运行和使用方法。
- [第一条链](FIRST-CHAIN.md)、[第二条链](SECOND-CHAIN.md)：逐点事件、操作出口、稳定态及长时采集。
- [公式覆盖](FORMULA-SOURCE-COVERAGE.md)、[新增关系](FORMULA-RELATIONS-IMPLEMENTATION.md)：公式实现、锁边界和适用范围。
- [KCOV 反馈](KCOV-FEEDBACK.md)：逐调用准入、PC/目标 edge/语义反馈与调度。
- [源码审计](audit/SOURCE-AUDIT.md)：源码与各构建产物的校验方式。

普通回归和默认 manager 使用由 `build.sh --normal` 生成的统一内核。一个
`CONFIG_EXT4_COUNTER_TRACE` 启用核心公式和附加模型。稳定态比较
由 `counter_trace.c` 和 `counter_relations.c` 共享实现，`counter_models.c` 补齐
附加模型、事务遥测和调试统计。`build.sh` 的无参数构建输出也位于
`build/linux-counter-trace-normal/`，所有 counter-trace 测试入口使用这套构建。

模板父本分池、每模板 seed 刷新和目标 edge 反馈已接入 runner；分段分析保存有界 carry trace。
manager 级多 VM corpus 合并、跨重启自动恢复和通用跨线程 parent 归因仍未完成。
离线 BIGALLOC 特性使用 superblock offset 100 的 ro-compat `0x0200`；META_BG 明确拒绝。

## 证据状态

2026-11-24 的统一构建通过补丁重放、源码清单和产物绑定校验。当天的短测结论如下。

- 四种文件系统的 smoke 通过序号、丢失计数、稳定快照和关机后磁盘检查。
- regular、bigalloc、inline 的单线程和并发 chain 通过。
- regular、bigalloc、inline、legacy 的关系检查通过，涵盖 orphan、xattr 和 eviction。
- 单 worker campaign 核验 32 条 syscall，双 worker campaign 核验 64 条。两轮 KCOV 完整，关机后磁盘检查通过。
- 自然 rename 前后及重新挂载后的内核快照均有效，离线磁盘检查通过。
- bigalloc、resize 和 fast-commit replay 通过证据校验。replay 重现既有目录计数问题，`e2fsck` 按预期返回 4。详见 [fast-commit 分析](FAST-COMMIT-DIR-COUNT-FINDING.md)。
- 18 个 Python 测试脚本通过。47 个 shell 脚本和 22 个 C 测试源文件通过语法检查；分析器和磁盘 checker 的反例验证也通过。

高水位采样在同组仍有 inode 描述符发布时记为
`unverified`。本次修复了初始化调用遗漏、orphan 文件容量误限、紧凑目录项扫描和
FC replay 中访问尚未初始化的 group-info 问题。重复 replay 的 descriptor 比较补齐真实读数，
避免以缺失值判断一致性。普通 TCG 回归使用 `qemu64` CPU。

2026-11-17 清理了当时的历史结果（415,334 个文件，约 29.3 GiB）。
2026-11-24 又清理了 `results/` 下的全部 30 个运行目录（2,061 个文件，约 9.8 GiB），
包括上述短测的报告、trace 和实验镜像。后续需要原始证据时，须重新运行对应测试。
早期主线恢复归档另存于本机 `baselines/`，其中保存了当时的源码与 `work/` 内容；当前回归不读取该归档。

2026-11-28 把 `ext4_rmdir` / `__ext4_unlink` / `ext4_rename` 的出错出口模型
（`ext4_checker_{rmdir,unlink,rename}_error`，判据"失败的 `ext4_mark_inode_dirty()` 不算
已持久化"，只读内存与内存中的目录块）并入本实验的 0001 基座补丁，与
[故障注入实验](../ext4-counter-trace-fault-injection/) 共用同一份判定逻辑；本实验仍只做
checker，补丁中不含任何错误注入。0001 重导出后 `--write-inventory`、`build.sh --normal`
（内核 #10）与 `--bind-build` 全部通过，`run-smoke.sh` 报 `counter_trace_smoke=passed`、
`failures=[]`（四种文件系统、766,756 个事件）。

同日清理 `results/`：删除了旧内核（md5 `172295c6…`，228 段长跑）的 `campaign-7soi4ouf`
（约 904 MiB）。本文件上方与 `README.md` 已声明长跑原始目录不作为当前证据，需要时按
[SECOND-CHAIN.md](SECOND-CHAIN.md) 重跑。当前 `results/` 只保留内核 #10 的
`smoke-2wcDOtL9/`。

最终漏洞结论维护在 [漏洞总表](../../BUG-INVENTORY.md)，
fast-commit 目录计数的唯一分析见 [finding report](FAST-COMMIT-DIR-COUNT-FINDING.md)。

## 验证边界

未证明稳定窗口、设备身份、原生 syscall 归属或 trace 完整性的事件应为 `unverified`。
运行时 mismatch 还需稳定快照或可靠持久化后的离线检查支持，才能确认内核缺陷。
