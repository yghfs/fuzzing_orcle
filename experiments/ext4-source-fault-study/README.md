# ext4 source-derived error-path study

This study starts from the verified Linux 6.12.1 source archive recorded in
[`../ext4-counter-trace/source/linux-6.12.1.tar.xz.sha256`](../ext4-counter-trace/source/linux-6.12.1.tar.xz.sha256)
and adds a standalone ext4 fault injector. The injection points were selected
by following the error returns and mutation order in the upstream ext4/JBD2
call paths. They do not use this repository's existing fault-injection
framework or its results to choose injection points.

The source patch is
[`0001-ext4-source-derived-fault-injection.patch`](patches/0001-ext4-source-derived-fault-injection.patch).
It builds behind `CONFIG_EXT4_SOURCE_FAULT_INJECTION`, is disabled by default,
and exposes one global, one-shot control at
`/sys/kernel/debug/ext4-source-fault/control`. Write `<operation> <errno>` to
arm it; read the file after a workload to see whether it was hit. The
operation-specific errno allow-list is the union of errors returned by the
helper at that source boundary. In Linux 6.12.1, `EFSCORRUPTED` aliases
`EUCLEAN`, so the log prints the canonical name `EUCLEAN` for either spelling.

The tests do not issue `EXT4_IOC_SHUTDOWN` and the patch adds no
`ext4_force_shutdown()` call. Each guest performs an ordinary `sync`, unmounts
the test filesystem, and powers off normally before the host runs `e2fsck -fn`.
JBD2 can abort a transaction when a real metadata-access or dirtying helper
returns an error; that is the normal error behavior exercised by these call
sites, separate from ext4 forced shutdown.

## Block-layer EIO validation (2026-11-02)

The later validation uses QEMU `blkdebug` to make guest block requests return
`EIO`, followed by normal unmount and offline checks. The full run matrix,
commands, logs, and limits are in
[`PHYSICAL-IO-CORE-20261102.md`](PHYSICAL-IO-CORE-20261102.md).

| Operation | Result |
|---|---|
| Group extend | A bitmap READ failed with `EIO`; the ioctl returned `EIO`, and the offline bitmap and free-block counts disagree with the published capacity. Exact target path reproduced. |
| `rmdir` | An inode-table READ failed with `EIO`; the syscall returned `EIO`, and the child/parent link state remained inconsistent after unmount. Exact target path reproduced. |
| `unlink` | A parent inode-table READ failed with `EIO`; the syscall returned `EIO`, and the removed name with the old target `nlink` persisted. A fixed-kernel control preserves the name and passes `e2fsck`. |
| Ordinary `rename` | A source inode-table READ failed with `EIO`; both names and the old `nlink` persisted. Exact target path reproduced. |
| `mkdir` | A real EIO persisted a dangling dirent at an earlier `add_dirent_to_buf()` error exit. The later parent-`nlink` dirty failure branch was not reached by the exact block-I/O attempt; the source-semantic test below covers that caller branch. |
| Group add | A real write EIO caused JBD2 abort. It does not test the finding's required `ext4_mb_add_groupinfo() == -ENOMEM` branch. |

“Block-layer EIO” here means a QEMU-injected guest-visible failed block request,
not a fault observed on a host physical drive. The source-semantic matrix below
remains useful for caller branches whose precise error condition was not
reproduced by block I/O, especially group-add allocation failure and the exact
mkdir parent-dirty exit.

For the unlink EIO case, a separate trace run records the actual
`jbd2_journal_abort()` entry probe, `jbd2_journal_stop()` handle fields and
return value, and transaction start/end commit tracepoints. In that run the
abort probe has no hit, unlink's TID 7 remains `T_RUNNING` at stop with no
handle/journal abort state, then TID 7 commits during `sync`; the offline
`nlink` mismatch still persists. See the JBD2 follow-up section and raw logs in
[`PHYSICAL-IO-CORE-20261102.md`](PHYSICAL-IO-CORE-20261102.md), reproduced by
[`run-unlink-jbd2-trace.py`](scripts/run-unlink-jbd2-trace.py).
This is distinct from the earlier `unlink_parent_global_abort` source-semantic
case, which deliberately set a writeback-error sequence and did enter JBD2's
abort path; its `Aborting journal` message does not describe the physical
inode-table READ EIO run.

## Reproduce

From the repository root, prepare the verified Linux 6.12.1 source, apply this
study's patch, then build the kernel and guest initramfs:

```sh
experiments/ext4-source-fault-study/scripts/prepare-source.sh
bash experiments/ext4-source-fault-study/scripts/build-local-kernel.sh
bash experiments/ext4-source-fault-study/scripts/build-initramfs.sh
KERNEL=experiments/ext4-source-fault-study/build/linux-source-fault/arch/x86/boot/bzImage \
  experiments/ext4-source-fault-study/scripts/run-qemu-cases.sh
```

The scripts need GCC 14, QEMU x86-64, BusyBox, `mke2fs`, `e2fsck`, and
`dumpe2fs`. Override `CC`, `HOSTCC`, `KERNEL`, `INITRD`, `OUT_DIR`, `RUN_DIR`,
or `CASE_FILTER` when needed. `CASE_FILTER=unlink-parent-global-abort` and
`CASE_FILTER=unlink-parent-inode-read` run the two deeper semantic checks
described below. The group-add case also writes a raw superblock/descriptor decode
to `group-add-ENOMEM.raw-descriptor.txt`; the decoder is deliberately limited
to the 4 KiB, 64-byte-descriptor image created by this test.

The runner deliberately collects `dumpe2fs` and `e2fsck -fn` output with
`|| true`, so its shell exit status does not report the filesystem-check result.
Likewise, `UNMOUNT_RESULT rc=0` only means that the guest unmount completed;
it does not mean that the on-disk metadata is consistent. The diagnostics in
the corresponding `.e2fsck.log` file are the authoritative check result. To
observe the checker status directly, run `e2fsck -fn IMAGE` without the wrapper.

## Source-semantic injection matrix (2026-11-01)

For the primary matrix, the injector substitutes an errno from the called
helper's real return set at its caller-visible result boundary. This selects
the unmodified caller error branch; it does not claim that the helper ran its
internal cleanup or that a physical I/O or allocation failure occurred. A
finding requires both the caller error result and a persistent inconsistency
after normal unmount. The outcomes below distinguish those findings from
cases where a journal abort or a deliberate recovery policy prevents that
conclusion.

| Candidate | Source boundary and injected error | Observed postcondition failure after unmount |
|---|---|---|
| Group extend publishes capacity before bitmap update | Substituted `-EIO` at the `ext4_mb_load_buddy()` call-result boundary, after the in-memory superblock capacity update; `-EIO` is a real helper return | `EXT4_IOC_GROUP_EXTEND` returns `EIO`; normal-unmount `e2fsck` reports blocks 16,384–19,999 still allocated while the superblock count is 20,000. See [`group-extend-EIO.log`](results/qemu-source-replay-20261101/group-extend-EIO.log), [`.dumpe2fs.log`](results/qemu-source-replay-20261101/group-extend-EIO.dumpe2fs.log), and [`.e2fsck.log`](results/qemu-source-replay-20261101/group-extend-EIO.e2fsck.log). **Confirmed conditional caller-path defect.** |
| Group add persists resize metadata before the new group is published | Substituted `-ENOMEM` at the `ext4_mb_add_groupinfo()` call-result boundary, after descriptor and resize-inode metadata were dirtied; `-ENOMEM` is its allocation-failure return | `EXT4_IOC_GROUP_ADD` returns `ENOMEM`; superblock remains at one group, while `e2fsck` reports resize inode 7 references blocks beyond the old block count and has an `i_blocks` mismatch. Raw group-1 descriptor is corroborating context; because that group is not published, its descriptor alone is not the invariant violation. See [`group-add-ENOMEM.raw-descriptor.txt`](results/qemu-source-replay-20261101/group-add-ENOMEM.raw-descriptor.txt) and [`.e2fsck.log`](results/qemu-source-replay-20261101/group-add-ENOMEM.e2fsck.log). **Confirmed conditional caller-path defect.** |
| `mkdir` leaves a child entry and parent increment | Substituted `-EIO` at the parent inode `ext4_mark_inode_dirty()` result boundary, after child entry insertion and parent `nlink` increment | `mkdir` returns `EIO`; offline `e2fsck` reports parent inode 13 count 3 instead of 2 and the `child` entry points to deleted/unused inode 14. See [`mkdir-parent-EIO.log`](results/qemu-source-replay-20261101/mkdir-parent-EIO.log) and [`.e2fsck.log`](results/qemu-source-replay-20261101/mkdir-parent-EIO.e2fsck.log). **Confirmed conditional caller-path defect.** |
| `rmdir` leaves parent link count high | Substituted `-ENOMEM` at the child inode `ext4_mark_inode_dirty()` result boundary, after removing the directory entry and clearing the child's link count | `rmdir` returns `ENOMEM`; offline `e2fsck` reports parent inode 13 count 3 instead of 2. See [`rmdir-child-ENOMEM.log`](results/qemu-source-replay-20261101/rmdir-child-ENOMEM.log) and [`.e2fsck.log`](results/qemu-source-replay-20261101/rmdir-child-ENOMEM.e2fsck.log). **Confirmed conditional caller-path defect.** |
| `unlink` removes a name before decrementing target `nlink` | Substituted `-EIO` at the parent inode dirty result boundary; separate reserve and inode-table read-result cases exercise deeper helper cleanup paths | `unlink` returns `EIO`; after normal unmount, inode 13 has `Links: 1`, the root has no `file` entry, and `e2fsck` reports it unattached. The deeper reserve and read-result cases reproduce this disk state too. See [`unlink-parent-EIO.log`](results/qemu-source-replay-20261101/unlink-parent-EIO.log), [`.e2fsck.log`](results/qemu-source-replay-20261101/unlink-parent-EIO.e2fsck.log), [`unlink-parent-reserve-EIO.log`](results/qemu-source-replay-20261101/unlink-parent-reserve-EIO.log), and [`unlink-parent-inode-read-EIO.log`](results/qemu-source-replay-20261101/unlink-parent-inode-read-EIO.log). **Confirmed conditional caller-path defect.** |
| `rename` publishes a new name before deleting the old name | Substituted `-EIO` at the source inode dirty result boundary, after destination entry insertion | `rename` returns `EIO`; offline `e2fsck` reports inode 13 has one link but two directory references. See [`rename-source-EIO.log`](results/qemu-source-replay-20261101/rename-source-EIO.log) and [`.e2fsck.log`](results/qemu-source-replay-20261101/rename-source-EIO.e2fsck.log). **Confirmed conditional caller-path defect.** |
| Inode eviction leaves inode allocated | Substituted `-ENOMEM` at final inode dirty result boundary | `e2fsck` reports the inode bitmap bit remains allocated, but `ext4_evict_inode()` explicitly says it cannot free the inode when mark-dirty fails, to prevent unsafe inode-number reuse. This is conservative recovery behavior, **not counted as a defect**. See [`evict-final-ENOMEM.log`](results/qemu-source-replay-20261101/evict-final-ENOMEM.log) and its `.e2fsck.log`. |
| Orphan-file slot remains occupied | Revised `EIO` injection sets the filesystem-device writeback-error sequence and calls real JBD2 write access; JBD2 detects it and aborts the journal | Log confirms `Aborting journal`; `e2fsck -fn` skips journal recovery and reports the orphan slot as not clean. That aborted, unrecovered image does not establish a committed orphan-slot accounting defect. **Not counted.** See [`orphan-access-EIO.log`](results/qemu-source-replay-20261101-native-jbd2/orphan-access-EIO.log) and its [`.e2fsck.log`](results/qemu-source-replay-20261101-native-jbd2/orphan-access-EIO.e2fsck.log). |
| Shared xattr block reference is not decremented | Revised `EIO` injection uses the same JBD2-native writeback-error check | Log confirms `Aborting journal`; `e2fsck -fn` skips recovery and reports no xattr block `h_refcount` mismatch. The earlier direct `EROFS` wrapper return did not execute JBD2's native error path and is superseded. **Not counted.** See [`xattr-release-EIO.log`](results/qemu-source-replay-20261101-native-jbd2/xattr-release-EIO.log) and its [`.e2fsck.log`](results/qemu-source-replay-20261101-native-jbd2/xattr-release-EIO.e2fsck.log). |
| `rename` setent dirty `-ENOSPC` | The faultable ext4 dirty-metadata wrapper substitutes the legal `-ENOSPC` result at its caller-visible boundary and routes it through ext4's common dirty-error handler; this hook skips `jbd2_journal_dirty_metadata()` and does not set the handle credits to zero | Offline checks report the same rename link-count inconsistency. This confirms the same conditional caller-path defect, but does not exercise JBD2's internal credit-exhaustion branch. Ordinary rename stress did not naturally return `ENOSPC`. **Same rename defect, not a separate root cause.** See [the final audit](FINAL-AUDIT-20261031.md) and [the no-injection stress report](../ext4-counter-trace/NATURAL-RENAME-JBD2-ENOSPC-20261031.md). |

The online `DIRLINK_RESULT`, `LINK_RESULT`, and `RENAME_RESULT` lines are
operation-time observations. The corresponding offline `e2fsck` diagnostics
are the persistence evidence for this injected run. The group-add row is
deliberately narrower than the earlier count-mismatch claim: the failed add
has not published group 1, so that unused descriptor must not be added to the
active-group totals. The direct persistent error is `e2fsck` reporting that
resize inode 7 references blocks beyond the old filesystem block count and
has an `i_blocks` mismatch; the raw descriptor only shows which resize data
was staged before `-ENOMEM` returned.

### Deep unlink reserve boundary

The ordinary unlink row injects at the `ext4_mark_inode_dirty_faultable()`
operation boundary. To audit the objection that this might bypass helper
side effects, the matrix also runs `unlink-reserve` with operation
`unlink_parent_reserve` and `EIO`. This point is inside
`ext4_reserve_inode_write()`, after `ext4_get_inode_loc()` has successfully
returned the inode-table buffer and at the real
`__ext4_journal_get_write_access()` boundary. The injected `EIO` is consumed
inside that wrapper, so the original `ext4_journal_abort_handle()` executes;
`ext4_reserve_inode_write()` then uses its normal `brelse()`/`iloc->bh = NULL`
cleanup and returns through the unmodified `__ext4_unlink()` error path. The
experiment does not call a shutdown API.

The verified run shows the native `aborting transaction` log from
`__ext4_journal_get_write_access`, followed by `unlink -> EIO`,
`SOURCE_FAULT_RESULT ... hits=1`, `UNMOUNT_RESULT rc=0`, and a normal power
down. Host-side `e2fsck -fn` then
reports `Unattached inode 13`. The host also preserves
[`unlink-parent-reserve-EIO.debugfs-stat-13.txt`](results/qemu/unlink-parent-reserve-EIO.debugfs-stat-13.txt),
whose raw `debugfs stat <13>` output contains `Links: 1`, and
[`unlink-parent-reserve-EIO.debugfs-root.txt`](results/qemu/unlink-parent-reserve-EIO.debugfs-root.txt),
which shows that the root directory no longer contains `file`. These files
are the direct disk-level link-count and directory-entry evidence for the
deeper boundary, separate from the earlier outer-wrapper run. The full guest
log is [`unlink-parent-reserve-EIO.log`](results/qemu/unlink-parent-reserve-EIO.log).

### B: native JBD2 global journal abort

Linux 6.12.1 has a filesystem-device error path in
`jbd2_journal_get_write_access()`: when `jbd2_check_fs_dev_write_error()` sees
a new writeback error on the filesystem device, JBD2 calls
`jbd2_journal_abort(journal, -EIO)` itself and returns `-EIO`. The
`unlink_parent_global_abort` case advances the filesystem device's writeback
error sequence immediately before calling the real JBD2 helper, so this
native check, abort, and return path execute. The ext4 wrapper does not call
`jbd2_journal_abort()` directly. This sets the exact `errseq_t` state JBD2
checks; it does not fail an actual QEMU block-device write.

The log confirms JBD2's `Aborting journal` message, `unlink -> EIO`, read-only
remount, `UNMOUNT_RESULT rc=0`, and normal power down. The raw post-unmount
image still has inode 13 with `Links: 1` and the `file` directory entry; the
specific unlink inconsistency is therefore not persisted in the home blocks.
Because the journal remains unclean, `e2fsck -fn` explicitly skips recovery.
A separate `e2fsck -fy` on a copy replays the journal and keeps the file
linked, but reports free-block and free-inode counts each one higher than the
bitmap totals, then repairs those counts and clears the clean
orphan-file-present flag. The matched no-fault precondition image recovers
cleanly. Thus the real global abort changes the outcome: it prevents the
tested dirent-versus-`nlink` mismatch, while leaving an aborted journal that
requires recovery and exposes a separate count discrepancy during recovery.
See [`unlink-parent-global-abort-EIO.log`](results/qemu/unlink-parent-global-abort-EIO.log),
the raw [`.e2fsck.log`](results/qemu/unlink-parent-global-abort-EIO.e2fsck.log),
[`debugfs stat <13>`](results/qemu/unlink-parent-global-abort-EIO.debugfs-stat-13.txt),
[`debugfs ls -l /`](results/qemu/unlink-parent-global-abort-EIO.debugfs-root.txt),
the [recovery log](results/qemu/unlink-parent-global-abort-EIO.e2fsck-recovery.log),
and the [recovered inode](results/qemu/unlink-parent-global-abort-EIO.recovered-debugfs-stat-13.txt)
and [root directory](results/qemu/unlink-parent-global-abort-EIO.recovered-debugfs-root.txt).
The clean comparison is
[`unlink-parent-precondition-EIO.e2fsck-recovery.log`](results/qemu/unlink-parent-precondition-EIO.e2fsck-recovery.log).

### C: inode-table read-result error cleanup

The `unlink_parent_inode_read` operation is passed only to the parent inode
lookup in `ext4_reserve_inode_write()`. After `sb_getblk()`, the injection
waits for any in-flight buffer I/O and routes to the same `read_error` cleanup
used by a failed inode-table read. It leaves `BH_Uptodate` and the cached data
unchanged. In particular, a cache hit does not trigger an artificial reread
of a valid buffer. `__ext4_get_inode_loc()` releases the reference, returns
`-EIO`, and `ext4_get_inode_loc()` reports `unable to read itable block`.
The injected error occurs before journal write access; the log has no JBD2
abort for this case. This tests the native helper error handling for an EIO
read result; it does not claim that a physical block read or bio failed.
The run records the target buffer state as
`uptodate=1 dirty=0 jbd=1 jbddirty=0 write_io_error=0` for inode-table block
41. Thus this particular lookup was a cache hit, and the injected failure
deliberately bypasses the cache-success result without changing that cached,
journal-head-attached buffer. A true device-read-error experiment still needs
to reach the ordinary cache-miss/read-I/O path.
The guest mounts with `errors=continue` (also the created image's recorded
error policy), so reporting the inode-table read error does not abort the
transaction. `ext4_delete_entry()` has already changed the directory block
before this parent-inode lookup fails; the continuing transaction can commit
that deletion while `drop_nlink(inode)` remains unreachable.

This case returns `unlink -> EIO`, unmounts successfully, and powers down
normally. Offline `e2fsck -fn` reports `Unattached inode 13`; the raw
`debugfs stat <13>` says `Links: 1`, and the root directory no longer contains
`file`. It independently reproduces the persistent unlink inconsistency
through the inode-table read-error path. See
[`unlink-parent-inode-read-EIO.log`](results/qemu/unlink-parent-inode-read-EIO.log),
its [`.e2fsck.log`](results/qemu/unlink-parent-inode-read-EIO.e2fsck.log),
[`debugfs stat <13>`](results/qemu/unlink-parent-inode-read-EIO.debugfs-stat-13.txt),
and [`debugfs ls -l /`](results/qemu/unlink-parent-inode-read-EIO.debugfs-root.txt).

## Error semantics at the injected boundaries

- `ext4_mark_inode_dirty()` can propagate inode-location errors such as
  `-EIO`, `-ENOMEM`, and `-EFSCORRUPTED`, journal access errors `-EIO` and
  `-EROFS`, and metadata-dirty errors from JBD2. The namespace and eviction
  hooks are placed at this helper boundary and accept only this downstream
  error set.
- Journal write-access hooks use the `-EIO` and `-EROFS` results returned by
  JBD2 write access.
- Journal metadata-dirty hooks use `-EUCLEAN`, `-EROFS`, `-ENOSPC`, and
  `-EINVAL` returned by `jbd2_journal_dirty_metadata()`; the no-journal
  synchronous path can also return `-EIO`.
- The deep `unlink_parent_reserve` hook is restricted to the return set of
  `__ext4_journal_get_write_access()` after inode-location success: `-EIO` and
  `-EROFS` from journal access. The injected error follows the wrapper's
  native `ext4_journal_abort_handle()` path before the caller's normal inode
  buffer cleanup.
- `unlink_parent_global_abort` models the filesystem-device writeback-error
  condition checked by `jbd2_journal_get_write_access()`. Its native branch
  invokes `jbd2_journal_abort()` and returns `-EIO`.
- `unlink_parent_inode_read` uses only the inode-table read path's native
  error cleanup and does not inject a journal write-access error. The
  injection is at the helper's read-result boundary; it does not submit or
  fail a physical block-device read, and it preserves cached buffer state.
- `ext4_mb_load_buddy()` can return allocation, bitmap-I/O, corruption, and
  internal mapping errors (`-ENOMEM`, `-EIO`, `-EFSCORRUPTED`, `-EINVAL`).
  `ext4_mb_mark_context_internal()` propagates bitmap/access/dirty errors.
- `ext4_mb_add_groupinfo()` returns `-ENOMEM` when its group-info allocations
  fail. The group-add hook is immediately after descriptor metadata is dirtied
  and before `ext4_update_super()` publishes the group count and global totals.

The dirty-metadata allow-list depends on the actual caller arguments. Hooks
which pass `inode == NULL` use the JBD2 return set only: `EUCLEAN`, `EROFS`,
`ENOSPC`, and `EINVAL`. The no-journal synchronous `EIO` path requires an
inode that needs synchronous writes, so it is accepted only for dirty hooks
that pass an inode. The checked QEMU cases that inject `EIO` at dirty points
target `ext4_mark_inode_dirty()`, whose earlier inode-location/access path
can return `EIO`; they do not claim `jbd2_journal_dirty_metadata()` itself
returns `EIO`.

The patch provides additional named points for the earlier and later mutation
boundaries in mkdir, rmdir, unlink, ordinary rename, rename exchange, orphan
cleanup, xattr release, and online resize. The checked-in QEMU matrix targets
nine primary inventory candidates and the additional unlink B/C semantic
checks; it does not claim to exhaust every point or every errno combination.

### Unlink inode-write preflight patch validation

The proposed fix is
[`0002-ext4-unlink-reserve-inode-write-access.patch`](patches/0002-ext4-unlink-reserve-inode-write-access.patch).
It reserves journal write access to the parent and target inode-table buffers
before changing the directory entry, while keeping the original namespace
mutation and link-count update order.

The clean Linux 6.12.1 source with this patch compiled `fs/ext4/namei.o`.
The patched fault-injection kernel also built successfully as bzImage #20. QEMU
injected `EIO` at each of the parent and target inode-table read-result and
journal write-access boundaries. In all four runs, `unlink` returned `EIO`,
the name and `Links: 1` remained on disk, normal unmount succeeded, and
`e2fsck -fn` passed. A no-fault unlink also completed normally and passed
`e2fsck -fn`. Raw guest logs, `debugfs stat <13>`, `debugfs ls -l /`, and
offline checks are in [`results/qemu-fix-validation/`](results/qemu-fix-validation/).

The read-result injection follows `ext4_get_inode_loc()`'s native `-EIO`
cleanup but this test hit an uptodate cached buffer; it does not claim a
physical block-device read failed. The journal-access injections execute the
normal `ext4_journal_abort_handle()` path. No forced-shutdown ioctl was used.

### Current mainline patch

The candidate fix is also applied to the current mainline tree at base commit
`d135ab453a7400a34930bdda1ef4bdc8acfac80e`, on branch
`ext4-unlink-reserve-inode-write-access` in `kernel/linux-mainline/`. The
mainline-specific diff is
[`mainline-d135ab-ext4-unlink-reserve-inode-write-access.patch`](patches/mainline-d135ab-ext4-unlink-reserve-inode-write-access.patch).
`scripts/checkpatch.pl --no-tree --no-signoff --ignore UNKNOWN_COMMIT_ID` and
`git diff --check` pass. The local mainline checkout is shallow and does not
contain the historical `Fixes:` commit object, so the checkpatch invocation
ignores only its unknown-commit warning; the hash and title were verified
against the upstream commit record.
`make O=/tmp/linux-mainline-d135ab-build ARCH=x86 W=1 -j8 fs/ext4/namei.o`
also passes after normalizing the config with the current tree's `olddefconfig`.
The changelog uses `Fixes: 4209ae12b122 ("ext4: handle ext4_mark_inode_dirty errors")`:
that commit made `__ext4_unlink()` propagate a parent inode dirtying failure
and return before decrementing the target inode's link count. The same change
also added error propagation in several unrelated ext4 operations; this patch
addresses only the unlink failure window demonstrated by the reproducer.

The QEMU fault matrix above was run on Linux 6.12.1 and has not been rerun on
this mainline revision. Its inode-read case follows native error cleanup but
hits an uptodate cache entry, so it does not model a failed physical read.
The fixed-kernel validation results are also from the Linux 6.12.1 tree:
all four parent/target inode-table read-result and journal write-access cases
returned `EIO` before changing the directory, unmounted normally, passed
`e2fsck -fn`, and retained `file` with inode 13 at `Links: 1`; the no-fault
control removed `file`, left `Links: 0`, and passed `e2fsck -fn`.
The patch was prepared with OpenAI Codex assistance; review and the submitter's
own DCO certification are still required before sending it upstream.
