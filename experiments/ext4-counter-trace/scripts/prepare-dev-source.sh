#!/usr/bin/env bash
set -euo pipefail

project=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python3 "$project/scripts/prepare-source.py"

pristine="$project/kernel/linux-6.12.1"
development="$project/dev/linux-6.12.1"
patch="$project/patches/0001-counter-trace-core-and-probes.patch"

if [[ -d "$development" ]]; then
	if patch --dry-run --reverse --fuzz=0 -s -d "$development" -p1 < "$patch"; then
		printf 'counter-trace patch already applied: %s\n' "$development"
		exit 0
	fi
	printf 'refusing to replace the existing development source tree: %s\n' "$development" >&2
	exit 1
fi

if ! patch --dry-run --fuzz=0 -s -d "$pristine" -p1 < "$patch"; then
	printf 'counter-trace patch does not match the verified Linux 6.12.1 source\n' >&2
	exit 1
fi
mkdir -p "$(dirname "$development")"
# Hard links save disk space; patch replaces changed files without altering the pristine tree.
cp -al "$pristine" "$development"
patch --batch --fuzz=0 --no-backup-if-mismatch -s -d "$development" -p1 < "$patch"
patch --dry-run --reverse --fuzz=0 -s -d "$development" -p1 < "$patch"
printf 'prepared counter-trace source: %s\n' "$development"
