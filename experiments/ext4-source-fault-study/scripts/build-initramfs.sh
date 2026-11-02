#!/usr/bin/env bash
set -euo pipefail

study_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
initrd_dir="${study_dir}/initramfs"
output="${1:-${study_dir}/build/source-fault-initramfs.cpio.gz}"
host_cc="${CC:-gcc}"

busybox="${BUSYBOX:-$(command -v busybox || true)}"
if [[ -z "$busybox" ]]; then
	echo 'busybox is required; install busybox-static' >&2
	exit 1
fi

mkdir -p "${initrd_dir}/bin" "${initrd_dir}/dev" "${initrd_dir}/proc" \
	"${initrd_dir}/sys" "${initrd_dir}/mnt" "$(dirname "${output}")"

install -m 0755 "$busybox" "${initrd_dir}/bin/busybox"
for applet in cat echo ln mkdir mount mv poweroff rm sh sync touch umount; do
	ln -sf busybox "${initrd_dir}/bin/${applet}"
done

"${host_cc}" -O2 -Wall -Wextra -Werror -static \
	-o "${initrd_dir}/bin/fault-workload" \
	"${study_dir}/source-fault-workload.c"

(
	cd "${initrd_dir}"
	find . -print0 | cpio --null -o -H newc --quiet | gzip -1
) >"${output}"
printf 'built %s\n' "${output}"
