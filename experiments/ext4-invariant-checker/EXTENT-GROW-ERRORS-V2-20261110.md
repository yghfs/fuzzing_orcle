# ext4 extent 增深失败回滚：v2 修复与验证

根据 [Jan Kara 的审阅](https://www.spinics.net/lists/linux-ext4/msg114422.html)，
修复扩展到 `ext4_ext_grow_indepth()` 中新块接入 extent tree 之前的全部
三个失败出口。正式邮件补丁：
[0002-ext4-free-unreachable-extent-block-on-error-v2.patch](patches/0002-ext4-free-unreachable-extent-block-on-error-v2.patch)。
v2 是完整修复补丁，应在未应用首版的基线上应用；本文统一维护最终验证证据。

## 代码与范围

三个失败出口统一跳到 `out_free`，由
`ext4_free_blocks(handle, inode, NULL, newblock, 1, EXT4_FREE_BLOCKS_METADATA)`
撤销尚未发布的新块分配与 inode 记账，再走 `out` 释放 buffer 引用。

| 失败位置 | 处理 |
|---|---|
| `ext4_new_meta_blocks()` | 没有成功分配新块，沿用直接返回 |
| `sb_getblk_gfp()` | 设定 `-ENOMEM`，回收新块；`brelse(NULL)` 安全 |
| `ext4_journal_get_create_access()` | 先解锁 buffer，再回收新块和释放 buffer 引用 |
| `ext4_handle_dirty_metadata()` | 此时 buffer 已解锁，回收新块和释放 buffer 引用 |
| `ext4_mark_inode_dirty()` | 新块已被 `ext4_idx_store_pblock()` 接入树，只释放 buffer 引用 |

没有手动调整 `i_blocks`，也没有把已接入树的新块送入 `out_free`。
使用的 metadata 释放标志与本文件已有的分裂失败回滚保持一致。
项目带 checker 的 `kernel/linux-6.12.1/fs/ext4/extents.c` 也已同步这三个出口，
保留原有 fault hook 和错误计数；该带 checker 的源码本轮没有重新构建运行。
上游补丁只改 `fs/ext4/extents.c`，不包含 checker 或测试 hook。

## 实际验证

两版 Linux 6.12.1 内核均已构建成功，使用相同配置和相同的三阶段测试 hook。
6 个独立 VM 对照用例均正常卸载、正常关机；修复版在每个阶段还验证了重试、
正常增深与截断。阶段开关只对测试盘 `/dev/vdb` 生效。

| caller 边界注入 | 未修复版：7 个失败文件 | v2：7 个失败文件 | v2 重试等检查 |
|---|---|---|---|
| buffer 获取返回 NULL / `ENOMEM` | 每个 `st_blocks=40`，7 个孤块，fsck=4 | 每个 `st_blocks=32`，无孤块，fsck=0 | 通过 |
| create access 返回 `-EIO` | 每个 `st_blocks=40`，7 个孤块，fsck=4 | 每个 `st_blocks=32`，无孤块，fsck=0 | 通过 |
| dirty metadata 返回 `-EIO` | 每个 `st_blocks=40`，7 个孤块，fsck=4 | 每个 `st_blocks=32`，无孤块，fsck=0 | 通过 |

全部保留文件的原始大小和种子数据均正确。修复版每个阶段总共命中 8 次失败：
7 个保留文件，加 1 个失败后立即重试的文件。重试成功后 `st_blocks=48`，
截断后为 0；未注入错误的成功增深为 48。六次 VM 运行的串口日志未见
`BUG:`、`WARNING: CPU:` 或 `Kernel panic`。

这些 hook 在调用方边界替换 helper 返回值：后两处在实际调用 metadata
helper 之前注入 EIO，因此不包含 helper 内部副作用，不触发真实 journal
abort。结果证明三个 caller 回滚出口的行为，不证明真实日志故障之后仍能
提交事务；已 aborted 的 journal 仍受其固有错误处理约束。

复现命令（输出目录必须不存在）：

```bash
cd /home/ikun/fuzzing-orcle
python3 experiments/ext4-invariant-checker/scripts/verify-extent-grow-errors-v2.py \
  --patch experiments/ext4-invariant-checker/patches/0002-ext4-free-unreachable-extent-block-on-error-v2.patch \
  --out work/extent-grow-errors-v2-rerun \
  --jobs 8
```

验证仍依赖本项目已有的原版 6.12.1 源码、构建配置、基础镜像、SSH key、
gcc-14、QEMU 和 e2fsprogs。负载支持指定预期 errno，旧 ENOMEM 用法保持兼容。

## 邮件与源码一致性

- 主线基线：`d135ab453a7400a34930bdda1ef4bdc8acfac80e`。
- v2 提交：`be113245431d01f9e9fd61a8424de1368bc22769`（最终复查后的提交，代码不变）。
- 分支：`fix/ext4-extent-grow-errors-v2`。
- worktree：`work/linux-mainline-extent-grow-submit`。
- 作者和签署：`Alan <3164729207@qq.com>`。
- 标题：`[PATCH v2] ext4: free the new extent block on failure to grow the tree`。

邮件由真实提交通过 `git format-patch -v2 -1 --base=...` 导出。
按用户要求精简长版 v2 变更说明与首版链接，并保持首版的
`Fixes: 8a83ac54940d ("ext4: call bdev_getblk() from sb_getblk_gfp()")`。
该标签对应本次报告的 buffer 分配失败路径变为可达的提交；另外两个 journal
错误出口更早存在，此次依审阅意见一并补齐回滚。Fixes 标签不表示所有
一并修复的分支都由该提交首次引入。没有添加未经授予的 Reviewed-by 标签。
2026-11-11 最终复查：邮件正文明确补充上述历史区别，恢复测试段落与标签之间的空行，并从真实提交重新导出邮件，
消除手工修改邮件造成的提交正文不一致和尾随空格。

最终版本完整 checkpatch 为 0 errors、0 warnings；主线
`git apply --check`、6.12.1 无 fuzz 应用检查均通过，发送演练正常。
重新应用最终邮件补丁所得的完整生产源码与构建前保存的源码逐字节一致；
加入相同 hook 后与实际构建修复版内核的完整测试源码逐字节一致。
主线本轮只验证应用与补丁格式，没有声称完成主线构建或运行。

## 原始证据

输出目录：[work/extent-grow-errors-v2-20261110](../../work/extent-grow-errors-v2-20261110/)。

- [汇总 JSON](../../work/extent-grow-errors-v2-20261110/summary.json)
- [buffer 获取：未修复版 fsck](../../work/extent-grow-errors-v2-20261110/getblk/baseline/fsck.txt)、[修复版 fsck](../../work/extent-grow-errors-v2-20261110/getblk/fixed/fsck.txt)
- [create access：未修复版 fsck](../../work/extent-grow-errors-v2-20261110/create_access/baseline/fsck.txt)、[修复版 fsck](../../work/extent-grow-errors-v2-20261110/create_access/fixed/fsck.txt)
- [dirty metadata：未修复版 fsck](../../work/extent-grow-errors-v2-20261110/dirty_metadata/baseline/fsck.txt)、[修复版 fsck](../../work/extent-grow-errors-v2-20261110/dirty_metadata/fixed/fsck.txt)
- [最终邮件补丁与构建源码一致性](../../work/extent-grow-errors-v2-20261110/submission-checks/final-patch-equivalence.txt)
- [checkpatch](../../work/extent-grow-errors-v2-20261110/submission-checks/checkpatch.txt)、[正式投递演练](../../work/extent-grow-errors-v2-20261110/submission-checks/send-email-dry-run.txt)、[自测收件人演练](../../work/extent-grow-errors-v2-20261110/submission-checks/selftest-dry-run.txt)
- [最终邮件补丁 SHA-256](../../work/extent-grow-errors-v2-20261110/submission-checks/sha256.txt)、[内核及负载 SHA-256](../../work/extent-grow-errors-v2-20261110/sha256.txt)

邮件精简后的最新检查与 SHA-256 见
[mail-simplified](../../work/extent-grow-errors-v2-20261110/submission-checks/mail-simplified/)。
原有 submission-checks 中的日志和哈希保留为精简之前的记录。

目录中的 `production.diff` 是构建时使用的同一代码 diff，最终邮件版另存为
`0002-ext4-free-unreachable-extent-block-on-error-v2.patch`。
两版完整源码、测试 hook、构建日志、内核、负载及 6 个用例的运行记录、
debugfs、串口日志和磁盘镜像均保留。

恢复首版 Fixes 标签后的最终邮件检查与 SHA-256 见
[fixes-restored](../../work/extent-grow-errors-v2-20261110/submission-checks/fixes-restored/)。
上述目录及更早检查与哈希保留用于追溯。
2026-11-11 最终复查的最新记录与 SHA-256 见
[final-review-20261111](../../work/extent-grow-errors-v2-20261110/submission-checks/final-review-20261111/)。

按用户要求，随后从邮件与提交说明中删除了末句 “These tests exercise the caller cleanup paths without aborting the journal.”。代码保持一致；详细验证范围仍见本报告的实际验证部分。最新邮件检查及哈希见
[sentence-removed-20261111](../../work/extent-grow-errors-v2-20261110/submission-checks/sentence-removed-20261111/)。
