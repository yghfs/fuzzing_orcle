# Implementation status

Updated: 2026-11-11 (Asia/Shanghai). Current evidence inventory: [documentation audit](../../DOCUMENTATION-AUDIT-20261111.md).

The formula families added after the original counter chain are implemented in
the development tree and have a passing bounded relation run. The authoritative
scope, evidence and limits are documented in
[`FORMULA-RELATIONS-IMPLEMENTATION.md`](FORMULA-RELATIONS-IMPLEMENTATION.md).
The original bounded relation run directory has been pruned. Historical directed
validation does not prove that every fault-injection exit has executed.

The Linux 6.12.1 counter-trace implementation, source audit, schema-3 write
audit, and no-trace build have historical passing records. The source/build
binding was checked again on 2026-11-11 and passed. The disk-layout regression
checks were rerun for the bigalloc correction; the older lifecycle, syzkaller,
KCOV and campaign test results are historical, not fresh runs of this audit.

Historical chain, smoke, and superseded campaign run directories have been pruned.
`results/campaign-lhze43mu`, `results/campaign-k3gxmeex`, its post-report,
and `results/campaign-fmxs5w3e` are absent on the audited host. Their remaining
written summaries must not be presented as available raw evidence. Current
retained coverage runs are indexed in [the coverage audit](research/2026-11-11-target-coverage/README.md).
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
accepted by campaign aggregation as a reset boundary. This does not prove that
every per-segment lifecycle carry handles the new epoch correctly: the retained
twelve-hour PC-only/full-feedback runs have 7/3 analysis failures immediately
after reset, with epoch/sequence and device-identity issues. Their KCOV samples
are complete but overall coverage/lifecycle validation is not. A tainted
filesystem is reset before reuse; continuity across that reset still needs validation.

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
