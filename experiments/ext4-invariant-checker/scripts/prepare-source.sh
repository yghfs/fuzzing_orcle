#!/usr/bin/env bash
set -euo pipefail

study=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
counter="$study/../ext4-counter-trace"
python3 "$counter/scripts/prepare-source.py"
pristine="$counter/kernel/linux-6.12.1"
source="$study/kernel/linux-6.12.1"
patch="$study/patches/0001-ext4-invariant-checker.patch"

if [[ -d "$source" ]]; then
	if patch --dry-run --reverse --fuzz=0 -s -d "$source" -p1 < "$patch"; then
		printf 'invariant-checker patch already applied: %s\n' "$source"
		exit 0
	fi
	echo "refusing to replace the existing kernel source tree: $source" >&2
	exit 1
fi
patch --dry-run --fuzz=0 -s -d "$pristine" -p1 < "$patch"
mkdir -p "$(dirname "$source")"
cp -al "$pristine" "$source"
patch --batch --fuzz=0 --no-backup-if-mismatch -s -d "$source" -p1 < "$patch"
patch --dry-run --reverse --fuzz=0 -s -d "$source" -p1 < "$patch"
printf 'prepared invariant-checker source: %s\n' "$source"
