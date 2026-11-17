# 实现状态

更新时间：2026-11-17（Asia/Shanghai）。

## 实现入口

- [README.md](README.md)：构建、运行和使用方法。
- [第一条链](FIRST-CHAIN.md)、[第二条链](SECOND-CHAIN.md)：逐点事件、操作出口、稳定态及长时采集。
- [公式覆盖](FORMULA-SOURCE-COVERAGE.md)、[新增关系](FORMULA-RELATIONS-IMPLEMENTATION.md)：公式实现、锁边界和适用范围。
- [KCOV 反馈](KCOV-FEEDBACK.md)：逐调用准入、PC/目标 edge/语义反馈与调度。
- [源码审计](audit/SOURCE-AUDIT.md)：源码与构建身份；重新核验使用 `scripts/source_audit.py --require-build`。

模板父本分池、每模板 seed 刷新、目标 edge 口径及按设备验证的 reset/carry 边界已接入 runner。
manager 级多 VM corpus 合并、跨重启自动恢复和通用跨线程 parent 归因仍未完成。
离线 BIGALLOC 特性使用 superblock offset 100 的 ro-compat `0x0200`；META_BG 明确拒绝。

## 证据状态

2026-11-17 已清空 `results/` 的全部历史结果（415,334 个文件，约 29.3 GiB）。
当前没有保留的原始 campaign、逐段 trace 或实验磁盘；旧汇报、实验过程和重复清理记录已删除。
`research/` 中保留的机器可读摘要不能替代原始数据，也不代表本轮重新验证。
历史通过字段及计数仅描述旧运行，后续验收需重跑对应工具和测试。

最终漏洞结论维护在 [漏洞总表](../../BUG-INVENTORY.md)，
fast-commit 目录计数的唯一分析见 [finding report](FAST-COMMIT-DIR-COUNT-FINDING.md)。
独立 source-fault 子项目的 [unlink 最终证据](../ext4-source-fault-study/UNLINK-FINAL.md) 保留完整。

## 验证边界

未证明稳定窗口、设备身份、原生 syscall 归属或 trace 完整性的事件应为 `unverified`。
错误注入命中和在线 mismatch 不自动等于已持久化的内核缺陷。
长时间反馈收益仍需相同负载、镜像策略、调用集合及多个 seed 的受控比较。
