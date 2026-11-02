# campaign-dzq91z1h 候选复核

这是对 `experiments/ext4-counter-trace/results/campaign-dzq91z1h/` 的只读派生分析；原始 campaign 结果没有修改。运行：

```text
python3 analyze_candidates.py
```

## 结论

这次 campaign 没有给出已确认的 ext4 持久化 bug。`campaign-report.json` 的 184 条 candidate 由两种记录组成：

- 128 条 `workload_exit`：全部是 guest exit `90`，即 fault 之后 workload/launcher 清理阶段的聚合错误退出；
- 56 条 `lifecycle`：共 119 个 `operation_exit_mismatch`，全部来自 experimental hook；所有生命周期报告的 `persistent_mismatch` 都是 0。

两类 candidate 只覆盖 149 个 segment，其中 35 个 segment 同时产生两类记录。没有一个 `stable_snapshot` 被报告为 `candidate`。337 个 hit 的 before snapshot 全部为 `clear/stable`；after snapshot 有 39 个 `clear/stable`，其余 298 个是 `unverified/concurrent_or_unhealthy`，不是对象级 mismatch。全局还有 128 个 `unclosed_phase`，都落在 `cleanup-*` phase，与 128 条 workload-exit candidate 一致。

原始 fault event 的 337/337 条都能在 raw trace 中找到并与 manifest 的 operation、errno、PID、随机种子/抽样字段一致，337/337 的 errno-choice 校验通过。327 条有 verified native syscall envelope，10 条是 hook-only attribution。这个结果说明 fault hit 是真实的，但 hit 本身不等同于 bug。

## admitted hook

27 次 admitted hit 分布为：

| hook | hit | verified native envelope | candidate |
| --- | ---: | ---: | ---: |
| `mballoc_mark` | 19 | 19 | 7（均为 workload exit 90） |
| `inode_evict_final_dirty` | 5 | 4；1 hook-only | 0 |
| `unlink_inode_dirty` | 3 | 3 | 0 |

因此 campaign 的候选集合里没有 admitted hook 的 lifecycle mismatch，也没有 admitted hook 的 stable object mismatch。7 个 `mballoc_mark` candidate 的共同形态是 fault 命中后清理动作返回 `EIO`，例如 `rmdir tree: Input/output error`，而不是离线 inode/block 关系错误。

`inode_evict_final_dirty` 的 5 次 hit 都是 `guest_exit=0`，after snapshot 只是 unverified；segment 492 的命中没有 native syscall envelope，不能作为 native syscall 因果证据。其余 4 次分别在 segment 243、244、328、480，attribution verified，但没有 candidate。

## 退出码和 lifecycle candidate 的含义

runner 明确把 exit 90 作为 launcher 的 aggregate worker-failure status，并在 fault 已命中时把非零退出保存为 candidate，以便后续复核。源码位置：

- `scripts/campaign_runner.py:1065-1068`：exit 90 是允许抓取和审计的 launcher 状态；
- `scripts/campaign_runner.py:1170-1176`：fault 命中且 `rc != 0` 时设置 `fault_expected_workload_exit`；
- `scripts/campaign.py:142-146`：该标记生成 `workload_exit` candidate；
- `tests/syz-launcher.c:16`：launcher 的 `die()` 使用 exit 90，清理失败会直接走该路径。

因此这 128 条 candidate 不能读成“内核返回 90”或“文件系统已经损坏”。128 个 workload 尾部中有 53 个 `rmdir tree: Directory not empty`、25 个 `rmdir tree: Input/output error`、49 个 `unlink tree: Input/output error`、1 个 `rmdir tree: Structure needs cleaning`，与注入的元数据错误及清理顺序一致。

56 条 lifecycle candidate 的报告细节只有 `operation_exit_mismatch`，没有 `persistent_mismatch`。119 个 episode 全部是 `R-LINK`：26 个在同一 trace 后续恢复，93 个到 segment 末尾仍未恢复，另有 29 个没有 native exit evidence。它们的 onset 只出现在 `NAMESPACE.REF.DELETE/REPLACE/REMOVE_DOT/REMOVE_DOTDOT/NLINK.DEC/ADD`，不是 inode bitmap 或离线 inode lifecycle。比如 segment 0 的 gap 在 `NAMESPACE.REF.DELETE` 后由 `unlinkat` 返回 `-ENOSPC`，之后有些 gap 恢复，有些到该 segment 末尾仍存在；这属于失败操作后的 namespace reference 轨迹未闭合，不是稳定快照确认的 inode/link 关系损坏。lifecycle 分类代码只有在有效 stable evidence 仍有 gap 时才产生 `persistent_mismatch`，见 `scripts/trace_lifecycle.py:692-703`。

## 离线镜像

三个最终镜像的 `disk.json` 都是 `status=verified`，三个 `fsck.txt` 都是 `e2fsck` exit 0，且 inode lifecycle/link checker 没有 mismatch。`bigalloc` 的 `matches_final_stable_snapshot=false` 不能作为 bug 证据：它是唯一被 fault-tainted 的 filesystem，最终 checkpoint 本身 `valid=false`、`reason=concurrent_or_unhealthy`，但 checkpoint 中的 free clusters、free inodes、dirs 与离线 disk checker 数值仍完全相同。

campaign 的全局分类也没有持久化证据：`persistent_mismatch=0`；另有 `12` 个 `concurrent_or_ambiguous`、`64` 个 `unsupported_or_unverified`，这些是证据边界，不能升级成 bug。

## 派生文件

- `candidate-ledger.csv`：184 条 candidate，包含 source、segment、hook、admission、exit、快照和 lifecycle 分类；
- `real-hits.csv`：337 条真实 hit 的 trace/event/syscall attribution；
- `admitted-hits.csv`：27 条 admitted hook 的子集；
- `snapshot-timeline.csv`：337 个 hit 的 before/after snapshot 状态；
- `lifecycle-candidate-episodes.csv`：119 个 operation-exit episode 的 stream、onset、恢复和 native exit evidence；
- `summary.json`：可供后续 campaign 对比的计数摘要。

当前最稳妥的定性是：**真实 fault hit + 预期的错误传播/清理失败候选，尚无 ext4 持久化 bug 证据**。若要把某个候选升级为 bug，需要在同一 hook 上补充有效 after stable snapshot 或离线 mismatch，并排除 launcher cleanup exit 和 experimental hook 语义。
