# Ext4 authoritative write audit

`source-inventory.json` is the reviewed lexical inventory for the Linux 6.12.1
tree used by this project. It hashes every `fs/ext4/*.c` and `*.h` file plus
the counter trace header and records candidate writes to descriptor fields,
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

The approved enabled kernel, its configuration, and replay-verified patch are
bound in `build-binding.json`. The no-trace configuration is compiled as a
separate compatibility check; long campaigns always use the bound enabled
artifact.
