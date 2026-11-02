#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work="$root/work"
creator="$root/tools/src/syzkaller-master/tools/create-image.sh"
mkdir -p "$work"
if [[ -e "$work/debian.img" && -r "$work/debian.id_rsa" ]]; then
	printf 'reusing existing Debian guest image and SSH key in %s\n' "$work"
	exit 0
fi
if [[ ! -e "$work/debian.img" && -r "$work/debian.id_rsa" && -s "$work/debian-clean-smalljournal.img" ]]; then
	ln -s debian-clean-smalljournal.img "$work/debian.img"
	printf 'using the existing small-journal guest image with its matching SSH key in %s\n' "$work"
	exit 0
fi
if [[ -e "$work/debian" || -e "$work/debian.img" || -e "$work/debian.id_rsa" || -e "$work/debian.id_rsa.pub" ]]; then
	echo "existing Debian guest files are incomplete; preserving them and refusing to overwrite anything in $work" >&2
	exit 1
fi
test -x "$creator" || { echo 'install the pinned syzkaller source first' >&2; exit 1; }
command -v debootstrap >/dev/null 2>&1 || { echo 'install tools/install-deps.sh prerequisites first' >&2; exit 1; }
(
	cd "$work"
	"$creator" -d trixie -f full -o debian -s "${EXT4_CT_GUEST_SIZE_MB:-8192}"
)
chmod 0600 "$work/debian.id_rsa"
printf 'guest image: %s\nguest key: %s\n' "$work/debian.img" "$work/debian.id_rsa"
