# `inode_evict_final_dirty` 定性结论

## 结论

截至当前证据，**不能把它定性为 ext4 的真实内核 bug**。更准确的分类是：

> fault injection 触发了 ext4 已定义的 final-inode-dirty 错误分支；该分支有意跳过
> `ext4_free_inode()`，因此在镜像中留下 allocated、zero-link、`dtime=0` 的 inode，
> 由 fsck 在后续修复。实验确认了一个真实、可重复的错误后置状态，但没有证明正常
> ext4 执行路径违反了自己的错误处理合同。

这不是说镜像中的状态是虚假的。六个 inode 的残留、fsck exit 4、后续 e2fsck 修复都是真实的。
结论是：**状态是真实的，触发条件是故障注入，状态本身符合源码明确选择的保守失败策略。**

建议登记为：

```text
bug_status: not_confirmed
finding: injected persistent postcondition / expected error recovery degradation
formula: R-IEVICT is valid only when final inode dirty succeeds
```

## 1. 源码合同

当前保留的 upstream `kernel/linux-mainline` 仍然有以下控制流：

```c
ext4_orphan_del(handle, inode);
EXT4_I(inode)->i_dtime = ktime_get_real_seconds();

if (ext4_mark_inode_dirty(handle, inode))
        ext4_clear_inode(inode);
else
        ext4_free_inode(handle, inode);
```

源码注释直接说明：如果 final `mark_dirty` 失败，不能释放 inode。原因是：

- `ext4_mark_inode_dirty()` 失败时，删除时间和 inode 内容未必已经安全写入 journal；
- `ext4_free_inode()` 会清除 inode bitmap、增加 free-inode 计数，并减少目录计数；
- 在 journal/metadata 已经失败的情况下继续释放，可能让 inode bitmap 状态与 inode 内容的
  提交顺序失去安全保证；
- 因此代码选择清除 in-core inode，保留 on-disk inode allocation，交给 fsck 处理。

`ext4_free_inode()` 自己的注释还要求先清除 in-core inode，再清除 bitmap，避免 inode number
复用时出现两个 in-core inode alias。它不是一个可以在任意错误点无条件调用的补偿函数。

源码位置：

- [upstream inode.c](</home/ikun/fuzzing-orcle/kernel/linux-mainline/fs/ext4/inode.c:372>)
- [faultable inode.c](<dev/linux-6.12.1/fs/ext4/inode.c:296>)
- [ext4_free_inode()](<dev/linux-6.12.1/fs/ext4/ialloc.c:225>)

## 2. fault injector 到底模拟了什么

这次注入并不是在 `ext4_evict_inode()` 之后随机改 bitmap，也不是凭空伪造一个用户态 close
返回错误。它位于：

1. `ext4_fill_raw_inode()` 已经把 in-memory inode 内容填入 inode buffer；
2. 即将调用 JBD2 `dirty_metadata`；
3. fault injector 返回一个错误；
4. 走真实 ext4 error handler；
5. `ext4_evict_inode()` 按正常错误分支执行 `ext4_clear_inode()`，跳过 `ext4_free_inode()`。

因此，**错误分支本身是真实代码，后置状态也是真实代码产生的**。但注入仍然不是完整的自然
硬件/JBD2故障：它在真正进入 JBD2 helper 前返回，不能证明该错误在这个精确 inode 状态下自然
可达，也不能完整复现真实 journal abort、buffer head、I/O ordering 等副作用。

## 3. 六次随机命中的 errno 质量

项目的 canonical `fault-errno-catalog.json` 对 `inode_evict_final_dirty` 只列出：

- `EFSCORRUPTED/EUCLEAN`；
- `EROFS`；
- `ENOSPC`。

六次残留实例中：

| segment | errno | canonical native set | 评价 |
|---:|---:|---|---|
| 53 | -30 EROFS | 是 | 可作为 native error branch 证据，但本次没有证明自然触发前提已存在 |
| 139 | -22 EINVAL | 否 | campaign 使用的旧宽集合；不能用于 native bug 结论 |
| 211 | -28 ENOSPC | 是 | native helper errno，仍需自然 JBD2 条件复现 |
| 256 | -28 ENOSPC | 是 | 同上 |
| 281 | -28 ENOSPC | 是 | 同上 |
| 347 | -117 EUCLEAN | 是 | native helper errno，仍需自然 JBD2 条件复现 |

所以六次中有 **5 次使用 canonical native errno，1 次使用非 canonical 的 EINVAL**。
这提高了错误分支覆盖的可信度，但没有把 fault-only 结果自动升级成漏洞。

## 4. 现有定点实验反而支持“预期失败语义”

根目录 `BUG-INVENTORY.md` 的 2026-10-26 定点复核表保留了该实验合同的唯一摘要：

- `expected_oracle_state = mismatch_reproduced`；
- `R-IEVICT = mismatch_reproduced`；
- pre-recovery fsck 的 inode bitmap issue 是预期问题；
- e2fsck 修复后 fsck、disk checker、remount counter 全部通过；
- 最终 `classification = finding_reproduced_recovered`。

它证明 fixture 能稳定地产生这个后置状态，并且修复路径符合预期。它没有把这个状态登记成
upstream bug。

同一历史复核的持久化摘要也记录了：

- 注入前 fsck clean；
- 注入后 fsck exit 4，`Deleted inode ... has zero dtime`、`Inode bitmap differences`；
- e2fsck 修复后 fsck exit 0；
- free inode 恢复增加 1。

这说明当前测试框架本来就把它定义为“可修复的注入后故障状态”。

## 5. 为什么 `R-IEVICT` 不能直接等同于 bug

当前 `R-IEVICT` 的期望值是无条件的：target inode bitmap 必须从 1 变成 0。
但在 upstream 的错误分支中，`ext4_free_inode()` 只在 final dirty 成功时调用。
因此这个 invariant 应该写成条件合同：

```text
if final_dirty_result == 0:
    inode bitmap must be released
else:
    inode bitmap release is intentionally not required;
    filesystem error/recovery state must be checked separately
```

当前 trace 中 `R-IDELTA-EVICT` 已经被标记为 `comparison_valid=0`、
`slot_not_released_or_other_inode_updates`；它不能被当作第二个独立 mismatch。
同样，`R-IG` 的 bitmap 和 free-inode counters 彼此一致，只是共同保留了一个 zero-link inode，
不构成计数公式错误。

## 6. 何时可以升级为真实 bug

需要至少满足以下一项：

1. **无 fault injection**，仅通过真实 I/O/JBD2/journal abort 触发 final dirty error，仍产生同样残留，且该行为超出 ext4 设计的 fsck-recovery边界；
2. 在 `EROFS`、`ENOSPC` 或 `EUCLEAN` 的真实 helper 前提成立时，源码执行路径仍然可以安全释放 bitmap，但当前代码错误地跳过了它；
3. 残留 inode 不能被正常 fsck 修复，导致 live namespace、inode reuse、数据块归属或后续挂载出现不可恢复的数据损坏；
4. 对比一个 matched control：同一 kernel、同一 workload、同一镜像，只去掉 fault injection 后，仍出现同样目标 inode 残留。

目前这些条件都没有被 campaign 证明。现有 causality audit 的 control 是
`control_observed_but_not_matched`，并明确存在 workload/kernel/probe 差异，不能作为 bug 对照。

## 最终判断

**当前不应把它报成 ext4 kernel bug。** 应把它保留为一个高价值的错误恢复语义测试结果：
fault 注入命中后，ext4 按源码设计拒绝释放 inode，产生可由 fsck 修复的持久化残留。
真正需要修正的首先是 oracle：把 `R-IEVICT` 改成依赖 final dirty 返回值的条件断言，
并把 native errno 和 synthetic errno 分开统计。
