#!/usr/bin/env bash
set -euo pipefail
project=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec python3 "$project/scripts/campaign_runner.py" "$@"
