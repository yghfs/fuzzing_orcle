#!/usr/bin/env python3
"""Validate the deterministic fast-commit inode-claim fault closure."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

from trace_lifecycle import fields, parse_trace


def read_json(path: pathlib.Path, issues: list[str], name: str):
	try:
		return json.loads(path.read_text())
	except (OSError, json.JSONDecodeError) as exc:
		issues.append(f"{name}_missing_or_invalid:{exc}")
		return {}


def stat_tuple(path: pathlib.Path, issues: list[str], name: str):
	try:
		values = tuple(int(value) for value in path.read_text().split())
		if len(values) != 3:
			raise ValueError("expected inode, nlink, size")
		return values
	except (OSError, ValueError) as exc:
		issues.append(f"{name}_missing_or_invalid:{exc}")
		return None


def stable_snapshot(path: pathlib.Path, issues: list[str], name: str):
	value = read_json(path, issues, name)
	for key in ("valid", "relations_valid", "namespace_valid"):
		if value.get(key) is not True:
			issues.append(f"{name}_{key}_false")
	return value


def main(run: pathlib.Path) -> dict:
	issues: list[str] = []
	result = run / "ext4-counter-trace"
	seed_text = (run / "first-boot/guest-evidence/fc-seed.txt").read_text(errors="replace")
	seed = re.search(r"fast_commit_inode_claim_ready fresh_ino=(\d+) size=(\d+)", seed_text)
	if not seed:
		issues.append("pending_fast_commit_inode_seed_missing")
		fresh_ino = None
		fresh_size = None
	else:
		fresh_ino, fresh_size = int(seed.group(1)), int(seed.group(2))
	if "needs_recovery" not in (run / "fc-before-replay.txt").read_text(errors="replace"):
		issues.append("power_cut_pending_recovery_not_proven")
	if "fast_commit_armed" not in (run / "guest-first.txt").read_text(errors="replace"):
		issues.append("first_boot_seed_not_armed")
	if not (result / "guest-done").exists():
		issues.append("second_boot_guest_incomplete")

	mount = read_json(result / "fault-mount.json", issues, "mount_result")
	status = read_json(result / "fault-status-hit.json", issues, "fault_status")
	disabled = read_json(result / "fault-status-disabled.json", issues,
				     "disabled_fault_status")
	target_pid = mount.get("pid")
	if (mount.get("mount_rc") != 0 or mount.get("errno") != 0 or
	    mount.get("comm") != "fc-replay" or
	    mount.get("operation") != "fast_commit_inode_claim"):
		issues.append("mount_prearm_result_invalid")
	if (status.get("enabled") is not True or
	    status.get("operation") != "fast_commit_inode_claim" or
	    status.get("errno") != -5 or status.get("remaining") != 0 or
	    status.get("injected") != 1 or status.get("matches") != 1 or
	    status.get("target_pid") != target_pid or
	    status.get("program") != "fc-replay" or
	    status.get("mount_prearmed") is not True):
		issues.append("one_shot_mount_prearm_status_invalid")
	if disabled.get("enabled") is not False or disabled.get("operation") != "off":
		issues.append("fault_disable_not_verified")

	trace_path = result / "trace.txt"
	try:
		events, lines = parse_trace(trace_path)
	except (OSError, ValueError) as exc:
		events, lines = [], []
		issues.append(f"raw_trace_missing_or_invalid:{exc}")
	prearms = []
	hits = []
	scan_records = []
	replay_records = []
	for line in lines:
		body = line["body"]
		if body.startswith("ext4_fc_replay_scan:"):
			scan_records.append((line, body))
		elif body.startswith("ext4_fault_prearm:"):
			prearms.append((line, fields(body.split("ext4_fault_prearm:", 1)[1])))
		elif body.startswith("ext4_fault_inject:"):
			hits.append((line, fields(body.split("ext4_fault_inject:", 1)[1])))
		elif body.startswith("ext4_fc_replay:"):
			replay_records.append((line, body))
	if len(prearms) != 1:
		issues.append(f"prearm_trace_count:{len(prearms)}")
	if not scan_records:
		issues.append("fast_commit_replay_scan_not_observed")
	if len(hits) != 1:
		issues.append(f"fault_hit_trace_count:{len(hits)}")
	hit = hits[0][1] if hits else {}
	hit_line = hits[0][0]["line"] if hits else -1
	if prearms:
		prearm = prearms[0][1]
		if (prearm.get("operation") != "fast_commit_inode_claim" or
		    int(prearm.get("errno", 0)) != -5 or
		    int(prearm.get("count", 0)) != 1 or
		    int(prearm.get("target_pid", -1)) != target_pid or
		    prearm.get("target_comm") != "fc-replay" or
		    int(prearm.get("call_index", -1)) != 0):
			issues.append("mount_time_prearm_trace_mismatch")
	if hits:
		if (hit.get("operation") != "fast_commit_inode_claim" or
		    int(hit.get("errno", 0)) != -5 or hit.get("result") != -5 or
		    int(hit.get("pid", -1)) != target_pid or
		    int(hit.get("target_pid", -1)) != target_pid or
		    hit.get("program") != "fc-replay" or
		    hit.get("target_program") != "fc-replay" or
		    int(hit.get("match_index", -1)) != 0):
			issues.append("fault_hit_identity_or_errno_mismatch")
		if fresh_ino is not None and hit.get("block") != fresh_ino:
			issues.append("fault_target_inode_differs_from_seed_inode")
	if fresh_ino is not None and not any(
		 re.search(rf"\bino\s+{fresh_ino}\b", body)
		 for _, body in replay_records):
		issues.append("target_inode_fast_commit_record_missing")

	matching = [event for event in events
		    if event.get("ino") == fresh_ino and
		    event.get("point", "").startswith("FAST_COMMIT.INODE.REPLAY")]
	failed_returns = [event for event in events
			  if event.get("point") == "FAST_COMMIT.INODE.REPLAY" and
			  event.get("pid") == target_pid and event.get("result") == -5 and
			  event.get("line", -1) > hit_line]
	failed_op = failed_returns[0].get("op") if failed_returns else None
	failed_mutations = [event for event in events
			    if event.get("pid") == target_pid and
			    event.get("op") == failed_op and event.get("line", -1) > hit_line and
			    event.get("point") in (
				    "FAST_COMMIT.INODE.REPLAY.BITMAP",
				    "FAST_COMMIT.INODE.REPLAY.GLOBAL",
				    "FAST_COMMIT.INODE.REPLAY.DESCRIPTOR")]
	if not failed_returns:
		issues.append("injected_inode_claim_formula_return_not_observed")
	if failed_mutations:
		issues.append("injected_inode_claim_mutated_state_before_bitmap_read")
	bitmap_claims = [event for event in matching
			 if event.get("point") == "FAST_COMMIT.INODE.REPLAY.BITMAP" and
			 event.get("reason") == "replay_inode_claimed" and
			 event.get("update_delta") == -1 and
			 event.get("line", -1) > (failed_returns[0].get("line", -1)
						      if failed_returns else -1)]
	successful_ops = {event.get("op") for event in bitmap_claims}
	successful_returns = [event for event in events
			      if event.get("point") == "FAST_COMMIT.INODE.REPLAY" and
			      event.get("pid") == target_pid and
			      event.get("op") in successful_ops and event.get("result") == 0]
	if not bitmap_claims:
		issues.append("successful_inode_bitmap_claim_after_injection_missing")
	consistent = [event for event in matching
		      if event.get("point") in ("FAST_COMMIT.INODE.REPLAY.DESCRIPTOR",
					       "FAST_COMMIT.INODE.REPLAY.GLOBAL") and
		      event.get("status") == 1 and event.get("comparison_valid") == 1 and
		      event.get("actual") == event.get("expected")]
	if not consistent:
		issues.append("R_IDELTA_valid_comparison_missing")
	if not successful_returns:
		issues.append("successful_inode_claim_return_missing")

	for name in ("fc-verify.txt", "fc-remount-verify.txt"):
		try:
			text = (result / name).read_text(errors="replace")
		except OSError as exc:
			text = ""
			issues.append(f"{name}_missing:{exc}")
		if "fast_commit_inode_claim_verify=ok" not in text:
			issues.append(f"{name}_namespace_or_inode_verification_failed")
	if fresh_size != 4096:
		issues.append("seed_inode_size_invalid")

	stat_after = stat_tuple(result / "fresh-stat-after.txt", issues, "fresh_inode_after")
	stat_remount = stat_tuple(result / "fresh-stat-remount.txt", issues,
				  "fresh_inode_remount")
	for name, value in (("after", stat_after), ("remount", stat_remount)):
		if value and (value[0] != fresh_ino or value[1] != 1 or value[2] != 4096):
			issues.append(f"fresh_inode_{name}_identity_or_link_count_mismatch")

	snapshots = {}
	for name in ("stable-after-a.json", "stable-after-b.json",
		     "remount-a.json", "remount-b.json"):
		snapshots[name] = stable_snapshot(result / name, issues, name.replace(".json", ""))
	for left_name, right_name in (("stable-after-a.json", "stable-after-b.json"),
				      ("remount-a.json", "remount-b.json")):
		left, right = snapshots[left_name], snapshots[right_name]
		for field in ("groups", "free_clusters", "free_inodes", "dirs"):
			if left.get(field) != right.get(field):
				issues.append(f"{left_name}_{right_name}_{field}_unstable")
	for field in ("free_clusters", "free_inodes", "dirs"):
		if snapshots["stable-after-a.json"].get(field) != snapshots["remount-a.json"].get(field):
			issues.append(f"remount_counter_mismatch:{field}")

	disk_evidence = read_json(run / "fc-disk-evidence.json", issues, "disk_evidence")
	disk = read_json(run / "fc-disk.json", issues, "disk_formula")
	if disk_evidence.get("fsck_exit") != 0:
		issues.append("offline_fsck_not_clean")
	if disk_evidence.get("disk_checker_exit") != 0 or disk.get("valid") is not True:
		issues.append("offline_disk_formula_not_valid")
	for name, expected in disk_evidence.get("sha256", {}).items():
		try:
			import hashlib
			h = hashlib.sha256((run / name).read_bytes()).hexdigest()
		except OSError as exc:
			issues.append(f"disk_evidence_file_missing:{name}:{exc}")
			continue
		if h != expected:
			issues.append(f"disk_evidence_hash_mismatch:{name}")
	fsck_text = (run / "fc-fsck.txt").read_text(errors="replace")
	if "FILE SYSTEM WAS MODIFIED" in fsck_text:
		issues.append("offline_fsck_would_modify_image")
	stat_files = sorted(result.glob("cpu*-stats.txt"))
	if not stat_files:
		issues.append("per_cpu_trace_loss_evidence_missing")
	for stat_path in stat_files:
		stat_text = stat_path.read_text(errors="replace")
		for label in ("overrun", "dropped events"):
			match = re.search(rf"(?m)^{re.escape(label)}:\s*(\d+)", stat_text)
			if match and int(match.group(1)) != 0:
				issues.append(f"trace_loss:{stat_path.name}:{label}")

	return {
		"schema": 1,
		"experiment": "fast-commit-inode-claim-eio",
		"passed": not issues,
		"classification": "passed" if not issues else "failed",
		"fault_id": "fast-commit-inode-claim",
		"target_inode": fresh_ino,
		"stages": {
			"arm": len(prearms) == 1 and not any("prearm" in issue for issue in issues),
			"hit": len(hits) == 1 and not any("fault_hit" in issue for issue in issues),
			"return": (mount.get("mount_rc") == 0 and bool(failed_returns) and
			   not failed_mutations and bool(successful_returns)),
			"formula": bool(bitmap_claims and consistent),
			"stable": all(s.get("valid") is True for s in snapshots.values()),
			"persistence": ("fast_commit_inode_claim_verify=ok" in
					((result / "fc-remount-verify.txt").read_text(errors="replace")
					 if (result / "fc-remount-verify.txt").exists() else "")),
			"offline_fsck": disk_evidence.get("fsck_exit") == 0,
			"offline_disk": disk_evidence.get("disk_checker_exit") == 0,
		},
		"fault_hit": hit,
		"fast_commit_scan_records": [body for _, body in scan_records],
		"inode_formula_events": matching,
		"disk_evidence": disk_evidence,
		"issues": issues,
	}


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("run_dir", type=pathlib.Path)
	parser.add_argument("--output", type=pathlib.Path)
	args = parser.parse_args()
	report = main(args.run_dir)
	rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
	if args.output:
		args.output.write_text(rendered)
	print(rendered, end="")
	sys.exit(0 if report["passed"] else 1)
