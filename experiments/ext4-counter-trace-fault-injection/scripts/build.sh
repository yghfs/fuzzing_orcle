#!/usr/bin/env bash
set -euo pipefail
project=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
base="$project/../ext4-counter-trace"
source_tree="$project/dev/linux-6.12.1"
out="$project/build/linux-error-site-trace"
cc=${EXT4_EFI_CC:-gcc-14}
command -v "$cc" >/dev/null
if (( $# )); then
  echo "Usage: $0 (configure EXT4_EFI_CC and EXT4_EFI_JOBS through the environment)" >&2
  exit 2
fi
python3 "$project/scripts/instrument-sites.py" --check
python3 "$project/scripts/configure-injection.py" --prepare-source
mkdir -p "$out"
if [[ ! -e "$out/.config" ]]; then
  cp "$base/config/counter-trace.config" "$out/.config"
fi
"$source_tree/scripts/config" --file "$out/.config" \
  --enable EXT4_FS --enable EXT4_COUNTER_TRACE --enable EXT4_ERROR_SITE_TRACE
make -s -C "$source_tree" O="$out" CC="$cc" HOSTCC="$cc" olddefconfig
rg -qx 'CONFIG_EXT4_ERROR_SITE_TRACE=y' "$out/.config"
make -s -C "$source_tree" O="$out" CC="$cc" HOSTCC="$cc" -j"${EXT4_EFI_JOBS:-4}" bzImage
python3 - "$project" "$out" <<'PY'
import hashlib, json, sys
from pathlib import Path
project, out = map(Path, sys.argv[1:])
inputs = {'sites': project/'sites.json', 'patch': project/'patches/0001-reviewed-error-site-trace.patch',
          'injection': project/'injection.json', 'injection_patch': project/'patches/0002-formula-gated-injection.patch',
          'base_patch': project/'../ext4-counter-trace/patches/0001-counter-trace-core-and-probes.patch',
          'source_audit': out/'source-audit.json',
          'config': out/'.config', 'kernel': out/'arch/x86/boot/bzImage'}
binding = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in inputs.items()}
(out/'build-binding.json').write_text(json.dumps(binding, indent=2)+'\n')
print(f'kernel={out}/arch/x86/boot/bzImage')
PY
