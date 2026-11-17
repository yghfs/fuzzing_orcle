# ext4 源码错误路径与持久化验证

本子项目研究 ext4 错误出口的更新顺序和回滚行为。
漏洞总表见 [BUG-INVENTORY.md](../../BUG-INVENTORY.md)。

## 最终报告

| 材料 | 内容 |
| --- | --- |
| [__ext4_unlink() 最终报告](UNLINK-FINAL.md) | 已认可、已修复；最终 v2 补丁及主线修复前后、无故障对照证据 |
| [块层 EIO 验证](PHYSICAL-IO-CORE-20261102.md) | group extend、rmdir、rename 等实际 guest 块请求错误及持久化边界 |
| [源码错误路径审计](FINAL-AUDIT-20261031.md) | group add 的分配失败、mkdir 的精确错误出口及未确认为漏洞的恢复行为 |
| [独立三候选审计](INDEPENDENT-THREE-CANDIDATES-20261116.md) | linkat、punch-hole、collapse-range 的独立故障组和控制组 |

unlink 的旧 helper 返回注入、审阅争议和投递过程由最终报告取代。
实际 inode-table READ EIO 发生在 journal write access 之前；它与 journal 已 abort
后的合成 helper 错误不同。最终主线对照保存于最终报告链接的两个原始目录中。

## 源码注入复现

实验以独立 Linux 6.12.1 为基础，注入补丁为
[0001-ext4-source-derived-fault-injection.patch](patches/0001-ext4-source-derived-fault-injection.patch)。
`CONFIG_EXT4_SOURCE_FAULT_INJECTION` 默认关闭；启用后通过
`/sys/kernel/debug/ext4-source-fault/control` 设置一次性 `<operation> <errno>`。
helper 调用边界的合法错误返回替代验证调用者错误分支，不能单独证明物理 I/O 或内存耗尽。

```sh
bash experiments/ext4-source-fault-study/scripts/prepare-source.sh
bash experiments/ext4-source-fault-study/scripts/build-local-kernel.sh
bash experiments/ext4-source-fault-study/scripts/build-initramfs.sh
KERNEL=experiments/ext4-source-fault-study/build/linux-source-fault/arch/x86/boot/bzImage \
  bash experiments/ext4-source-fault-study/scripts/run-qemu-cases.sh
```

依赖 GCC、QEMU、BusyBox 和 e2fsprogs。可以用 `CASE_FILTER` 选择用例，
通过 `KERNEL`、`INITRD`、`OUT_DIR` 和 `RUN_DIR` 指定产物路径。
运行器采集 `e2fsck -fn` 时允许命令失败，所以 runner 的退出码或正常卸载不等于 fsck 通过；
持久化判断必须读取对应离线日志。实际块层 EIO 由 QEMU `blkdebug` 提供，
不表示宿主物理介质故障；journal abort 后未恢复的镜像不能直接证明已提交的不一致。

独立三候选可只读核验：

```sh
python3 experiments/ext4-source-fault-study/scripts/verify-independent-ext4-audit.py
```
