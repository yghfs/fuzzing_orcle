#!/usr/bin/env bash
# Usage: bash assert-ext4-count-model.sh REPORT MODEL control|violation [verified]
set -euo pipefail
report="$1"
model="$2"
expect="$3"
verified="${4:-any}"
awk -F= -v model="${model}" -v expect="${expect}" \
	-v verified="${verified}" '
  $1 == "model_" model "_checks" { checks = $2; have_checks = 1 }
  $1 == "model_" model "_violations" { violations = $2; have_violations = 1 }
  $1 == "model_" model "_unverified" { unverified = $2; have_unverified = 1 }
  $1 == "violations_total" { total = $2; have_total = 1 }
  END {
    if (!have_checks || !have_violations || !have_total || checks < 1)
      exit 1;
    if (verified == "yes" && (!have_unverified || unverified != 0)) exit 1;
    if (expect == "control") {
      if (violations != 0 || total != 0) exit 1;
    } else if (expect == "violation") {
      if (violations < 1) exit 1;
    } else exit 2;
  }
' "${report}" || {
  echo "count model assertion failed: model=${model} expected=${expect} verified=${verified} report=${report}" >&2
  exit 1
}
echo "count model assertion passed: model=${model} expected=${expect} report=${report}"
