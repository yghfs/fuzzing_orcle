# Host setup

For a clean x86-64 Debian/Ubuntu host, install and build dependencies with
`bash tools/bootstrap.sh`. The root `README.md` documents host requirements
and the QEMU smoke test. Generated kernel trees, toolchains, VM images,
private keys, and campaign output are host-local and ignored by Git.

Load this checkout's tool paths with `. tools/env.sh`. Go 1.26.0 is installed
locally under `tools/go126/bin/`; syzkaller is fetched at the revision in
`tools/syzkaller-revision.txt`.

Generate a host-specific manager config from a tracked template with:

```sh
python3 tools/configure-syzkaller.py tools/ext4-manager.cfg
```

This writes an ignored `tools/ext4-manager.local.cfg` with paths to the rebuilt
invariant-checker kernel, local guest image and key, syzkaller checkout, and
work directory.

Load the paths for a shell with:

```sh
. tools/env.sh
```


The checker is enabled in the kernel config with:

```sh
scripts/config --enable CONFIG_EXT4_INVARIANT_CHECKER
scripts/config --disable CONFIG_EXT4_FAULT_INJECTION
make olddefconfig
```

After boot, read `/sys/kernel/debug/ext4/<device>/invariant_check` to run a
consistent snapshot and print the recent metadata event ring. Set the built-in
parameter `ext4_checker_panic=1` on the kernel command line to panic on the
first mismatch. The report also includes `violations_total`, a sticky count of
operation-level and snapshot mismatches observed since the filesystem mount.

The normal runtime checker smoke test is:

```sh
tests/ext4-checker-smoke.sh
```

Fault injection is a separate candidate validation facility. It requires
`CONFIG_EXT4_FAULT_INJECTION=y` in a separate kernel and uses
`ext4.ext4_fault_mode`, independently of the checker. Synthetic checker
modes 1..4 have been removed. Historical experiments below used the old
combined interface; see [tests/README.md](../tests/README.md) for current
build commands and validation entry points.

The ext4-focused syzkaller seed and manager configuration are:

```sh
tests/syzkaller-ext4-workload.sh
python3 tools/configure-syzkaller.py tools/ext4-manager.cfg
tools/syzkaller/bin/syz-manager -config=tools/ext4-manager.local.cfg -mode=smoke-test
SYZ_REPEAT=100 SYZ_PROCS=4 SYZ_THREADED=1 tests/run-ext4-checker-workload.sh tests/ext4-race-workload.syz
python3 tools/configure-syzkaller.py tools/ext4-checker-targeted.cfg
tools/syzkaller/bin/syz-manager -config=tools/ext4-checker-targeted.local.cfg
```

`tests/ext4-workload.syz` exercises file allocation, truncation, fallocate,
xattrs, hard/symbolic links, rename, nested directories and orphan cleanup.
`tests/ext4-race-workload.syz` collides namespace replacement with extent,
copy-range and xattr updates; the targeted manager config also enables mmap,
file-range and fd-based xattr calls.
`tests/ext4-move-ext-workload.syz` and `tests/ext4-sparse-move-workload.syz`
exercise `EXT4_IOC_MOVE_EXT` with initialized, unwritten, sparse, and
cross-inode extent ranges. `tests/ext4-range-mmap-workload.syz` combines
extent shifting, punch/zero/insert/collapse ranges, mmap invalidation, and
`EXT4_IOC_GET_ES_CACHE` queries.
`tests/ext4-fiemap-clone-workload.syz` targets FIEMAP/ES-cache queries,
`FS_IOC_FSSETXATTR`, copy-range, sync/fadvise, and FIEMAP after rename/unlink.
The checker event ring now reports event type, operation id, inode, group,
physical range, journal transaction id and delta.
`tests/ext4-checker-dir-lifecycle-workload.syz`,
`tests/ext4-checker-extent-state-workload.syz`,
`tests/ext4-checker-journal-checkpoint-workload.syz`, and
`tests/ext4-checker-delalloc-cache-workload.syz` are checker-focused sequences
for directory accounting, extent transitions, journal checkpoints, and delayed
allocation/cache state. `tests/ext4-checker-cross-move-workload.syz` combines
cross-file copy, sparse extent transitions, `MOVE_EXT`, FIEMAP/ES-cache,
writeback, truncate, unlink, and checkpoint paths in one bounded sequence.

The global-counter rule treats the percpu aggregate matching the sum of group
descriptor counters as authoritative. The event-derived global shadow is
reported as `global_shadow_drift` only: ext4 recovery and bitmap-error paths
can maintain percpu counters outside the normal metadata delta hooks, so that
drift alone is not a filesystem violation. Snapshot reports also classify
bitmap, descriptor checksum/bounds, group shadow, and global-sum failures.

The global event shadow can temporarily drift from ext4's percpu counters
because ext4 updates those counters before entering the checker hook. That
drift is diagnostic only; stable invariants compare the percpu aggregate with
the sum of group descriptors and independently compare allocation bitmaps
with their descriptors.

For a broader ext4 syscall campaign, use `tools/ext4-full-manager.cfg`. Two
bounded campaigns on 2026-09-26 and a one-hour campaign on 2026-09-27 completed
without a kernel crash, WARN/OOPS, KASAN/UBSAN report, or saved syzkaller
repro. A later clean-image regression found that the one-hour campaign's
`ext4_checker_event_cap=65536` made the checker allocate a multi-megabyte event
ring with `kcalloc`; that contiguous allocation failed at ext4 mount, so the
campaign's lack of checker violations is not a valid checker result. The
event-ring allocation now uses `kvcalloc`/`kvfree`. The follow-up clean-image
campaign ran with the checker active; its `tx_underflows` were traced to
no-journal handles using transaction id 0 without a matching stop event. The
checker now ignores that id.

```text
campaign 1: about 4.5 minutes, 4,371 executions, coverage 7,144, corpus 240
campaign 2: 5 minutes, 5,360 executions, coverage 7,909, corpus 317
campaign 3: 59 minutes of VM fuzzing, 46,945 executions, coverage 23,090,
            corpus 1,328; checker disabled by allocation failure
campaign 4: 60 minutes of VM fuzzing, 39,666 executions, coverage 25,625,
            corpus about 1,266; clean base image, checker active
```

Campaign 4 also ran the dedicated FIEMAP/copy-range and range/mmap workloads.
Its snapshots stayed `OK`; the only nonzero checker
counter was the diagnosed no-journal pairing false positive. With build #34,
both dedicated workloads finish with `snapshot OK`, `violations_total=0`,
`tx_underflows=0`, and no dropped events or KASAN/Oops/WARN/ext4 error. The
all-manager config explicitly enables mmap, copy-range, sync/fadvise, FIEMAP,
`MOVE_EXT`, and ES-cache ioctls. These are negative results for the tested
configurations and durations, not a proof that ext4 has no vulnerabilities.
KASAN and KCOV were enabled; KCSAN was not, and the campaign used one QEMU VM.

The earlier two-hour clean-image attempt used
`tools/ext4-full-2h-build34.cfg`; its work directory was removed during the
2026-10-06 cleanup. It stopped after about 86,989 executions
because the guest root filesystem was shut down by a broad random ioctl and
the executor then reported `SYZFAIL: posix_spawnp failed (EIO)`. Those are not
classified as ext4 bugs. Build #35 therefore used a focused ioctl whitelist
and disabled unrestricted `ioctl`, `EXT4_IOC_SHUTDOWN`, and online resize
calls. It ran about 17.7k executions before checker `TX_END` underflows were
observed. Source inspection found that `ext4_journal_restart()` changes the
JBD2 transaction attached to a live handle without checker boundary events;
the checker now records the old transaction end and the new transaction begin
around that restart.

Build #36 passed a clean-image smoke test and checker fault modes 0–4. A
100-repeat, 4-process replay of `tests/ext4-checker-cross-move-workload.syz`
completed with `snapshot OK`, `violations_total=0`, `tx_underflows=0`, and no
dropped events. During its full fuzz run, two TX_END underflows were traced to
`fs/ext4/super.c:update_super_work()`, which directly starts/stops a JBD2
handle while bypassing the ext4 journal wrappers. The historical diagnosis identified a checker instrumentation gap, not a
confirmed ext4 inconsistency or kernel vulnerability. Its bulky run evidence
was removed during the 2026-10-06 cleanup.

Build #37 adds checker TX_BEGIN/TX_END events around all three direct-stop
branches in `update_super_work()`. Its 100-repeat cross-move replay and a
1000-repeat, 4-process `tests/ext4-workload.syz` replay both finish with
`snapshot OK`, `violations_total=0`, and `tx_underflows=0` (the larger replay
only wrapped the bounded event ring). The clean-image campaign used
`tools/ext4-full-2h-build37-safe.cfg`; its work directory was removed during
the 2026-10-06 cleanup. It inherited the build #36 corpus,
keeps the focused ioctl whitelist, and receives the checker-specific seeds
through `/addcandidate`. Its two-hour interval began at 2026-09-28 05:09:22.
The manager enables 236 syscall variants; KASAN and KCOV are active, KCSAN is
not, and the campaign used one QEMU VM. Its run logs were removed during
the 2026-10-06 cleanup. At that point there was no confirmed ext4 runtime
vulnerability from those campaigns.

The smoke test completed successfully on 2026-09-26: the guest booted from
its ext4 disk, SSH worked, KCOV coverage was enabled, and syzkaller identified
2356 usable syscall variants. The guest emits a non-fatal systemd remount
warning. Other disabled features in the machine-check output belong to
subsystems outside the ext4 MVP. `procs: 1` limits syzkaller to one executor
process per VM; the syscall-threading policy for the ext4 workload still needs
to be set explicitly when that workload is designed.

The 2026-09-28 build #38 clean-image one-hour campaign used
`tools/ext4-full-1h-build38-sharp.cfg`. Its bulky work directory was removed
during the 2026-10-06 cleanup. It ran from
11:31 to 12:31, reached 21,054 executions and 28,141 coverage, and reported
zero syzkaller crashes. Source-guided extent and journal candidates were
injected through `/addcandidate`. One checker transaction-pairing underflow
appeared between the 11:57 and 12:02 snapshots, but subsequent snapshots
remained `snapshot OK`. It is not a confirmed ext4 runtime vulnerability:
the old checker overwrote its transaction report with the next snapshot, and
the monitor retained only the last few kernel log lines. The guest VM reached
its automatic one-hour restart before the monitor's final paused snapshot.
Build #39 retains `last_violation` in debugfs and the monitor now records
invariant warnings separately and samples just before the campaign deadline.
Build #40 adds the trigger's pid, CPU and event sequence to the sticky
transaction-pairing report. A 40-minute clean-image replay of the build #38
corpus (`tools/ext4-underflow-repro-build40.cfg`) reached 15,162 executions
and 28,031 coverage; the final snapshot was `OK`, with
`violations_total=0`, `tx_underflows=0`, and zero syzkaller crashes.
`tools/monitor-ext4-underflow.sh` polls the checker and, on a nonzero
violation, pauses the manager and saves the full event ring and guest dmesg.

## Bigalloc pending-reservation failure test

The source-guided workload `tests/ext4-bigalloc-pending-reservation.c` creates
two delayed boundary extents on a 64 KiB-cluster bigalloc filesystem, allocates
the range between them with `fallocate()`, then truncates, syncs, unlinks, and
closes the file. If the second pending-reservation node allocation fails,
`__revise_pending()` leaves its first insertion in the tree but returns
`-ENOMEM` without reporting partial progress. The retry counts the first node
as a duplicate and releases only one of two reservations.

`tests/run-ext4-bigalloc-pending-reservation.sh` runs a control and a
deterministic failure pass using checker fault mode 5. This local test hook
fails the second `GFP_ATOMIC` pending-node allocation and is disabled by
default. Three independent QEMU runs retained one cluster (16 blocks) after
truncate/unlink and emitted `i_reserved_data_blocks (1) not cleared!`; control
runs returned to zero. Checker snapshots and offline journal/e2fsck checks were
clean because the inconsistency is in volatile reservation accounting.

Linux master commit `ce1e0223d8ad4211275c82a17ed6d43ab81e13d9` (2026-10-01)
still has the same `__revise_pending()` logic and retry accounting. No fix was
present at that revision. The full source analysis and run paths are recorded
in `ext4-runtime-invariant-audit.md`.
