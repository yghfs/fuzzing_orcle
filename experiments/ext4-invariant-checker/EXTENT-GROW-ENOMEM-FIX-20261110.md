# ext4 extent 增深 ENOMEM 块泄漏：修复与对照验证

后续审阅更新：Jan Kara 要求一并处理新块接入树前的其他错误出口，
已完成 [v2 修复与三阶段对照验证](EXTENT-GROW-ERRORS-V2-20261110.md)。
下文保留首版 ENOMEM 修复和当时的验证记录。

2026-11-10：已修复 `ext4_ext_grow_indepth()` 在 `sb_getblk_gfp()` 返回
NULL 后遗漏释放 `newblock` 的错误出口。原版 Linux 6.12.1 的对照实验稳定
留下 7 个孤块及 7 个 inode 的 `i_blocks` 错误；加入修复后，相同错误出口
仍然命中，但正常卸载后的 `e2fsck -fn` 返回 0。

## 修复

独立补丁：
[`0002-ext4-free-unreachable-extent-block-on-enomem.patch`](patches/0002-ext4-free-unreachable-extent-block-on-enomem.patch)。
它面向不带 checker 的上游源码，可用 `patch -p1` 应用；没有包含验证用的注入代码。
工作区 `kernel/linux-6.12.1/fs/ext4/extents.c` 也已应用相同的回收逻辑。

```c
if (unlikely(!bh)) {
        /* The new block has not been linked into the extent tree yet. */
        ext4_free_blocks(handle, inode, NULL, newblock, 1,
                         EXT4_FREE_BLOCKS_METADATA);
        return -ENOMEM;
}
```

这个出口尚未发布新根指针，因此新元数据块可以安全释放。
`ext4_free_blocks()` 同时处理 bitmap、空闲空间和 quota/inode 字节记账；
不能只手动减 `i_blocks`。`EXT4_FREE_BLOCKS_METADATA` 保留元数据块在
transaction 提交前不能被过早复用的处理。

在工作区带 checker 的主源码中，移除了该内部出口的过早结构检查。
此时 caller 可能还持有待回滚的新数据块，应由原有的
`ext4_map_blocks()` 错误出口在 `ext4_ext_map_blocks()` 完成数据块清理后
检查。上游独立补丁不涉及这些项目专用 hook。

本次补丁和测试针对 `sb_getblk_gfp() == NULL`；没有把后续 JBD2/dirty
错误出口视为已修复或已验证。

## 验证方法

新增实际文件系统回归负载：
[`ext4-extent-grow-enomem-regression.c`](../../tests/ext4-extent-grow-enomem-regression.c)。
构建、QEMU 运行与离线检查入口：
[`verify-extent-grow-enomem.py`](scripts/verify-extent-grow-enomem.py)。

```bash
python3 experiments/ext4-invariant-checker/scripts/verify-extent-grow-enomem.py \
  --out work/extent-grow-enomem-fix-validation --jobs 8
```

运行需要现有的原版 6.12.1 源码、实验构建配置、Debian 基础镜像、SSH key、
gcc-14、QEMU 和 e2fsprogs，路径均取自本项目现有环境。输出目录应为新目录。

验证从原版 Linux 6.12.1 源码创建隔离副本，禁用 checker、项目 fault
injection、KASAN 和 KCOV。在隔离副本中添加同一份测试 hook：只对 `/dev/vdb`
的 extent 增深操作，在成功分配元数据块后、获取 buffer 之前返回 NULL。
hook 不调用 `sb_getblk_gfp()`，也不会先取得 buffer 后丢失它的引用。

这是精确 caller 错误路径的语义注入，**不代表 helper 内部真实发生内存耗尽**。
注入代码只存在于验证用源码和内核中。两版内核使用相同配置、相同 hook、
相同初始 ext4 镜像和相同负载；生产修复是固定版唯一额外的功能改动。
还单独验证了导出补丁产生的源码与实际构建固定版的源码逐字节一致。

每个文件先同步写入 4 个带洞的 4 KiB extent，确认 `st_blocks=32`，然后
用 `fallocate()` 触发增深。保留文件，正常卸载、正常关闭 VM，再进行只读
`e2fsck -fn`。负载还读取全部种子数据，检查错误处理没有损坏原内容。

## 实际结果

| 检查项 | 未修复版 | 修复版 |
|---|---|---|
| 7 个独立文件的精确失败出口 | 全部返回 `ENOMEM` | 全部返回 `ENOMEM` |
| 失败后的 inode 12–18 `st_blocks` | 全部 40，预期 32 | 全部 32 |
| 原有文件大小和种子内容 | 保持正确 | 保持正确 |
| 离线 `i_blocks` 错误 | 7 个 | 0 个 |
| bitmap 不可达占用块 | 7 个：33825–33831 | 0 个 |
| 正常卸载后的 `e2fsck -fn` | 返回 4 | 返回 0 |
| 另一个文件失败后立即重试 | 未执行 | 32 → 48，成功 |
| 重试成功后截断到 0 | 未执行 | `st_blocks=0` |
| 无注入的正常增深 | 未执行 | 成功，`st_blocks=48` |

固定版总计命中 8 次失败出口：7 个保留文件，加 1 个重试测试文件。
失败仍正常返回给用户，回滚后文件保持 4 个数据块；成功增深后的 48 sectors
对应 5 个数据块加 1 个 extent 元数据块。

未修复版的 `debugfs icheck` 对块 33825–33831 全部报告 `<block not found>`，
`testb` 对这 7 个块全部报告 `marked in use`。固定版的多次失败能够回收并
重新使用同一块 33825，最终完整离线扫描无 bitmap 差异。

两版 Linux 6.12.1 内核构建成功。最初的未署名补丁通过
`patch --dry-run --fuzz=0` 及跳过签署检查的 checkpatch；随后按下文生成正式
邮件补丁，并通过包含签署检查的完整格式检查。

## 邮件补丁整理

2026-11-10 根据用户指定的既有补丁，使用 `Alan <3164729207@qq.com>`
作为作者及 `Signed-off-by` 身份。在独立主线 worktree 中创建真实 Git 提交，
再用 `git format-patch` 导出，加入作者、日期、收件人、diffstat、blob index、
`Fixes:`、stable 抄送及 `base-commit:`。功能改动保持一致。

- 主线基线：`d135ab453a7400a34930bdda1ef4bdc8acfac80e`
- 修复提交：`4c2a9240fdff9b59f0c1b908327e9a2d57c1a987`
- 分支：`fix/ext4-extent-grow-enomem`
- worktree：`work/linux-mainline-extent-grow-submit`
- `Fixes: 8a83ac54940d ("ext4: call bdev_getblk() from sb_getblk_gfp()")`：
  该提交将 `sb_getblk_gfp()` 改为直接调用 `bdev_getblk()`，移除了旧的
  `__getblk_gfp()` 路径隐含添加的 `__GFP_NOFAIL`，使本次遗漏回滚的
  NULL 返回分支可达。

正式版通过 `checkpatch.pl --no-tree`（0 errors、0 warnings）、主线基线上的
`git apply --check`、原版 6.12.1 上的 `patch --dry-run --fuzz=0`、
`git mailinfo` 解析，以及 `git send-email --dry-run`。
临时移除相同测试 hook、应用正式补丁、恢复 hook 后，源码与此前构建固定版
内核所用的 `fixed-extents.c` 逐字节一致，因此此次格式整理未重复构建或运行 VM。
本报告不声称已在主线基线上完成内核构建或运行验证。

新检查记录与补丁 SHA-256 位于
[`work/extent-grow-submit-format-20261110`](../../work/extent-grow-submit-format-20261110/)。
下方原始证据目录中的补丁副本和 SHA-256 仍对应当时未整理邮件格式的版本，
保留以便追溯，不能用它们校验现在的邮件补丁。
首版整理完成时尚未发送，随后已由用户正式投递。
审阅后的新版提交请使用 [v2 提交指南](EXTENT-GROW-V2-SUBMISSION.md)。

## 原始证据

本次输出目录：[`work/extent-grow-enomem-fix-20261110-1443`](../../work/extent-grow-enomem-fix-20261110-1443/)。

- [机器可读汇总](../../work/extent-grow-enomem-fix-20261110-1443/summary.json)
- [未修复版运行记录](../../work/extent-grow-enomem-fix-20261110-1443/baseline/runtime.txt)、[fsck](../../work/extent-grow-enomem-fix-20261110-1443/baseline/fsck.txt)、[debugfs](../../work/extent-grow-enomem-fix-20261110-1443/baseline/debugfs.txt)
- [修复版运行记录](../../work/extent-grow-enomem-fix-20261110-1443/fixed/runtime.txt)、[fsck](../../work/extent-grow-enomem-fix-20261110-1443/fixed/fsck.txt)
- [生产补丁与构建源码一致性](../../work/extent-grow-enomem-fix-20261110-1443/patch-equivalence.txt)
- [测试 hook](../../work/extent-grow-enomem-fix-20261110-1443/test-hook.diff)、[内核配置](../../work/extent-grow-enomem-fix-20261110-1443/kernel.config)、[SHA-256](../../work/extent-grow-enomem-fix-20261110-1443/sha256.txt)

目录还保留了两版内核、源码快照、构建日志、VM 串口记录、QEMU 命令、负载
源码及二进制、初始镜像和两版正常卸载后的镜像。原有复现材料未修改。
