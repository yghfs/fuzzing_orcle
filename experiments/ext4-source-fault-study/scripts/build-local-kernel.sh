#!/usr/bin/env bash
set -euo pipefail

study=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
bash "$study/scripts/prepare-source.sh"
jobs=${EXT4_CT_JOBS:-$(nproc)}
cc=${EXT4_CT_CC:-}
if [[ -z "$cc" ]]; then
	for candidate in gcc-14 gcc-13 gcc-12 gcc; do
		if command -v "$candidate" >/dev/null 2>&1; then
			cc=$candidate
			break
		fi
	done
fi
command -v "$cc" >/dev/null 2>&1 || { printf 'no supported kernel compiler found\n' >&2; exit 1; }
CC="$cc" HOSTCC="$cc" bash "$study/scripts/build-kernel.sh" \
	"$study/kernel/linux-6.12.1" \
	"$study/build/linux-source-fault" "$jobs"
