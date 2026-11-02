#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source_dir="$root/tools/src/syzkaller-master"
go="$root/tools/go126/bin/go1.26.0"
test -x "$go" || { echo 'run tools/install-go.sh first' >&2; exit 1; }
bash "$root/tools/install-syzkaller.sh"
PATH="$(dirname "$go"):$PATH" GOTOOLCHAIN=local \
	make -C "$source_dir" TARGETOS=linux TARGETARCH=amd64 \
	GO="$go" HOSTGO="$go" manager execprog executor
mkdir -p "$root/experiments/ext4-counter-trace/build"
GOFLAGS=-buildvcs=false "$go" -C "$root/experiments/ext4-counter-trace/syzgen" \
	build -mod=readonly \
	-o "$root/experiments/ext4-counter-trace/build/ct-syzgen" .
