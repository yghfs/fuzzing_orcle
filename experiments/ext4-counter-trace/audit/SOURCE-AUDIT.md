# Ext4 authoritative write audit

`source-inventory.json` is the reviewed lexical inventory for the Linux 6.12.1
tree used by this project. It hashes the ext4 source and build files, trace
event headers, and the changed JBD2 source and header. It records candidate writes to descriptor fields,
per-cpu summaries, bitmap bits, group flags, namespace links, directory
entries, and superblock summaries.

The inventory is a drift guard. It is not a C compiler proof: helper internals,
constant initialization, test fixtures, and disk-image parsing are classified
separately below. A changed source or candidate list fails the campaign
preflight until a maintainer reviews and regenerates the inventory.

Authoritative semantic mutations are covered by the `ext4_ct_*` wrappers and
point-specific events in the development tree. Bitmap metadata/padding writes,
buddy/preallocation copies, inode-on-disk serialization, and test-only fixtures
are recorded as non-formula layout or representation writes; they do not alter
the active counter formulas. Fast-commit replay, resize, corruption exclusion,
lazy materialization, and internal/EA inode link updates are separate domains.

After a successful build from the reviewed source, the build script records
the inventory, kernel, configuration, and exported patch hashes in the local
build directory's `build-binding.json`. From the repository root, check the
normal build with:

```sh
python3 experiments/ext4-counter-trace/scripts/source_audit.py --require-build \
  --kernel-obj experiments/ext4-counter-trace/build/linux-counter-trace-normal
```

Without `--kernel-obj`, verification checks the unified output under
`build/linux-counter-trace-normal/`. The binding checks the identity of the
reviewed inventory, exported patch, build configuration, and kernel image.
VM regressions are run separately.

The 2026-11-24 patch replay applied the current ext4/JBD2 changes to the
verified Linux 6.12.1 source with zero fuzz. The unified build passed the
binding check against the refreshed source inventory.
