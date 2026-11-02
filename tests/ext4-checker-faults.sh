#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-0}" != 0 ]]; then
	echo "Synthetic checker faults 1..4 were removed. Use candidate validation runners." >&2
	exit 2
fi
exec "$(dirname "${BASH_SOURCE[0]}")/ext4-checker-smoke.sh" "${2:-10030}"
