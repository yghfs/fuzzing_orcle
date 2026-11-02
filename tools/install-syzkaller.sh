#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source_dir="$root/tools/src/syzkaller-master"
revision=$(awk '!/^#/ && NF {print; exit}' "$root/tools/syzkaller-revision.txt")

if [[ -e "$source_dir" ]]; then
	if [[ ! -d "$source_dir/.git" ]]; then
		if [[ -f "$source_dir/go.mod" && -f "$source_dir/syz-manager/manager.go" ]]; then
			printf 'using existing local syzkaller source without replacing it: %s\n' "$source_dir"
			exit 0
		fi
		echo "existing syzkaller path is not a usable checkout: $source_dir" >&2
		exit 1
	fi
	current=$(git -C "$source_dir" rev-parse HEAD)
	if [[ "$current" != "$revision" ]]; then
		echo "syzkaller revision mismatch: expected $revision, found $current" >&2
		exit 1
	fi
else
	mkdir -p "$(dirname "$source_dir")"
	git clone --filter=blob:none --no-checkout https://github.com/google/syzkaller.git "$source_dir"
	git -C "$source_dir" fetch --depth=1 origin "$revision"
	git -C "$source_dir" checkout --detach FETCH_HEAD
fi

if [[ -L "$root/tools/syzkaller" ]]; then
	[[ $(readlink "$root/tools/syzkaller") == src/syzkaller-master ]] || {
		echo "unexpected tools/syzkaller symlink; refusing to replace it" >&2
		exit 1
	}
elif [[ -e "$root/tools/syzkaller" ]]; then
	echo "tools/syzkaller exists and is not the managed symlink" >&2
	exit 1
else
	ln -s src/syzkaller-master "$root/tools/syzkaller"
fi
