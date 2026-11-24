#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work="$root/work"
creator="$root/tools/src/syzkaller-master/tools/create-image.sh"
mkdir -p "$work"
if [[ -e "$work/debian.img" && -r "$work/debian.id_rsa" ]]; then
	printf 'reusing existing Debian guest image and SSH key in %s\n' "$work"
elif [[ ! -e "$work/debian.img" && -r "$work/debian.id_rsa" && -s "$work/debian-clean-smalljournal.img" ]]; then
	ln -s debian-clean-smalljournal.img "$work/debian.img"
	printf 'using the existing small-journal guest image with its matching SSH key in %s\n' "$work"
elif [[ -e "$work/debian" || -e "$work/debian.img" || -e "$work/debian.id_rsa" || -e "$work/debian.id_rsa.pub" ]]; then
	echo "existing Debian guest files are incomplete; preserving them and refusing to overwrite anything in $work" >&2
	exit 1
else
	test -x "$creator" || { echo 'install the pinned syzkaller source first' >&2; exit 1; }
	command -v debootstrap >/dev/null 2>&1 || { echo 'install tools/install-deps.sh prerequisites first' >&2; exit 1; }
	(
		cd "$work"
		"$creator" -d trixie -f full -o debian -s "${EXT4_CT_GUEST_SIZE_MB:-8192}"
	)
	chmod 0600 "$work/debian.id_rsa"
fi

normal="$work/debian-normal.img"
if [[ ! -s "$normal" ]]; then
	# The merged kernel can reject orphan-file layouts on small guest root disks.
	temporary=$(mktemp "$work/debian-normal-XXXXXXXX.img")
	trap 'rm -f "$temporary"' EXIT
	cp --reflink=auto --sparse=always "$work/debian.img" "$temporary"
	features=$(LC_ALL=C dumpe2fs -h "$temporary" 2>/dev/null)
	if rg -q '^Filesystem features:.*\borphan_file\b' <<< "$features"; then
		tune2fs -O ^orphan_file "$temporary"
	fi
	e2fsck -fn "$temporary"
	ln "$temporary" "$normal"
fi
printf 'normal guest image: %s\nguest key: %s\n' "$normal" "$work/debian.id_rsa"
