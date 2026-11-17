#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$script_dir/../../.." && pwd)
image=${1:-$repo_root/experiments/ext4-source-fault-study/results/unlink-mainline-fresh-clean-20261116/seed-mainline.img}
mkdir -p "$(dirname "$image")"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
: > "$tmp/empty"
printf x > "$tmp/one-byte"
truncate -s 64M "$image"
mke2fs -q -F -t ext4 -b 4096 -g 1024 -N 32768 -O orphan_file "$image"
{
	echo 'mkdir /p'
	echo 'mkdir /p/a-fill'
	for i in $(seq 1 2048); do
		printf 'write %s /p/a-fill/f%010d\n' "$tmp/empty" "$i"
	done
	printf 'write %s /p/arm\n' "$tmp/one-byte"
	printf 'write %s /p/file\n' "$tmp/one-byte"
} > "$tmp/debugfs.cmd"
debugfs -w "$image" < "$tmp/debugfs.cmd" >/dev/null 2>&1
e2fsck -fn "$image"
