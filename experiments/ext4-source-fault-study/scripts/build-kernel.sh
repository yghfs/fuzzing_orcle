#!/usr/bin/env bash
set -euo pipefail

study_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="${1:?usage: build-kernel.sh LINUX_SOURCE_DIR OUTPUT_DIR [JOBS]}"
output_dir="${2:?usage: build-kernel.sh LINUX_SOURCE_DIR OUTPUT_DIR [JOBS]}"
jobs="${3:-$(nproc)}"
cc="${CC:-gcc}"
hostcc="${HOSTCC:-${cc}}"

mkdir -p "${output_dir}"
cp "${study_dir}/config-linux-6.12.1-x86_64" "${output_dir}/.config"
make -C "${source_dir}" O="${output_dir}" ARCH=x86_64 \
	CC="${cc}" HOSTCC="${hostcc}" olddefconfig
grep -qx 'CONFIG_EXT4_SOURCE_FAULT_INJECTION=y' "${output_dir}/.config"
grep -qx 'CONFIG_DEBUG_FS=y' "${output_dir}/.config"
make -C "${source_dir}" O="${output_dir}" ARCH=x86_64 \
	CC="${cc}" HOSTCC="${hostcc}" -j"${jobs}" bzImage
printf 'built %s/arch/x86/boot/bzImage\n' "${output_dir}"
