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

## 2026-11-03 source revalidation

The source drift found on 2026-11-03 is confined to the current exported
counter-trace patch in `ialloc.c`, `inline.c`, `migrate.c`, `namei.c`, and
`resize.c`. The patch manifest covers all five files, and the complete patch
replayed against the verified Linux 6.12.1 source with zero fuzz. Review found
the changes add counter/namespace observations and caller-scoped fault hooks;
fault state initializes to `EXT4_FAULT_OFF`, and the disabled configuration
returns zero from every injection check. Counter setter wrappers retain the
underlying setter, resize-end returns the same overhead-update result as
before, and the inline-data length change widens the parameter from
`unsigned int` to `loff_t`.

The refreshed lexical inventory still contains 149 candidate sites. Its 58
added and 58 removed records reflect changed line locations and the new
instrumented setter/nlink call sites in those reviewed files. At that review the previous
kernel image predated the final source, patch, and compiler-config updates.
That rebuild requirement was historical: `python3 -B scripts/source_audit.py
--require-build` was rerun on 2026-11-11 and returned `passed=true, issues=[]`.
The current source inventory, kernel, config and exported patch match their
binding. This hash check does not rerun the build or prove campaign validity.
The host-side offline bigalloc parser correction does not change the bound
kernel artifacts.
