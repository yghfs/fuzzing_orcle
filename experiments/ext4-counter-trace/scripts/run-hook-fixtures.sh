#!/usr/bin/env bash
set -uo pipefail

project=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
scripts="$project/scripts"
catalog="$project/fault-errno-catalog.json"
hook_filter=
limit=0
start_index=0
port_base=${EXT4_HOOK_FIXTURE_PORT_BASE:-12000}
build_kernel=0
skip_complete=0

usage() {
	cat <<'EOF'
usage: run-hook-fixtures.sh [--hook OPERATION] [--limit N]
	       [--start-index N] [--port-base PORT] [--build-kernel]
       [--skip-complete]

Runs an isolated single-worker VM fixture for each selected catalog hook.
Failed or untriggered hooks are recorded and the batch continues.
EOF
}

while (($#)); do
	case "$1" in
		--hook) (($# >= 2)) || { usage; exit 2; }; hook_filter=$2; shift 2 ;;
		--limit) (($# >= 2)) || { usage; exit 2; }; limit=$2; shift 2 ;;
		--start-index) (($# >= 2)) || { usage; exit 2; }; start_index=$2; shift 2 ;;
		--port-base) (($# >= 2)) || { usage; exit 2; }; port_base=$2; shift 2 ;;
		--build-kernel) build_kernel=1; shift ;;
		--skip-complete) skip_complete=1; shift ;;
		-h|--help) usage; exit 0 ;;
		*) usage; exit 2 ;;
	esac
done

[[ "$limit" =~ ^[0-9]+$ && "$start_index" =~ ^[0-9]+$ &&
   "$port_base" =~ ^[0-9]+$ ]] || { usage; exit 2; }

python3 "$scripts/fault_errno_catalog.py" --check || exit $?
python3 "$scripts/hook_formula_matrix.py" --check || exit $?
python3 "$scripts/source_audit.py" || exit $?
if (( build_kernel )); then
	bash "$scripts/build.sh" || exit $?
fi

operations=$(python3 - "$catalog" "$hook_filter" "$start_index" "$limit" <<'PY'
import json
import sys

catalog = json.load(open(sys.argv[1]))
selected = list(catalog["hooks"])
name = sys.argv[2]
start = int(sys.argv[3])
limit = int(sys.argv[4])
if name:
    if name not in catalog["hooks"]:
        raise SystemExit(f"unknown hook: {name}")
    selected = [name]
else:
    selected = selected[start:]
if limit:
    selected = selected[:limit]
print("\n".join(selected))
PY
) || exit $?

if [[ -z "$operations" ]]; then
	echo 'no hooks selected' >&2
	exit 2
fi

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
# Include the runner PID so disjoint fixture batches can execute concurrently
# without interleaving or overwriting their campaign logs.
log="$project/results/hook-fixture-campaign-$timestamp-$$.log"
mkdir -p "$project/results"
total=$(printf '%s\n' "$operations" | wc -l)
index=0
failed=0
candidate=0
processed=0

while IFS= read -r operation <&3; do
	[[ -n "$operation" ]] || continue
	index=$((index + 1))
	processed=$((processed + 1))
	port=$((port_base + index))
	base="$project/results/hook-fixtures/$operation"
	if (( skip_complete )); then
		complete=$(python3 - "$base" "$operation" <<'PY'
import json
import pathlib
import sys

base = pathlib.Path(sys.argv[1])
operation = sys.argv[2]
for run in sorted(base.glob("vm-*"), key=lambda path: path.name, reverse=True):
    if not run.is_dir():
        continue
    if operation == "fast_commit_inode_claim":
        plan_path = run / "fault-plan.json"
        report_path = run / "validation.json"
        trace_path = run / "ext4-counter-trace/trace.txt"
    else:
        plan_path = run / "evidence/fault-plan.json"
        report_path = run / "evidence/validation.json"
        trace_path = run / "evidence/trace.txt"
    try:
        plan = json.loads(plan_path.read_text())
        report = json.loads(report_path.read_text())
    except (OSError, json.JSONDecodeError):
        continue
    stages = report.get("stages", {})
    if (plan.get("operation") == operation and trace_path.is_file() and
            all(stages.get(stage) is True
                for stage in ("arm", "hit", "return", "stable"))):
        print("yes")
        break
else:
    print("no")
PY
)
		if [[ "$complete" == yes ]]; then
			printf '[%s/%s] HOOK FIXTURE SKIP operation=%s reason=verified_vm_evidence_exists\n' \
				"$index" "$total" "$operation" | tee -a "$log"
			continue
		fi
	fi
	run="$base/vm-$timestamp"
	mkdir -p "$base"
	if [[ -e "$run" ]]; then
		run="$base/vm-${timestamp}-retry-$index"
	fi
	mkdir -p "$run"
	printf '[%s/%s] HOOK FIXTURE START operation=%s port=%s run_dir=%s\n' \
		"$index" "$total" "$operation" "$port" "$run" | tee -a "$log"

	if [[ "$operation" == fast_commit_inode_claim ]]; then
		set +e
		EXT4_FAULT_FC_RUN_DIR="$run" EXT4_FAULT_FC_PORT="$port" \
			bash "$scripts/run-fast-commit-inode-claim.sh" 2>&1 | tee -a "$log"
		rc=${PIPESTATUS[0]}
		set +e
		validation="$run/validation.json"
	else
		set +e
		EXT4_FAULT_RUN_DIR="$run" EXT4_FAULT_PORT="$port" \
			bash "$scripts/run-fault-formula-single-worker.sh" \
				--fault-id "$operation" 2>&1 | tee -a "$log"
		rc=${PIPESTATUS[0]}
		set +e
		validation="$run/evidence/validation.json"
	fi

	if [[ -s "$validation" ]]; then
		readiness=$(python3 - "$validation" "$operation" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1]))
stages = report.get("stages", {})
ready = all(stages.get(stage) is True
            for stage in ("arm", "hit", "return", "stable"))
if not ready:
    print("incomplete")
elif report.get("finding_reproduced") is True or \
        report.get("persistent_postcondition_mismatch") is True or \
        report.get("runtime_formula_mismatch") is True:
    print("candidate")
else:
    print("hit_stable")
PY
)
	else
		readiness=missing
	fi

	if [[ "$readiness" == candidate ]]; then
		candidate=$((candidate + 1))
		printf '[%s/%s] HOOK FIXTURE CANDIDATE operation=%s rc=%s evidence=%s\n' \
			"$index" "$total" "$operation" "$rc" "$validation" | tee -a "$log"
	elif (( rc == 0 )) && [[ "$readiness" == hit_stable ]]; then
		printf '[%s/%s] HOOK FIXTURE COMPLETE operation=%s evidence=%s\n' \
			"$index" "$total" "$operation" "$validation" | tee -a "$log"
	else
		failed=$((failed + 1))
		printf '[%s/%s] HOOK FIXTURE INCOMPLETE operation=%s rc=%s evidence=%s readiness=%s\n' \
			"$index" "$total" "$operation" "$rc" "$validation" "$readiness" |
			tee -a "$log"
	fi
done 3<<< "$operations"

if (( processed != total )); then
	missing=$((total - processed))
	if (( missing < 0 )); then
		missing=0
	fi
	failed=$((failed + missing))
	printf 'HOOK FIXTURE CAMPAIGN INPUT MISMATCH selected=%s processed=%s missing=%s\n' \
		"$total" "$processed" "$missing" >&2
fi

python3 "$scripts/hook_fixture_inventory.py" --write
printf 'HOOK FIXTURE CAMPAIGN END selected=%s processed=%s candidates=%s incomplete=%s log=%s\n' \
	"$total" "$processed" "$candidate" "$failed" "$log"
if (( failed > 0 )); then
	exit 1
fi
exit 0
