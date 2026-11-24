# ext4 Fast-Commit Directory Count Finding

Status: confirmed natural-syscall `R-UG` violation; reproduced on 2026-11-24.

## Finding

When fast-commit replay restores a newly created directory, the inode bitmap and free-inode descriptor are updated, but the group's used-directory count is not. The restored inode is therefore an allocated directory while `bg_used_dirs_count` remains one too small. This violates `R-UG` and leaves the count used by ext4's directory-allocation heuristics inaccurate after recovery.

In Linux 6.12.1, [`ext4_fc_replay_create()`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/fast_commit.c#L1652) calls `ext4_mark_inode_used()` before initializing and linking the inode. [`ext4_mark_inode_used()`](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/ialloc.c#L807) sets the inode bitmap and decrements free inodes, but does not inspect the inode mode or update `bg_used_dirs_count`. The normal allocator does increment the directory count for `S_ISDIR(mode)` at `ialloc.c:1291`.

## Natural Trigger

The test creates a full-commit baseline, then issues ordinary filesystem operations:

1. `mkdir(".../fc/child")`.
2. Create a file inside `child` and call `fsync()` on that file to publish the fast-commit records.
3. Hard-stop the VM from the host without a guest sync or clean unmount.
4. Boot again and mount the ext4 filesystem, causing journal recovery and fast-commit replay.
5. Verify the child directory and its file are visible, then compare directory inodes with group descriptors.

The guest workload runs under the unified checker. See [special-fc-workload.c](tests/special-fc-workload.c) and [run-special.sh](scripts/run-special.sh).

## Evidence

The 2026-11-24 run reproduced the finding. The raw trace, disk image, and
reports were deleted in the results cleanup. The observations below summarize
that run.

- Standard ext4 replay trace records `tag 3` (`EXT4_FC_TAG_CREAT`) for directory inode 14. The checker trace records inode 14's replay bitmap claim and directory initialization.
- Post-replay syscall verification succeeds: `fast_commit_verify=ok`.
- Stable runtime snapshot records group 0 directory count 3 while its independent inode-mode scan finds 4. The corresponding `SNAPSHOT.MEMORY.STABLE` `R-UG` event is valid and has `actual=3`, `expected=4`.
- The independent offline checker reports `descriptor_dirs=3`, `inode_dirs=4`, `r_ug.valid=false` for group 0.
- `e2fsck -fn` independently reports `Directories count wrong for group #0 (3, counted=4)` and exits 4.
- The targeted analyzer validated the new run's trace, expected runtime mismatch signature,
  offline checker result, and fsck diagnostic with `finding_reproduced=true, issues=[]`.

The recovered directory also exposes a directory link-count discrepancy in the same replay chain. This report counts one finding for the `R-UG` violation. The other `e2fsck` diagnostics were secondary observations.

## Mainline Check

The historical mainline check used torvalds/linux commit `0fcb05b72344f40806cbe810de8872f1698bf053` (2026-10-14). In that tree, `fs/ext4/ialloc.c:761` still has `ext4_mark_inode_used()` updating free inodes without updating used directories, and `fs/ext4/fast_commit.c:2031-2078` still replays a directory create without compensating for that omission. This specific `R-UG` issue was not fixed at that revision. The latest upstream state was not rechecked during the 2026-11-24 local reproduction.
