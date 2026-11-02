# Implementation status

The formula families added after the original counter chain are implemented in
the development tree and have a passing bounded relation run. The authoritative
scope, evidence and limits are documented in
[`FORMULA-RELATIONS-IMPLEMENTATION.md`](FORMULA-RELATIONS-IMPLEMENTATION.md).
The original bounded relation run directory has been pruned. Historical directed
validation does not prove that every fault-injection exit has executed.

The Linux 6.12.1 counter-trace implementation, source audit, schema-3 write
audit, and no-trace build are complete. Directed lifecycle, disk-layout,
syzkaller, KCOV, and campaign-governance tests pass.

Historical chain, smoke, and superseded campaign run directories have been pruned. The
retained long runs are `results/campaign-lhze43mu` (single-worker fault-injection run)
and `results/campaign-k3gxmeex` with its post-report (two-worker concurrent run).
The fast-commit claim-retry comparison was also historical material and its result directory
has been pruned; only the source-level conclusion is retained in `BUG-INVENTORY.md`.

Feedback comparison outputs remain only where they are independent retained summaries;
removed campaign directories are historical material and are not referenced as active
evidence. Manager-level feedback remains outside this single-VM campaign.

## Deterministic fault injection

The formula-focused runner retains 12 deterministic plans: one allocation error
control and 11 hook cases with observed postcondition violations. Their historical result
directories have been pruned; formula, stable-state, persistence and recovery limits are
recorded once in the root `BUG-INVENTORY.md`.
An observed injected violation is not evidence of natural syscall reachability.
The independent `fast_commit_inode_claim` replay control was a passing historical check;
its result directory is not retained.

The errno catalog separates kernel capability from campaign admission. Every
hook marked `runtime_random` has a source-level errno contract and can be
selected explicitly, but the default long-run pool is the smaller
`random_admission` set (`mballoc_mark`, `unlink_inode_dirty`, and
`inode_evict_final_dirty`) with reviewed fixture/return-chain evidence. Other
compiled hooks are recorded as `experimental` when explicitly selected and do
not silently expand a long run. The fast-commit replay hook remains
deterministic-only. Historical campaign evidence preserves the operation and
errno policy used at the time of each run. The root kernel's legacy checker,
its additional count models, existing tests and evidence are explicitly
retained.

After a random hit the target filesystem is marked tainted. The next segment
that would use it runs an in-guest `mke2fs`/mount reset before collecting a new
workload; the reset evidence is retained and its counter epoch boundary is
explicitly accepted by campaign aggregation. A hit therefore never causes the
next workload to inherit an unreviewed filesystem state.

The ext4 per-mount rule selects one operation, errno and count, with an optional
target PID. `program` and `call_index` are captured trace labels; they do not
filter inside the kernel. Formula-stage evidence must therefore prove the hit
falls inside the unique target syscall of the single-worker fixture. Schema 1
is specific to the pre-mutation `mballoc_mark` error; schema 2 records expected
formula states and fault-specific persistence/recovery outcomes.

The formula evidence path now derives its hook contract from the errno catalog
and hook/formula matrix, including every declared mutation stage and caller
return contract. The runner keeps `test.raw` immutable, copies it to
`recovery.raw` before offline checks, and archives boundary hashes in
`image-integrity.json`. The collector emits `association-index.json`; the v2
validator rebuilds its hit-to-syscall-to-formula-to-object-to-snapshot links
from raw trace lines. `run-fault-formula-control.sh` provides a matched
no-injection control, while `run-blkdebug-io-validation.py` independently
checks QEMU block-layer errno propagation. These controls are classified
separately from ext4 helper-fault findings.
