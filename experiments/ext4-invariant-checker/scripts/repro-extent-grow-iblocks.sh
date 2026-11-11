#!/usr/bin/env bash
# Reproduce "extent tree grow_indepth leaves i_blocks charged for an
# unreachable metadata block" with natural memcg ENOMEM, and keep evidence
# under results/. Wraps tests/run-ext4-extent-grow-natural-enomem.sh.
set -euo pipefail

study=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
root=$(cd "$study/../.." && pwd)
build="$study/build/linux-invariant-checker"
out="${1:-$study/results/extent-grow-iblocks-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$out"

# Provenance: kernel, config, patch, source tree state.
cp "$build/.config" "$out/kernel.config"
sha256sum "$build/arch/x86/boot/bzImage" "$study/patches/0001-ext4-invariant-checker.patch" \
	>"$out/provenance.sha256"
grep -E 'CONFIG_EXT4_(INVARIANT_CHECKER|FAULT_INJECTION)' "$out/kernel.config" \
	>"$out/provenance-config.txt" || true
sed -n '1329,1342p' "$study/kernel/linux-6.12.1/fs/ext4/extents.c" \
	>"$out/source-grow-indepth-6.12.1.txt"

# The shared debian root image carries the orphan_file feature with an
# orphan inode larger than ext4_init_orphan_info() in 6.12.1 accepts, so the
# guest panics on root mount. Use a copy with the feature cleared.
base="$out/root-base.raw"
if [[ ! -r "$base" ]]; then
	cp --sparse=always "${EXT4_EXTGROW_NATURAL_ROOT_IMAGE:-$root/work/debian-clean-smalljournal.img}" "$base"
	tune2fs -O ^orphan_file "$base"
fi

EXT4_EXTGROW_NATURAL_KERNEL="$build/arch/x86/boot/bzImage" \
EXT4_EXTGROW_NATURAL_CONFIG="$build/.config" \
EXT4_EXTGROW_NATURAL_ROOT_IMAGE="$base" \
EXT4_EXTGROW_NATURAL_RUN_DIR="$out" \
	bash "$root/tests/run-ext4-extent-grow-natural-enomem.sh" || echo "runner_rc=$?" >>"$out/report.txt"

# Offline per-file evidence: i_blocks and the reachable extent tree.
img="$out/test.raw"
if [[ -r "$img" ]]; then
	for f in $(debugfs -R 'ls -p /' "$img" 2>/dev/null | awk -F/ '$6 ~ /^extent-/ {print $6}'); do
		{
			echo "=== /$f"
			debugfs -R "stat /$f" "$img" 2>/dev/null | grep -E 'Inode:|Blockcount|Links|Size'
			debugfs -R "ex /$f" "$img" 2>/dev/null
		} >>"$out/offline-extent-iblocks.txt"
	done
	e2fsck -fn "$img" >"$out/fsck-final.txt" 2>&1 || echo "fsck_final_rc=$?" >>"$out/fsck-final.txt"
	zstd -q -19 -f "$img" -o "$out/test.raw.zst" && rm -f "$img"
fi
rm -f "$out/root.qcow2"
echo "evidence: $out"
