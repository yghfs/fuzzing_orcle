#!/usr/bin/env bash
set -euo pipefail

study=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
counter="$study/../ext4-counter-trace"
pristine="$counter/kernel/linux-6.12.1"
source="$study/kernel/linux-6.12.1"
patch="$study/patches/0001-ext4-source-derived-fault-injection.patch"

if [[ ! -f "$pristine/Makefile" ]]; then
	python3 "$counter/scripts/prepare-source.py"
fi
if [[ -d "$source" ]]; then
	if test -f "$source/fs/ext4/ext4_source_fault.c"; then
		printf 'source-fault patch already applied: %s\n' "$source"
		exit 0
	fi
	printf 'refusing to replace the existing kernel source tree: %s\n' "$source" >&2
	exit 1
fi

if ! patch --dry-run --fuzz=0 -s -d "$pristine" -p1 < "$patch"; then
	printf 'source-fault patch does not match verified Linux 6.12.1\n' >&2
	exit 1
fi
mkdir -p "$(dirname "$source")"
cp -al "$pristine" "$source"
patch --batch --fuzz=0 --no-backup-if-mismatch -s -d "$source" -p1 < "$patch"
patch --dry-run --reverse --fuzz=0 -s -d "$source" -p1 < "$patch"
printf 'prepared source-fault kernel: %s\n' "$source"
