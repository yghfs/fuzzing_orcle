# Source-Derived Error Injection Plan

This plan follows the Linux 6.12.1 source in `kernel/linux-6.12.1/fs/ext4` and
`fs/jbd2`. The instrumented copy is `dev/linux-6.12.1`. Candidate selection,
hook placement, and errno sets below come from those source paths; runtime
observations and prior inventory classifications do not determine them.

The 88-operation catalog includes mutation-specific hooks, shared block
allocation/free boundaries, and one existing fast-commit hook outside these
candidate families. Each entry records the source path, errno being modeled,
and caller return behavior. Most hooks short-circuit immediately before a
source helper and feed that helper's eligible error code into the caller's
existing error path; they do not execute the helper or reproduce its internal
I/O/allocation side effects. The JBD2 access/dirty wrappers additionally model
the relevant handle/journal error state. The fast-commit claim hook wraps the
inode-bitmap read and returns its `-EIO` before bitmap inspection or mutation.
`inline_data_block_write_begin` wraps both callers of
`ext4_block_write_begin()`: the journaled conversion has already removed inline
data, while the delayed-allocation conversion still has its inline
representation. Both return `-EIO` before block mapping preparation; their
existing cleanup paths differ. Neither helper itself runs on its injected
path. None call
`ext4_force_shutdown()`. A returned error does not imply that ext4 rolls back
earlier in-memory changes or that those changes reached disk.

## JBD2 Error Sets

The errno catalog is `fault-errno-catalog.json`; the generated kernel whitelist
is `dev/linux-6.12.1/fs/ext4/fault_errno_catalog.h`.

| Hook boundary | Native return | Source behavior modeled |
| --- | --- | --- |
| `jbd2_journal_get_write_access()` | `-EIO` | A filesystem-device writeback error calls `jbd2_journal_abort()` before returning. The ext4 wrapper records the handle error. |
| `jbd2_journal_get_write_access()` | `-EROFS` | The handle or journal is already aborted. The ext4 wrapper returns through its normal abort-handle path. |
| `jbd2_journal_dirty_metadata()` | `-EUCLEAN` (`EFSCORRUPTED`) | The buffer has no JBD2 journal head; this early return precedes JBD2 buffer/credit mutation. The ext4 dirty-metadata error handler is used. |
| `jbd2_journal_dirty_metadata()` | `-EROFS` | The handle is already aborted. The helper's aborted-handle branch is modeled, then normal ext4 handling runs. |
| `jbd2_journal_dirty_metadata()` | `-ENOSPC` | The handle has no buffer credits. The ext4 dirty-metadata error handler marks the handle aborted and the caller receives the error. |

`do_get_write_access()` allocates frozen data with `__GFP_NOFAIL`, so that
allocation does not provide a native `-ENOMEM` return to inject. The dirty
metadata helper also has a later `-EINVAL` invariant-failure branch after it has
marked the buffer dirty and consumed a credit. That branch is excluded from
these hooks because returning it before JBD2 would not reproduce those prior
JBD2 mutations. The selected hooks model helper error returns inside the
candidate mutation windows, including allocation, read, checksum, quota, and
transaction metadata errors where the source helper can return them.
Pre-mutation syscall checks such as name lookup and argument validation are
not synthesized; their natural source exits remain unchanged.

### Return-path model used by the hooks

The 88 operations were checked against their actual call sites in the
instrumented Linux 6.12.1 tree and the matching pristine source. The checks
distinguish a caller-path simulation from executing the helper that could
produce the same errno:

| Hook class | Injection behavior | What the result establishes |
| --- | --- | --- |
| Journal write access (`*_access`, `mballoc_mark`, `mballoc_free_mark`, and the resize/group-add access hooks) | With a valid journal handle, the faultable wrapper returns before `jbd2_journal_get_write_access()`. For `EIO` it aborts the journal as that helper does; for `EROFS` it marks the handle aborted as the already-aborted-handle case requires. Without a valid handle, it delegates to the original non-faultable path. | The ext4 caller's handling of that access error. It does not execute JBD2 buffer access or create a real block-device writeback error. |
| Journal dirty metadata (`*_dirty`, `rename_dir_finish`, and bitmap/descriptor dirty hooks) | With a valid journal handle, the wrapper sets the ext4 buffer flags that precede JBD2, skips `jbd2_journal_dirty_metadata()`, and routes the selected errno through ext4's common dirty-metadata handler. Without a valid handle, it delegates to the original non-faultable path. | The ext4 caller's handling of the modeled dirty error. It does not consume JBD2 credits, attach journal metadata, or create real credit exhaustion. |
| Read, allocation, and reservation hooks | The fault is returned immediately before the named read/allocation/claim helper, except where the catalog and candidate table identify an earlier point in a multi-step operation. | The caller's cleanup/propagation for that error code. It does not execute the helper's I/O, allocate memory, or establish physical resource exhaustion. |
| `inline_data_block_write_begin` | The wrapper returns `-EIO` before `ext4_block_write_begin()` in both callers. `ext4_convert_inline_data_to_extent()` reaches it after destroying inline data; `ext4_da_convert_inline_data_to_extent()` reaches it with inline data still present and without a journal handle. | The journaled path enters orphan-add, journal-stop, and failed-write truncation cleanup. The delayed-allocation path unlocks/releases the folio and runs failed-write truncation. Neither path prepares block mappings at the hook. |

Thus each catalog errno is a source-valid return value for the named operation
or caller boundary, but a hook hit by itself is not evidence that the
underlying I/O, allocator, or JBD2 failure occurred. Findings must be based on
the separately observed caller result and resulting state.

The review checked all 88 catalog operations against their enum/parser
registration and source call sites. It found two placement ambiguities. In the
mkdir family, child/parent dirty IDs were also wired to earlier append/dirent
inode-dirty calls, so an armed hook could fail before the state stage named in
the candidate table. Those early assignments are removed; child and parent
access/dirty hooks now target the explicit marks in `ext4_mkdir()`. In group
add, `group_add_desc_access` also covered a superblock buffer; that access is
now left to the dedicated `group_add_super_access` hook, while descriptor
access remains on GDT and resize-inode pointer buffers. The
`group_add_desc_dirty` catalog anchor was corrected from
`ext4_add_new_descs()` to its actual injection site,
`ext4_setup_new_descs()`. The catalog also records that `xattr_release_access`
runs before the refcount branch and that `mkdir_append_alloc` has both append
and missing-block fallback sites. The inline-data write-begin hook also has two
real callers with different pre-hook state: one has destroyed inline data and
the delayed-allocation caller has not. The helper-boundary hook is valid for
both, but the prior single-state description was inaccurate and is now
branch-specific in the catalog and candidate table. This review is source
inspection, not evidence that each helper failure was produced by physical
I/O or real memory pressure.

## Candidate Boundaries

| Candidate | Hook(s) and source point | State already changed at the hook | Source error handling |
| --- | --- | --- | --- |
| Inode eviction bitmap release | `inode_evict_final_access`, `inode_evict_final_dirty` at the final `ext4_mark_inode_dirty()` in `ext4_evict_inode()` | Truncate, xattr cleanup, and orphan removal have run; `i_dtime` is assigned. The inode bitmap has not been freed. | On final inode dirty error, `ext4_evict_inode()` clears the in-core inode and skips `ext4_free_inode()`. The eviction callback has no syscall errno return. |
| Orphan-file slot release | `orphan_file_delete_access`, `orphan_file_delete_dirty` in `ext4_orphan_file_del()` | Access failure is before slot mutation. Dirty failure is after the slot is zeroed and `ob_free_entries` is incremented in memory. | Every exit clears the in-core orphan-file state/list. Dirty-helper error is returned by `ext4_orphan_file_del()`; callers may ignore it. |
| Shared xattr block reference decrement | `xattr_release_access` and `xattr_release_dirty` in `ext4_xattr_release_block()` | The access hook runs before `h_refcount` is read, so it applies to shared and final-reference blocks. Dirty failure is only on the shared-block path, after `h_refcount` decrement and checksum update. | The release helper is `void`, reports through `ext4_std_error()`, and its caller continues detaching the inode's external-xattr pointer. For a final reference, the separate `xattr_release_*bitmap*` and `xattr_release_*group_desc*` hooks cover `ext4_free_blocks()`, which is also `void`. |
| Group extend counter/bitmap ordering | `resize_extend_super_access`, `resize_extend_add_blocks`, `resize_extend_group_desc_access`, `resize_extend_bitmap_dirty`, `resize_extend_group_desc_dirty`, `resize_extend_super_dirty` | Superblock access failure is before count updates. The add-blocks hooks are reached after `ext4_group_extend_no_check()` updates superblock size/free counts. Bitmap/descriptor dirty hooks follow the bitmap and in-memory descriptor/global counter transition. Super dirty is after the bitmap operation. | Bitmap and descriptor errors return from `ext4_mb_mark_context()` through `ext4_group_add_blocks()`. The final super dirty return is ignored locally, but the aborted handle is observed by `ext4_journal_stop()`, whose error is returned by `ext4_group_extend_no_check()`. |
| Group add descriptor/global publication | `group_add_desc_access` in `ext4_add_new_descs()`, `group_add_desc_dirty` in `ext4_setup_new_descs()`, and `group_add_super_dirty` in `ext4_flex_group_add()` | The descriptor access hook covers GDT and resize-inode pointer buffers; the superblock access has its own hook. Descriptor access failure precedes descriptor initialization. Descriptor dirty failure follows filling/checksumming the new descriptor but precedes `ext4_update_super()`. Super dirty failure follows `ext4_update_super()`, which publishes the new group count and updates superblock and global counters. | Descriptor error breaks setup and returns through journal stop. Super dirty error is retained in `err`, then journal-stop error is used if no earlier error. The source has no general rollback of the already published in-memory counters. |
| `rmdir()` link-count sequence | `rmdir_dirent_delete`, `rmdir_dirent_dirty`, `rmdir_child_access`, `rmdir_child_dirty`, `rmdir_parent_access`, `rmdir_parent_dirty` in `ext4_delete_entry_internal()` and `ext4_rmdir()` | Dirent access is before deletion. Dirent dirty is after the in-memory dirent deletion. Child inode access/dirty follows child nlink/size clear and orphan add, before parent decrement. Parent inode access/dirty follows the parent nlink decrement. Inline directories use the same access/dirty hook IDs through `ext4_delete_inline_entry()`. | Each returned error goes to `end_rmdir`; the direct `retval` is returned. The function does not restore earlier in-memory dirent or nlink changes. |
| `unlink()` dirent/inode sequence | `unlink_dirent_delete`, `unlink_dirent_block_dirty`, `unlink_dirent_inode_access`, `unlink_dirent_dirty`, `unlink_inode_access`, `unlink_inode_dirty` in `__ext4_unlink()` | Dirent access precedes deletion. Block dirty follows in-memory dirent deletion. Parent inode access/dirty follows mtime/iversion update but precedes target nlink decrement. Target inode access/dirty follows nlink decrement and possible orphan add. Inline directories use the same access/dirty IDs through `ext4_delete_inline_entry()`. | Errors from the deletion and inode-dirty calls leave through `out_handle` and the direct `retval` is returned. There is no local rollback of earlier changes. |
| `rename()` dirent, `..`, and link-count sequence | `rename_dir_prepare_access`, `rename_setent_access`, `rename_setent_inode_access`, `rename_setent_inode_dirty`, `rename_setent_dirty`, `rename_old_inode_access`, `rename_old_inode_dirty`, `rename_delete_old`, `rename_delete_old_dirty`, `rename_dir_finish`, `rename_dir_finish_inode_access`, `rename_new_parent_access`, `rename_new_parent_dirty`, `rename_old_parent_access`, `rename_old_parent_dirty`, `rename_replaced_inode_access`, `rename_replaced_inode_dirty`, `rename_exchange_old_parent_access`, `rename_exchange_old_parent_dirty`, `rename_exchange_new_parent_access`, `rename_exchange_new_parent_dirty` | Each hook is after the source mutations that precede its named helper call. In particular, setent dirty follows dirent replacement; dir-finish dirty follows in-memory `..` replacement; parent hooks follow link-count updates. Inode-table access hooks fail before raw-inode update; dirty hooks fail after it. | Most named helper errors go to `end_rename`. `ext4_rename_delete()` is `void` and only warns on deletion failure, so the caller continues. `ext4_update_dir_count()` is also `void` and ignores both inode-table access and dirty errors; the exchange-specific hooks preserve that behavior. Inline `..` finish uses the faultable inode-dirty path. |
| `mkdir()` child/parent/free-inode sequence | `mkdir_dirblock_dirty`, `mkdir_child_access`, `mkdir_child_dirty`, `mkdir_parent_access`, `mkdir_parent_dirty` in `ext4_init_new_dir_faultable()` and `ext4_mkdir()` | Dirblock dirty follows creation of `.`/`..` and initial child nlink. Child inode access/dirty is injected only at the explicit `ext4_mkdir()` mark after directory initialization and before parent dirent insertion. Parent inode access/dirty is injected only at the explicit `ext4_mkdir()` mark after dirent insertion and parent nlink increment. The internal inode dirties used while appending a directory block or inserting the parent dirent remain on the original non-faultable path. | Child errors enter `out_clear_inode`: child nlink is cleared, the child is orphaned, and a cleanup inode-dirty is attempted. Parent errors enter the same cleanup after parent dirent/link-count mutation; this cleanup does not roll back those parent mutations. Cleanup can itself see the aborted handle and replace `err`; journal-stop return is ignored on this path. |

The hooks describe error exits and mutation order, not expected postconditions.
Each run must record the injected return (or actual helper return when the
helper executes), caller return behavior, in-memory state, and raw on-disk
state independently before making any finding claim.

## Additional Source Exits

The candidate table emphasizes the mutation sequence. These helper exits are
also instrumented because they can stop or partially complete those sequences:

| Candidate path | Additional hooks | Source behavior at the injection point |
| --- | --- | --- |
| Inode eviction | `mballoc_free_mark`, `inode_evict_bitmap_read`, `inode_evict_bitmap_access`, `inode_evict_group_desc_access`, `inode_evict_group_desc_dirty`, `inode_evict_bitmap_dirty` | Block freeing during truncate can fail before its bitmap transition. `ext4_free_inode()` clears the in-core inode before reading the inode bitmap. A group-descriptor access error is recorded, but the source then clears the inode bitmap bit, skips descriptor/global counter updates, and still attempts to dirty the bitmap. Descriptor-dirty failure occurs after the bit and counters changed in memory; bitmap dirty is still attempted and the first error is retained. |
| Shared xattr release | `mballoc_free_mark`, `xattr_release_bitmap_access`, `xattr_release_group_desc_access`, `xattr_release_bitmap_dirty`, `xattr_release_group_desc_dirty` | When `h_refcount == 1`, block release is `void` at the xattr caller boundary. Access errors precede the bitmap transition; dirty errors follow the in-memory bitmap, descriptor, and counter updates. `ext4_free_blocks()` reports the error through `ext4_std_error()` and the caller continues detaching the inode's xattr pointer. |
| Group extend | `resize_extend_buddy_load`, `resize_extend_bitmap_read` | Both failures can occur after `ext4_group_extend_no_check()` has updated the in-memory superblock block/free counts, but before `ext4_group_add_blocks()` changes the bitmap. The error returns through the resize path; there is no local restoration of the earlier counts. |
| Group add | `group_add_super_access`, `group_add_bitmap_read`, `group_add_groupinfo_alloc` | Superblock access fails before descriptor publication. Bitmap-read failure follows descriptor initialization in memory but stops setup before it is completed. Group-info allocation failure follows descriptor dirtying and stops before `ext4_update_super()` publishes the new group count and global counters. |
| Fast-commit inode claim | `fast_commit_inode_claim` substitutes an `ERR_PTR(-EIO)` for `ext4_read_inode_bitmap()` in `ext4_mark_inode_used()` | The inode bitmap has not been inspected or changed. The existing bitmap-read error exit returns the error before inode-bit claim. | The replay callback receives the error; the trace fixture observes a later tag entering the claim for the same inode and completing it. This models the read helper's return without submitting physical I/O. |
| mkdir | `mballoc_mark`, `mkdir_append_alloc`, `mkdir_add_entry_read`, `mkdir_append_block_access`, `mkdir_add_entry_access`, `mkdir_add_entry_dirty` | `mkdir_append_alloc` short-circuits before `ext4_bread()` both in `ext4_append_faultable()` and in `ext4_add_entry()`'s missing-block fallback; each propagates into `out_clear_inode`. Read/access/dirty failures return through `ext4_init_new_dir_faultable()` or `ext4_add_entry()` into the same cleanup. Append-block access occurs after block allocation and the in-memory size update. Parent dirent dirty failure occurs after the dirent and parent timestamps changed in memory; mkdir then runs its existing child-inode cleanup. |

## Formula-Oriented Error Exits

These hooks target formula boundaries documented in `公式.md`. Dirty-stage
hooks enter through the faultable JBD2 dirty-metadata wrapper and retain the
source caller's error path; their errno whitelist is `EFSCORRUPTED`, `EROFS`,
and `ENOSPC`. Allocation/reservation hooks use their catalogued source points
and errno sets. The inline-data conversion hook returns `-EIO` immediately
before the write-begin helper.

| Formula | Hook(s) and source point | State at the dirty return | Caller behavior |
| --- | --- | --- | --- |
| `R-MIGRATE` | `migrate_swap_inode_data` at the existing inode dirty call in `ext4_ext_swap_inode_data()` | The extent mapping, EXTENTS flag, and `i_blocks` have been switched. The source calls the inode dirty helper after `free_ind_block()` even when that helper returned an error. | The dirty hook is still reached after an earlier free error, but the caller preserves the earlier `free_ind_block()` error. Otherwise the injected dirty error is returned through the migration cleanup path, which frees the temporary extent tree. The hook does not change the source transaction-credit budgets. |
| `R-XATTR-ATOMIC` | `xattr_set_inode_dirty` at the final inode dirty call in `ext4_xattr_set_handle()` | Xattr body/block updates, inode pointer changes, ctime, and i_version have already been applied in memory. | The dirty error is returned from xattr set and follows its normal buffer release, xattr unlock, and journal-stop path. Earlier in-memory changes are not claimed to be rolled back. |
| `EA-REF-DELTA` | `ea_inode_ref_dirty` in `ext4_xattr_inode_update_ref()` | EA inode reference count and the associated zero-to-one/one-to-zero nlink/orphan transition have already changed in memory. | The helper returns the dirty error; its caller may attempt the existing cleanup decrement or may report and continue, depending on the xattr path. The event records the exact reference boundary. |
| `R-ACL-MODE` | `acl_mode_dirty` after `ext4_set_acl()` updates mode for an access ACL | The access ACL xattr and in-memory mode/ctime have changed; this point is only used when the ACL operation actually changes mode. | The dirty error follows the existing ACL journal-stop and ENOSPC retry path. |
| `R-INLINE-DIR` | `inline_dir_block_dirty`, then `inline_dir_inode_dirty`, in `ext4_finish_convert_inline_dir()` | First point: the converted directory block contents and checksum are prepared. Second point: the block has been dirtied and marked verified; the inode size and disksize were expanded. | Either error returns to `ext4_convert_inline_data_nolock()` and enters its existing inline-data restore path. |
| `R-INLINE` | `inline_data_block_write_begin` immediately before `ext4_block_write_begin()` in both inline-data-to-extent conversion paths | Journaled path: `ext4_destroy_inline_data_nolock()` has removed the inline representation. Delayed-allocation path: inline data remains present. Neither path has prepared the first block mapping at the hook. | The journaled caller uses orphan-add, journal-stop, and failed-write truncation; the delayed-allocation caller releases the folio and runs failed-write truncation. The fixture independently reads `I_i` with `FS_IOC_GETFLAGS` and `M_i` with `FIEMAP`; the collector evaluates `I_i ∨ M_i` and records a mismatch as a candidate without treating the hook hit itself as a bug. |
| `R-FILEATTR` flags | `fileattr_flags_dirty` in `ext4_ioctl_setflags()` | At least one ordinary user-modifiable flag has changed in memory and the raw inode has been updated. No hook is offered for a no-op request or the separately handled journal-data/extents transition. | The error follows `flags_err`, stops the journal handle, and returns from the ioctl. |
| `R-FILEATTR` project id | `fileattr_project_dirty` in `ext4_ioctl_setproject()` | Quota transfer succeeded and the new project id, ctime, and i_version are set in memory. | The error returns through `out_dirty` and journal stop. A quota-transfer error uses the original non-faultable dirty call and does not consume this hook. |

The remaining formula/source hooks have distinct caller contracts that should
not be inferred from their errno alone:

| Hook | Exact source boundary | State and caller behavior |
| --- | --- | --- |
| `delalloc_reserve` | After `dquot_reserve_block()` and before `ext4_claim_free_clusters()` in `ext4_da_reserve_space()` | Injected `-ENOSPC` releases the just-acquired quota reservation and exits before `i_reserved_data_blocks` or `s_dirtyclusters_counter` changes. The write path may consume the error and retry through its non-delalloc fallback. |
| `es_insert_alloc` | In `__es_insert_extent()` when no node was preallocated, after `ext4_es_insert_extent()` has already called `__es_remove_extent()` | Injected `-ENOMEM` prevents the replacement cache node from being published. For this ordinary non-delayed status, the caller intentionally treats the optional cache insertion failure as success; it does not retry the allocation or return the error to the syscall. |
| `extent_insert_dirty` | Final publish dirty call in `ext4_ext_insert_extent()` after extent-tree/index edits | The common dirty helper returns the injected JBD2 error. The caller frees its temporary path and propagates the error, but does not undo the earlier in-memory extent-tree edits. |
| `mb_buddy_load` | Entry to `ext4_mb_load_buddy_gfp()` before bitmap/buddy folios are loaded and validated | The selected helper error returns before buddy state is handed to the caller. Allocator/free callers then use their existing path-specific cleanup or skip behavior; this hook does not establish actual I/O failure, memory exhaustion, or checksum failure. |

The exact errno choices and source references for every hook, including the
hooks in the main table, are maintained in `fault-errno-catalog.json` and its
generated kernel whitelist. The catalog and enum currently contain 88 matching
operations; each operation has an ext4 C call site. `runtime_random` means that
the kernel path is available for explicit experiments. The separate top-level
`random_admission` list is the reviewed default pool for long campaigns and is
intentionally smaller until a hook has a deterministic fixture, native syscall
envelope, and userspace return-chain evidence. Errno selection is validated
against each operation's own whitelist.
