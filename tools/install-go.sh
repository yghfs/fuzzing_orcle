#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
go=${HOST_GO:-$(command -v go || true)}
if [[ -z "$go" ]]; then
	echo 'install golang-go before installing the pinned Go toolchain' >&2
	exit 1
fi
bin="$root/tools/go126/bin"
mkdir -p "$bin"
GOTOOLCHAIN=auto GOBIN="$bin" "$go" install golang.org/dl/go1.26.0@latest
"$bin/go1.26.0" download
"$bin/go1.26.0" version
