# 单 worker 错误注入证据格式

每次确定性错误注入使用独立目录，保存计划、arm 读回、内核命中、真实系统调用、公式事件、稳定快照和持久化检查。原始文件完成后不可覆盖。当前有两种格式：schema 1 服务于已验证的 `mballoc-mark` 旧 fixture；schema 2 服务于公式阶段 fixture。

schema 2 还保存 `association-index.json`，把 fault hit 的原始 trace 行连接到同一 PID、同一 `call_index` 的 `CT_SYSCALL` marker、`raw_syscalls` entry/exit、故障后的公式事件、对象 inode/块字段和稳定快照。`fault_evidence_v2.py` 会从 `trace.txt` 和 JSON 原件重建这些连接，索引不能替代原始事件。

## Schema 1：mballoc-mark

schema 1 的验证器是 `scripts/fault_evidence.py`，只用于 bitmap 修改前失败的 `mballoc_mark=-EIO`。它要求注入前后计数和目标文件状态不变，不能用于 bitmap、descriptor、inode 或 namespace 已部分修改后才失败的出口。

计划示例：

```json
{
  "schema": 1,
  "fault_id": "mballoc-mark-eio",
  "operation": "mballoc_mark",
  "errno": -5,
  "count": 1,
  "program": "fixture-w0",
  "call_index": 3,
  "syscall_nr": 285,
  "rollback_fields": ["free_clusters", "free_inodes", "dirs", "target_file"],
  "expected_return": {"result": -1, "errno": 5}
}
```

fixture 的 arm 状态、kernel hit、同 PID syscall trace 窗口、稳定快照、重挂载和 offline checker/e2fsck 都由 `fault_evidence.py` 校验。旧的示例运行目录已清理；新的通过 VM 证据应由 runner 在当前 `results/` 下生成并由该验证器校验。

## Schema 2：公式阶段

schema 2 的计划由 `scripts/fault_plan.py` 生成，验证器是 `scripts/fault_evidence_v2.py`。计划至少定义：

- `fault_id`、精确 `operation`、fixture `action`、目标 syscall 编号和返回策略；
- 要检查的 `formulas`、目标 `oracle_points`、预期 oracle/formula 状态；
- 若故障本身预期留下坏态，显式列出 `expected_namespace_states`。

### 长跑随机注入隔离证据

`campaign_runner.py` 的随机注入默认使用 `isolate_after_hit=1`。单 worker 的 runtime
在注入 syscall 返回后写入 `fault-stop-after-call`，内容包含 `program`、`call_index`
和实际 hook `operation`，然后结束该 workload。runner 只允许该 program 的已验证调用前缀
缺失，其他调用、worker 和 trace 丢失仍然使 segment 失败。命中后 guest 对受影响设备执行
正常 `sync`、卸载和只读 `e2fsck -fn`，结果分别保存在：

- `fault-stop-after-call`
- `fault-fsck.txt`
- `fault-offline-status.txt`，包含 `unmount_exit`、`fsck_exit` 和 `remount_exit`；只有 `unmount_exit=0`
  时才把 fsck 结果作为离线持久化证据

manifest 的 `fault_injection.isolated_after_call`、`fault_injection.offline_check` 和
`fault_stop_after_call` 将这些文件绑定到单次命中。`campaign-report.json` 的每个 hit 也
记录隔离状态和离线检查结果。`--no-fault-isolation` 仅用于探索连续 workload；这种模式
不会把收尾镜像自动解释为某次注入的独立持久化证据。

隔离 fault 的离线 remount 可能启动新的 counter epoch；counter event enable 是全局 trace
开关，因此该段剩余 checkpoint 的最终 endpoint 不再与 fault 前 raw trace 强行做同 epoch
比较。此时 endpoint 完整性由 fault 的 stop marker、syscall envelope、`fault-offline-status.txt`
和离线 fsck 原件分别约束，tainted filesystem 的持久化结论仍需人工复核。

离线检查结束后 guest 会恢复全局 `ext4_counter_event`，并写入
`FAULT_COUNTER_EVENT_REENABLED` marker，保证后续 segment 仍能收集 ext4 counter domain。
若失败 segment 没有生成 `analysis/carry-out.txt.gz`，后续 segment 会停止复用 carry 和
stable anchor，并在 manifest 中记录 `missing_carry_trace`；这是局部证据缺口，不应因
`ENOENT` 直接中止整个 campaign。

每个证据目录应包含 `fault-plan.json`、`arm.json`、`hits.json`、`workload-result.json`、`formula-events.json`、`association-index.json`、`trace.txt`、CPU trace loss 统计、`stable-before.json`、`stable-after.json`、`stable-oracle.json`、`remount.json`、`persistence.json`，以及离线修复前后的 fsck 和磁盘 checker 原始输出。schema 2 从同一 PID 原始 trace 检查 arm 后的 syscall 窗口、native syscall entry/exit、kernel hit 和调用结果；workload JSON 的布尔确认不能替代 raw trace。

### 稳定状态和持久化

离线检查分为两个镜像：guest 产生的 `test.raw` 是不可变主证据，runner 在检查前复制出 `recovery.raw`。`e2fsck -fy` 和最终只读 fsck/check-disk 只作用于副本。`image-integrity.json` 保存两份镜像的相对路径和 SHA-256：主镜像的 `sha256_after_guest`/`sha256_after_checks` 必须相同，副本的 `sha256_before_repair` 必须等于主镜像 guest hash，并记录 `sha256_after_repair` 和 `sha256_after_checks`。

`stable-before`、`stable-after` 与 `remount` 要有 `observation_stable=true`、`trace_loss=0`、`workers=1`。对于在真实 ext4/JBD2 metadata helper 边界注入 `-EIO` 的计划，journal abort 会使故障后的内存公式和 namespace 快照失效；计划必须显式声明 `error_semantics.journal_abort=true`，并把这种状态标为运行时中间态。它不能作为 raw 持久化损坏的证据。

2026-10-26 的真实 helper-boundary `-EIO` 复核中，11 项 hook 均命中一次且 syscall return 与 raw syscall trace 相符。它们的稳定快照双读一致，但 journal abort 后内存公式或 namespace validity 可为 false；对应的卸载前 `e2fsck -fn` 和独立磁盘检查均 clean。逐项结论和 syscall errno 在根目录 `BUG-INVENTORY.md` 的 2026-10-26 条目中。之前用旧高层 fault-only 结果标记的持久化违例已不适用于这 11 个新运行。

这 11 个原始目录中的 `remount-target.json` 有些由修复前 collector 写成了缺少外层对象的成员片段；原始材料保持不变，v2 validator 不用该可选文件判定持久化，新的 collector 已输出完整 JSON 对象。需要复核采集链路时，应以新 run 的完整 artifact 和 `validation.json` 为准。

分类说明：`injected_journal_abort_no_persistent_damage` 表示注入和错误传播已验证、raw 镜像未发现持久化差异；它允许明确计划的稳定双读快照为 `valid=false`，不把这种状态称为文件系统稳定健康。`persistence.matches_remount` 比较的是 raw 检查前的 remount counters 与 raw descriptor totals，不是修复后的挂载状态。验证器从原始 trace、双读 snapshot、fsck 文本、run metadata 和 disk JSON 独立复核，不只信 collector 的布尔标志。

分类：

| 分类 | 含义 |
| --- | --- |
| `not_triggered` | 计划/arm/kernel hit 缺失或不匹配 |
| `wrong_return` | 真实调用结果不符合计划 |
| `unstable` | 稳定观察无效、有 trace loss 或 worker 范围错误 |
| `oracle_unverified` | 计划要求的公式事件没有有效观测或状态不匹配 |
| `association_invalid` | hit、目标 syscall、公式事件、对象或稳定快照的原始关联索引无法重建 |
| `persistence_mismatch` | 持久化证据缺失、修复后检查失败或未满足计划恢复判据 |
| `injected_journal_abort_no_persistent_damage` | 真实 helper 错误已命中，syscall 返回符合源码传播，故障后内存观察可失效，但卸载前 raw fsck 与独立磁盘检查干净 |
| `injected_persistent_postcondition_violation` | 真实 helper 错误已命中，且卸载前 raw fsck 或独立磁盘检查已经观察到持久化后置条件违例 |
| `finding_reproduced_recovered` | 预期公式不一致被真实观测，且计划定义的离线恢复检查通过 |
| `passed` | 注入执行后公式状态符合预期，且稳定/持久化验收通过 |
| `matched_control` | 同一 fixture 在 fault arm 关闭时 syscall、稳定快照和离线镜像均通过 |
| `blkdebug_io_error_propagated` | 独立 QEMU blkdebug 读写控制确认 I/O errno 传播且镜像未改变 |

`finding_reproduced_recovered` 只表示 fixture 复现了错误并通过其明示的恢复检查。对本次 inode eviction 结果，恢复指 offline `e2fsck -fy` 后的 `e2fsck -fn` 与 disk checker；不指内核自动恢复。

当前可运行的 runner：

```sh
bash experiments/ext4-counter-trace/scripts/run-fault-single-worker.sh
bash experiments/ext4-counter-trace/scripts/run-fault-formula-single-worker.sh
bash experiments/ext4-counter-trace/scripts/run-fault-formula-control.sh --fault-id PLAN
```

`run-fault-formula-control.sh` 使用相同的计划、fixture、kernel、rootfs 和 raw 镜像参数，但在 guest 内跳过 fault arm。它由 `scripts/validate-fault-control.py` 分类为 `matched_control`，不会进入 schema 2 的 fault-hit 结论。

块设备层另有独立的 QEMU `blkdebug` 流程：

```sh
python3 experiments/ext4-counter-trace/scripts/run-blkdebug-io-validation.py
```

该流程先做正常 `qemu-io` 读取，再用 blkdebug 注入 I/O errno，要求错误向调用者传播且镜像 SHA-256 不变。`blkdebug_io_error_propagated` 只说明 QEMU/storage 层错误传播成功，不能证明 ext4 helper 或计数公式有问题。

schema 2 的 11 个 2026-10-26 定点运行目录已清理；逐项结论唯一保留在根目录
`BUG-INVENTORY.md` 的对应表格中。`fault_evidence_v2.py` 仍根据计划的错误语义区分
运行时中间态与 raw 持久化差异，新的运行应使用该验证器生成完整证据目录。
