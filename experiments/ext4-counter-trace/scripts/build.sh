#!/usr/bin/env bash
set -euo pipefail
project=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
out="$project/build/linux-counter-trace-normal"
case "${1:-}" in
	--normal) shift ;;
	"") ;;
esac
if (( $# )); then
	printf 'Usage: %s [--normal]\n' "$0" >&2
	exit 2
fi
if [[ ! -f "$project/dev/linux-6.12.1/Makefile" ]]; then
	bash "$project/scripts/prepare-dev-source.sh"
fi
cc=${EXT4_CT_CC:-gcc-14}
if ! command -v "$cc" >/dev/null 2>&1; then
	if [[ -n "${EXT4_CT_CC:-}" ]]; then
		printf 'requested kernel compiler not found: %s\n' "$cc" >&2
		exit 1
	fi
	cc=
	for candidate in gcc-13 gcc-12 gcc; do
		if command -v "$candidate" >/dev/null 2>&1; then
			cc=$candidate
			break
		fi
	done
fi
command -v "$cc" >/dev/null 2>&1 || { echo "kernel compiler not found: $cc" >&2; exit 1; }
mkdir -p "$out"
python3 "$project/scripts/source_audit.py"
if [[ ! -e $out/.config ]]; then
  cp "$project/config/counter-trace.config" "$out/.config"
fi
"$project/dev/linux-6.12.1/scripts/config" --file "$out/.config" \
  --enable EXT4_COUNTER_TRACE
make -C "$project/dev/linux-6.12.1" O="$out" CC="$cc" HOSTCC="$cc" olddefconfig
grep -qx 'CONFIG_EXT4_COUNTER_TRACE=y' "$out/.config"
make -C "$project/dev/linux-6.12.1" O="$out" CC="$cc" HOSTCC="$cc" -j"${EXT4_CT_JOBS:-$(nproc)}" bzImage
python3 "$project/scripts/source_audit.py" --bind-build --kernel-obj "$out"
