#!/usr/bin/env bash
set -euo pipefail

# Keep the control invocation identical to the deterministic fault runner:
# same plan, fixture, kernel and image preparation, with fault arming disabled
# inside the guest.  The runner writes control-validation.json and never feeds
# this evidence to the fault validator.
project=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec "$project/scripts/run-fault-formula-single-worker.sh" --control "$@"
