#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
bash tools/install-deps.sh
bash tools/install-go.sh
bash tools/install-syzkaller.sh
bash experiments/ext4-invariant-checker/scripts/prepare-source.sh
bash experiments/ext4-invariant-checker/scripts/build.sh
bash experiments/ext4-counter-trace/scripts/prepare-dev-source.sh
bash experiments/ext4-counter-trace/scripts/build.sh
bash experiments/ext4-source-fault-study/scripts/prepare-source.sh
bash tools/build-syzkaller.sh
bash tools/create-vm-image.sh
printf 'bootstrap complete. Run bash experiments/ext4-counter-trace/scripts/run-smoke.sh\n'
