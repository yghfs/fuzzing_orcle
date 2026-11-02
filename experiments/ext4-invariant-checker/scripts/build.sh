#!/usr/bin/env bash
set -euo pipefail

study=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ ! -f "$study/kernel/linux-6.12.1/Makefile" ]]; then
	bash "$study/scripts/prepare-source.sh"
fi
source="$study/kernel/linux-6.12.1"
out="$study/build/linux-invariant-checker"
config="$study/config/x86_64.config"
cc=${EXT4_CT_CC:-gcc-14}
if ! command -v "$cc" >/dev/null 2>&1; then
	if [[ -n "${EXT4_CT_CC:-}" ]]; then
		echo "requested kernel compiler not found: $cc" >&2
		exit 1
	fi
	cc=
	for candidate in gcc-13 gcc-12 gcc; do
		if command -v "$candidate" >/dev/null 2>&1; then cc=$candidate; break; fi
	done
fi
command -v "$cc" >/dev/null 2>&1 || { echo 'no supported kernel compiler found' >&2; exit 1; }
mkdir -p "$out"
if [[ ! -e "$out/.config" ]]; then
	cp "$config" "$out/.config"
fi
"$source/scripts/config" --file "$out/.config" \
	--enable EXT4_INVARIANT_CHECKER --enable EXT4_FAULT_INJECTION \
	--disable EXT4_COUNTER_TRACE --enable DEBUG_FS --enable KCOV --enable KASAN
make -C "$source" O="$out" ARCH=x86_64 CC="$cc" HOSTCC="$cc" olddefconfig
grep -qx 'CONFIG_EXT4_INVARIANT_CHECKER=y' "$out/.config"
grep -qx 'CONFIG_EXT4_FAULT_INJECTION=y' "$out/.config"
make -C "$source" O="$out" ARCH=x86_64 CC="$cc" HOSTCC="$cc" \
	-j"${EXT4_CT_JOBS:-$(nproc)}" bzImage
