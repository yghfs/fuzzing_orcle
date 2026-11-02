#!/usr/bin/env bash
set -euo pipefail

study=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
root=$(cd "$study/../.." && pwd)
baseline="$root/experiments/ext4-counter-trace/kernel/linux-6.12.1"
modified="$root/kernel/linux-6.12.1"
output="$study/patches/0001-ext4-invariant-checker.patch"
mkdir -p "$(dirname "$output")"
test -f "$baseline/Makefile" || { echo "verified upstream tree missing: $baseline" >&2; exit 1; }
test -f "$modified/fs/ext4/checker.c" || { echo "checker source missing: $modified" >&2; exit 1; }

mapfile -t paths < <(
	{
		find "$baseline/fs/ext4" "$modified/fs/ext4" -maxdepth 1 -type f -printf 'fs/ext4/%f\n'
		printf '%s\n' include/linux/jbd2.h fs/jbd2/journal.c
	} | sort -u
)
: > "$output"
for path in "${paths[@]}"; do
	[[ "$path" != *.orig ]] || continue
	before="$baseline/$path"
	after="$modified/$path"
	[[ -f "$after" ]] || continue
	if [[ -f "$before" ]] && cmp -s "$before" "$after"; then
		continue
	fi
	old="$before"
	[[ -f "$old" ]] || old=/dev/null
	if diff -u --label "a/$path" --label "b/$path" "$old" "$after" >> "$output"; then
		echo "unchanged source unexpectedly produced an empty diff: $path" >&2
		exit 1
	else
		rc=$?
		[[ $rc == 1 ]] || exit "$rc"
	fi
done
test -s "$output" || { echo 'no source changes exported' >&2; exit 1; }
patch --dry-run --fuzz=0 -s -d "$baseline" -p1 < "$output"
printf 'exported checker patch: %s (%s bytes)\n' "$output" "$(stat -c %s "$output")"
