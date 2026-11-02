#!/usr/bin/env bash

# Source this file from the repository root or from any subdirectory.
_fuzzing_orcle_tools_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export FUZZING_ORCLE_ROOT="$(cd "${_fuzzing_orcle_tools_dir}/.." && pwd)"
export FUZZING_ORCLE_KERNEL="${FUZZING_ORCLE_ROOT}/kernel/linux-6.12.1"
export FUZZING_ORCLE_SYZKALLER="${FUZZING_ORCLE_ROOT}/tools/syzkaller"
export FUZZING_ORCLE_WORKDIR="${FUZZING_ORCLE_ROOT}/work"

if [[ -z "${FUZZING_ORCLE_GO:-}" ]]; then
	if [[ -x "${FUZZING_ORCLE_ROOT}/tools/go126/bin/go1.26.0" ]]; then
		export FUZZING_ORCLE_GO="${FUZZING_ORCLE_ROOT}/tools/go126/bin/go1.26.0"
	elif [[ -x "${FUZZING_ORCLE_ROOT}/tools/go126/go/bin/go" ]]; then
		export FUZZING_ORCLE_GO="${FUZZING_ORCLE_ROOT}/tools/go126/go/bin/go"
	else
		export FUZZING_ORCLE_GO="$(command -v go || true)"
	fi
fi
if [[ -n "${FUZZING_ORCLE_GO}" ]]; then
	export PATH="$(dirname "${FUZZING_ORCLE_GO}"):${FUZZING_ORCLE_SYZKALLER}/bin:${PATH}"
else
	printf 'warning: install the Go toolchain with tools/install-go.sh\n' >&2
fi
export GOTOOLCHAIN=local

if [[ ! -d "${FUZZING_ORCLE_KERNEL}" ]]; then
	printf 'warning: kernel source is missing: %s\n' "${FUZZING_ORCLE_KERNEL}" >&2
fi

if [[ ! -x "${FUZZING_ORCLE_SYZKALLER}/bin/syz-manager" ]]; then
	printf 'warning: syz-manager is missing: %s\n' "${FUZZING_ORCLE_SYZKALLER}/bin/syz-manager" >&2
fi
