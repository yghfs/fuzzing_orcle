#!/usr/bin/env python3
"""Tests for deterministic fault evidence validation."""
import importlib.util
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "fault_evidence.py"
sys.path.insert(0, str(SCRIPT.parent))
from fault_plan import get_plan
spec = importlib.util.spec_from_file_location("fault_evidence", SCRIPT)
fault_evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fault_evidence)

V2_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "fault_evidence_v2.py"
v2_spec = importlib.util.spec_from_file_location("fault_evidence_v2", V2_SCRIPT)
fault_evidence_v2 = importlib.util.module_from_spec(v2_spec)
v2_spec.loader.exec_module(fault_evidence_v2)

COLLECT_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / \
    "collect-fault-formula-evidence.py"
collect_spec = importlib.util.spec_from_file_location(
    "collect_fault_formula_evidence", COLLECT_SCRIPT)
collect_fault_formula_evidence = importlib.util.module_from_spec(collect_spec)
collect_spec.loader.exec_module(collect_fault_formula_evidence)


class FaultEvidenceTests(unittest.TestCase):
    def make(self, **changes):
        temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(temp.name)
        plan = dict(schema=1, fault_id="mballoc-mark-eio", operation="mballoc_mark",
                    errno=-5, count=1, program="fixture-w0", call_index=3,
                    syscall_nr=285,
                    rollback_fields=["free_clusters", "free_inodes", "dirs", "target_file"],
                    expected_return=dict(result=-1, errno=5))
        arm = dict(status="armed", enabled=True, remaining=1, injected=0,
                   operation="mballoc_mark", errno=-5, errno_choices="-5",
                   errno_choice_index=0, count=1,
                   program="fixture-w0", call_index=3, generation=4, target_pid=1234)
        hit = [dict(operation="mballoc_mark", errno=-5,
                    errno_choice_index=0, errno_choice_draw=0, program="fixture-w0",
                    call_index=3, generation=4, injection_count=1, pid=1234,
                    target_pid=1234, group=0, block=42, len=1, result=-5,
                    trace_line=4)]
        ret = dict(program="fixture-w0", call_index=3, result=-1, errno=5,
                   verified=True, native_verified=True, marker_verified=True,
                   workload_pid=1234, marker_id="fixture-1234-3",
                   marker_begin_line=2, native_enter_line=3, kernel_hit_line=4,
                   native_exit_line=5, marker_end_line=6,
                   native_raw_result=-5, native_syscall_nr=285)
        target_file = dict(size=0, blocks=0, nlink=1, extent_count=0, extent_hash=42)
        stable = dict(valid=True, relations_valid=True, namespace_valid=True,
                      trace_loss=0, workers=1, free_clusters=100,
                      free_inodes=80, dirs=3, target_file=target_file)
        persistence = dict(checked=True, valid=True, matches_stable_after=True,
                           matches_remount=True, fsck_exit=0,
                           target_state_remount=target_file)
        status_before = dict(enabled=True, operation="mballoc_mark", errno=-5,
                             errno_choices="-5", errno_choice_index=0,
                             remaining=1, injected=0, generation=4, target_pid=1234,
                             program="fixture-w0", call_index=3)
        status_after = dict(status_before, remaining=0, injected=1)
        workload = dict(pid=1234, arm_verified=True, call_performed=True,
                        disable_rc=0, status_before=json.dumps(status_before),
                        status_after=json.dumps(status_after), syscall_rc=-1,
                        syscall_errno=5, return_matches_expected=True)
        payload = {"fault-plan.json": plan, "arm.json": arm, "hits.json": hit,
                   "return.json": ret, "stable-before.json": stable,
                   "stable-after.json": stable, "persistence.json": persistence,
                   "remount.json": stable,
                   "workload-result.json": workload,
                   "disk.json": dict(valid=True, status="verified",
                                     descriptor_totals=dict(free_clusters=100,
                                                            free_inodes=80, dirs=3))}
        for name, value in payload.items():
            if name in changes:
                value = changes[name]
            (root / name).write_text(json.dumps(value) + "\n")
        (root / "trace.txt").write_text(
            "worker-1234 [000] ..... 1.000000: tracing_mark_write: "
            "CT_FAULT_ARM phase=ARM_END pid=1234 call_index=3 operation=mballoc_mark "
            "errno=-5 program=fixture-w0 status=armed\n"
            "worker-1234 [000] ..... 1.001000: tracing_mark_write: "
            "CT_SYSCALL phase=BEGIN id=fixture-1234-3 nr=285 name=fallocate "
            "program=fixture-w0 call_index=3 ret=0 errno=0\n"
            "worker-1234 [000] ..... 1.002000: sys_enter: NR 285 (3, 0, 0, 1000, 0, 0)\n"
            "worker-1234 [000] ..... 1.003000: ext4_fault_inject: dev=253,16 "
            "operation=mballoc_mark errno=-5 errno_choice_index=0 errno_choice_draw=0 "
            "generation=4 injection=1 pid=1234 "
            "target_pid=1234 call_index=3 group=0 block=42 len=1 result=-5\n"
            "worker-1234 [000] ..... 1.004000: sys_exit: NR 285 = -5\n"
            "worker-1234 [000] ..... 1.005000: tracing_mark_write: "
            "CT_SYSCALL phase=END id=fixture-1234-3 nr=285 name=fallocate "
            "program=fixture-w0 call_index=3 ret=-1 errno=5\n")
        return temp, root

    def test_complete_closed_loop(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        report = fault_evidence.validate(root)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["classification"], "passed")
        self.assertEqual(report["stages"],
                         {"arm": True, "hit": True, "return": True,
                          "stable": True, "rollback": True, "persistence": True})

    def test_kernel_hit_is_required_and_missing_is_not_clean(self):
        temp, root = self.make(**{"hits.json": []})
        self.addCleanup(temp.cleanup)
        report = fault_evidence.validate(root)
        self.assertFalse(report["passed"])
        self.assertEqual(report["classification"], "not_triggered")
        self.assertIn("hit:count_mismatch", report["issues"])

    def test_return_must_match_injected_errno(self):
        temp, root = self.make(**{"return.json": {"program": "fixture-w0",
                                                   "call_index": 3, "result": 0,
                                                   "errno": 0, "verified": True}})
        self.addCleanup(temp.cleanup)
        report = fault_evidence.validate(root)
        self.assertFalse(report["passed"])
        self.assertEqual(report["classification"], "wrong_return")

    def test_stable_mismatch_is_not_persistent_finding(self):
        temp, root = self.make(**{"stable-after.json": {"valid": False,
                                    "relations_valid": False, "trace_loss": 0}})
        self.addCleanup(temp.cleanup)
        report = fault_evidence.validate(root)
        self.assertFalse(report["passed"])
        self.assertEqual(report["classification"], "unstable")

    def test_valid_stable_state_with_bad_disk_is_persistent_mismatch(self):
        temp, root = self.make(**{"persistence.json": {"checked": True,
                                  "valid": False, "matches_stable_after": False,
                                  "fsck_exit": 4}})
        self.addCleanup(temp.cleanup)
        report = fault_evidence.validate(root)
        self.assertFalse(report["passed"])
        self.assertEqual(report["classification"], "persistence_mismatch")

    def test_syscall_events_from_another_pid_cannot_prove_return(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        trace_path = root / "trace.txt"
        trace = trace_path.read_text()
        trace = trace.replace("worker-1234 [000] ..... 1.002000: sys_enter",
                              "worker-9999 [000] ..... 1.002000: sys_enter")
        trace_path.write_text(trace)
        report = fault_evidence.validate(root)
        self.assertFalse(report["passed"])
        self.assertEqual(report["classification"], "not_triggered")
        self.assertIn("return:native_syscall_pair_count_mismatch", report["issues"])

    def test_stable_but_changed_state_is_rollback_mismatch(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        stable_after = json.loads((root / "stable-after.json").read_text())
        stable_after["free_clusters"] -= 1
        (root / "stable-after.json").write_text(json.dumps(stable_after) + "\n")
        report = fault_evidence.validate(root)
        self.assertFalse(report["passed"])
        self.assertEqual(report["classification"], "rollback_mismatch")
        self.assertIn("rollback:free_clusters_changed", report["issues"])

    def test_trace_markers_are_checked_when_trace_is_saved(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        (root / "trace.txt").write_text(
            "worker-1234 [000] ..... 1.000000: tracing_mark_write: "
            "CT_FAULT_ARM phase=ARM_END pid=1234 call_index=3 operation=mballoc_mark "
            "errno=-5 program=fixture-w0 status=armed\n"
            "worker-1234 [000] ..... 1.001000: tracing_mark_write: "
            "CT_SYSCALL phase=BEGIN id=fixture-1234-3 nr=285 name=fallocate "
            "program=fixture-w0 call_index=3 ret=0 errno=0\n"
            "worker-1234 [000] ..... 1.002000: sys_enter: NR 285 (3, 0, 0, 1000, 0, 0)\n"
            "worker-1234 [000] ..... 1.003000: ext4_fault_inject: dev=253,16 "
            "operation=mballoc_mark errno=-5 errno_choice_index=0 errno_choice_draw=0 "
            "generation=4 injection=1 pid=1234 "
            "target_pid=1234 call_index=3 group=0 block=42 len=1 result=-5\n"
            "worker-1234 [000] ..... 1.004000: sys_exit: NR 285 = -5\n"
            "worker-1234 [000] ..... 1.005000: tracing_mark_write: "
            "CT_SYSCALL phase=END id=fixture-1234-3 nr=285 name=fallocate "
            "program=fixture-w0 call_index=3 ret=-1 errno=5\n")
        report = fault_evidence.validate(root)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["trace_markers"], {"arm": 1, "hit": 1, "return": 1})

    def test_targeted_hit_rejects_a_different_errno_choice(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        trace_path = root / "trace.txt"
        trace_path.write_text(trace_path.read_text().replace(
            "errno_choice_index=0 errno_choice_draw=0",
            "errno_choice_index=1 errno_choice_draw=0"))
        report = fault_evidence.validate(root)
        self.assertFalse(report["passed"])
        self.assertIn("hit:trace_errno_choice_mismatch", report["issues"])


class InlineFormulaTests(unittest.TestCase):
    def target(self, **changes):
        state = {
            "exists": True,
            "stat_ok": True,
            "inline_data_valid": True,
            "inline_data": True,
            "block0_mapped": False,
            "block0_initialized": False,
            "block0_flags": 0,
        }
        state.update(changes)
        return state

    def test_inline_representation_satisfies_formula(self):
        result = collect_fault_formula_evidence.inline_formula_observation(
            self.target())
        self.assertEqual(result["oracle_state"], "consistent")
        self.assertTrue(result["comparison_valid"])
        self.assertEqual(result["actual"], 1)

    def test_initialized_mapped_block_also_satisfies_formula(self):
        result = collect_fault_formula_evidence.inline_formula_observation(
            self.target(inline_data=False, block0_mapped=True,
                        block0_initialized=True))
        self.assertEqual(result["oracle_state"], "consistent")
        self.assertTrue(result["comparison_valid"])

    def test_missing_representation_is_a_reproduced_mismatch(self):
        result = collect_fault_formula_evidence.inline_formula_observation(
            self.target(inline_data=False))
        self.assertEqual(result["oracle_state"], "mismatch_reproduced")
        self.assertTrue(result["comparison_valid"])
        self.assertEqual(result["actual"], 0)

    def test_unreadable_mapping_is_unverified(self):
        result = collect_fault_formula_evidence.inline_formula_observation(
            self.target(inline_data=False, inline_data_valid=False))
        self.assertEqual(result["oracle_state"], "unverified")
        self.assertFalse(result["comparison_valid"])

    def test_validator_rechecks_inline_event_against_raw_anchor(self):
        target_before = self.target()
        target_after = self.target(inline_data=False, block0_mapped=True,
                                   block0_initialized=True, block0_flags=1)
        observation = fault_evidence_v2.inline_formula_observation(target_after)
        event = {
            "formula": "R-INLINE",
            "point": "FAULT.INLINE.DATA.BLOCK.WRITE_BEGIN",
            "oracle_state": observation["oracle_state"],
            "status": 1,
            "comparison_valid": True,
            "actual": 1,
            "expected": 1,
            "reason": observation["reason"],
            "pid": 123,
            "trace_line": 4,
            "evidence_kind": "fixture_inline_formula",
            "observation": target_after,
        }
        trace = [{"line": 3, "pid": 123, "body": "ext4_fault_inject:",
                  "fields": {}},
                 {"line": 4, "pid": 123,
                  "body": "ext4_counter_event:",
                  "fields": {"formula": "R-INLINE",
                             "point": "FAULT.INLINE.DATA.BLOCK.WRITE_BEGIN",
                             "status": "3",
                             "reason": "block_write_begin_return"}}]
        issues = []
        valid = fault_evidence_v2.validate_inline_formula_event(
            event, {"target_before": target_before,
                    "target_after": target_after}, trace, 123, 3, issues)
        self.assertTrue(valid, issues)
        self.assertEqual(issues, [])


class FaultEvidenceV2Tests(unittest.TestCase):
    def make(self, replacements=None):
        temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(temp.name)
        plan = {
            "schema": 2, "fault_id": "inode-evict-final-dirty-efscorrupted",
            "operation": "inode_evict_final_dirty", "action": "evict-close",
            "syscall_nr": 3, "count": 1, "return_policy": "exact_success",
            "expected_return": {"result": 0, "errno": 0},
            "formulas": ["R-IEVICT", "R-IDELTA-EVICT"],
            "oracle": "final_eviction_bit_release",
            "expected_oracle_state": "mismatch_reproduced",
            "expected_formula_states": {"R-IEVICT": "mismatch_reproduced",
                                         "R-IDELTA-EVICT": "unverified"},
            "allowed_unverified_formulas": ["R-IDELTA-EVICT"],
            "oracle_points": {"R-IEVICT": ["INODE.EVICT.TARGET.EXIT"],
                              "R-IDELTA-EVICT": ["INODE.EVICT.DESCRIPTOR.EXIT",
                                                 "INODE.EVICT.GLOBAL.EXIT"]},
            "expected_namespace_states": {"stable-before": True,
                                           "stable-after": False, "remount": False},
            "expected_objects": {
                "present": {"target_before": True, "target_after": False},
            },
            "program": "fault-formula", "call_index": 0, "errno": -117,
            "recovery": {
                "status": "specified",
                "pre_recovery": {
                    "fsck": {"state": "known_issue", "allowed_exit": [4],
                             "required_signatures": [
                                 "Deleted inode {target_inode} has zero dtime",
                                 "Inode bitmap differences:"]},
                    "disk": {"state": "verified"},
                    "remount_counters": {"state": "must_match"}},
                "repair": {"kind": "e2fsck-fy", "allowed_exit": [1],
                           "required_signatures": ["FILE SYSTEM WAS MODIFIED"]},
                "post_recovery": {"fsck": "clean", "disk": "verified"},
                "allowed_delta": {"free_clusters": 0, "free_inodes": 1,
                                  "dirs": 0}},
        }
        arm = {"schema": 2, "status": "armed", "enabled": True,
               "operation": plan["operation"], "errno": -117,
               "errno_choices": "-117", "errno_choice_index": 0, "count": 1,
               "program": "fault-formula", "call_index": 0, "generation": 2,
               "target_pid": 262, "remaining": 1, "injected": 0}
        target_file = {"exists": True, "stat_ok": True, "ino": 14, "size": 1048576,
                       "blocks": 2048, "nlink": 1, "extent_count": 1,
                       "extent_hash": 99}
        status_before = {"enabled": True, "operation": plan["operation"], "errno": -117,
                         "errno_choices": "-117", "errno_choice_index": 0,
                         "remaining": 1, "injected": 0, "generation": 2,
                         "target_pid": 262, "program": "fault-formula", "call_index": 0,
                         "matches": 0}
        status_after = dict(status_before, remaining=0, injected=1, matches=1)
        workload = {"schema": 2, "pid": 262, "arm_verified": True,
                    "call_performed": True, "disable_rc": 0,
                    "status_before": json.dumps(status_before),
                    "status_after": status_after, "syscall_result": 0,
                    "syscall_errno": 0, "syscall_rc": 0,
                    "return_verified": True, "before_capture_ok": True,
                    "after_capture_ok": True, "target_before": target_file,
                    "target_after": {"exists": False, "stat_ok": False}}
        hit = {"operation": plan["operation"], "generation": 2,
               "injection_count": 1, "pid": 262, "target_pid": 262,
               "program": "fault-formula", "target_program": "fault-formula",
               "call_index": 0, "match_index": 0,
               "errno": -117, "errno_choice_index": 0, "errno_choice_draw": 0,
               "result": -117,
               "group": 0, "block": 14, "len": 1, "trace_line": 4}
        events = [
            {"formula": "R-IEVICT", "point": "INODE.EVICT.TARGET.EXIT",
             "oracle_state": "mismatch_reproduced", "oracle_relevant": True,
             "actual": 1, "expected": 0, "status": 2, "comparison_valid": True,
             "pid": 262, "trace_line": 5},
            {"formula": "R-IDELTA-EVICT", "point": "INODE.EVICT.DESCRIPTOR.EXIT",
             "oracle_state": "unverified", "oracle_relevant": True,
             "actual": 0, "expected": 1, "status": 3, "comparison_valid": False,
             "pid": 262, "trace_line": 6},
            {"formula": "R-IDELTA-EVICT", "point": "INODE.EVICT.GLOBAL.EXIT",
             "oracle_state": "unverified", "oracle_relevant": True,
             "actual": 0, "expected": 1, "status": 3, "comparison_valid": False,
             "pid": 262, "trace_line": 7},
        ]
        stable = {"valid": True, "reason": "ok", "groups": 1, "scanned": 1,
                  "observed_mismatches": 0,
                  "relations_valid": True, "relations_reason": "ok",
                  "relations_mismatches": 0,
                  "namespace_valid": True, "namespace_reason": "ok",
                  "namespace_compared": 1, "namespace_mismatched": 0,
                  "namespace_skipped_dir_nlink": 0,
                  "namespace_skipped_inline": 0, "namespace_skipped_ea": 0,
                  "namespace_skipped_reserved": 0,
                  "namespace_skipped_nlink0": 0, "xattr_blocks": 1,
                  "orphan_slots": 1, "version": 12, "snapshot_id": 10,
                  "observation_stable": True,
                  "trace_loss": 0, "workers": 1,
                  "free_clusters": 100, "free_inodes": 10, "dirs": 3}
        after = dict(stable, namespace_valid=False, namespace_reason="mismatch")
        remount = dict(after, snapshot_id=20)
        disk_common = {
            "valid": True, "status": "verified", "bitmap_descriptor_match": True,
            "superblock_descriptor_match": True, "unverified": [],
            "r_link": {"valid": True, "unverified": []},
            "r_ug": {"valid": True, "unverified": []},
            "r_itable_highwater": {"valid": True, "unverified": []},
            "r_inode_lifecycle": {"valid": True, "unverified": []},
            "superblock": {"free_clusters": 100, "free_inodes": 10},
        }
        disk_before = dict(disk_common, descriptor_totals={
            "free_clusters": 100, "free_inodes": 10, "dirs": 3})
        disk_after = dict(disk_common, descriptor_totals={
            "free_clusters": 100, "free_inodes": 11, "dirs": 3},
            superblock={"free_clusters": 100, "free_inodes": 11})
        persistence = {"checked": True, "valid": True, "matches_remount": True,
                       "fsck_exit": 0, "disk_counters": {
                           "free_clusters": 100, "free_inodes": 11, "dirs": 3}}
        run_meta = {"guest_exit": 0, "pre_recovery_fsck_exit": 4,
                    "pre_recovery_disk_exit": 0, "recovery_exit": 1,
                    "fsck_exit": 0, "disk_exit": 0}
        run_meta.update({"status_before": {"enabled": True,
                                             "operation": plan["operation"],
                                             "errno": -117, "errno_choices": "-117",
                                             "errno_choice_index": 0, "remaining": 1,
                                             "injected": 0, "generation": 2,
                                             "target_pid": 262,
                                             "program": "fault-formula", "call_index": 0,
                                             "matches": 0},
                        "status_after": status_after, "target_inode": 14,
                        "trace_loss": 0, "workers": 1})
        payload = {
            "fault-plan.json": plan, "arm.json": arm, "workload-result.json": workload,
            "hits.json": [hit], "formula-events.json": events,
            "stable-before.json": stable, "stable-after.json": after,
            "remount.json": remount, "persistence.json": persistence,
            "run-meta.json": run_meta, "disk-before-recovery.json": disk_before,
            "disk.json": disk_after,
        }
        replacements = replacements or {}
        for name, value in payload.items():
            (root / name).write_text(json.dumps(replacements.get(name, value)) + "\n")
        snapshot_semantic_fields = fault_evidence_v2.SNAPSHOT_SEMANTIC_FIELDS
        for pair_id, (name, value) in enumerate((
                ("stable-before.json", stable),
                ("stable-after.json", after),
                ("remount.json", remount)), start=10):
            raw = {field: value.get(field) for field in snapshot_semantic_fields}
            raw["snapshot_id"] = pair_id * 2
            raw_second = dict(raw, snapshot_id=pair_id * 2 + 1)
            (root / f"{name}.a.json").write_text(json.dumps(raw) + "\n")
            (root / f"{name}.b.json").write_text(json.dumps(raw_second) + "\n")
        oracle = dict(after, snapshot_id=24)
        oracle_first = {field: oracle.get(field) for field in snapshot_semantic_fields}
        oracle_first["snapshot_id"] = 24
        oracle_second = dict(oracle_first, snapshot_id=25)
        (root / "stable-oracle.json").write_text(json.dumps(oracle) + "\n")
        (root / "stable-oracle.json.a.json").write_text(json.dumps(oracle_first) + "\n")
        (root / "stable-oracle.json.b.json").write_text(json.dumps(oracle_second) + "\n")
        (root / "persistence.json").write_text(json.dumps({
            "checked": True, "valid": True, "matches_remount": True,
            "fsck_exit": 0, "pre_recovery_fsck_exit": 4,
            "pre_recovery_disk_exit": 0, "recovery_exit": 1, "disk_exit": 0,
            "expected_pre_recovery_inode_leak": True,
            "disk_counters": {"free_clusters": 100, "free_inodes": 11, "dirs": 3},
        }) + "\n")
        (root / "trace.txt").write_text(
            "fault-formula-262 [000] ..... 1.000000: tracing_mark_write: "
            "CT_FAULT_ARM phase=ARM_END pid=262 call_index=0 "
            "operation=inode_evict_final_dirty errno=-117 "
            "program=fault-formula status=armed\n"
            "fault-formula-262 [000] ..... 1.001000: tracing_mark_write: "
            "CT_SYSCALL phase=BEGIN id=fault-262-0 nr=3 name=close "
            "program=fault-formula call_index=0 action=evict-close target_fd=3\n"
            "fault-formula-262 [000] ..... 1.002000: sys_enter: NR 3 (3, 0, 0, 0, 0, 0)\n"
            "fault-formula-262 [000] ..... 1.003000: ext4_fault_inject: dev=253,16 "
            "operation=inode_evict_final_dirty errno=-117 errno_choice_index=0 "
            "errno_choice_draw=0 generation=2 injection=1 "
            "pid=262 target_pid=262 program=fault-formula "
            "target_program=fault-formula call_index=0 match_index=0 "
            "group=0 block=14 len=1 result=-117\n"
            "fault-formula-262 [000] ..... 1.003100: ext4_counter_event: dev=253,16 "
            "epoch=1 seq=1 op=1 parent=0 point=INODE.EVICT.TARGET.EXIT "
            "formula=R-IEVICT ino=14 group=0 tid=262 actual=1 expected=0 "
            "delta=1 update_delta=0 status=2 comparison_valid=1 reason=failed\n"
            "fault-formula-262 [000] ..... 1.003200: ext4_counter_event: dev=253,16 "
            "epoch=1 seq=2 op=1 parent=0 point=INODE.EVICT.DESCRIPTOR.EXIT "
            "formula=R-IDELTA-EVICT ino=14 group=0 tid=262 actual=0 expected=1 "
            "delta=0 update_delta=0 status=3 comparison_valid=0 reason=failed\n"
            "fault-formula-262 [000] ..... 1.003300: ext4_counter_event: dev=253,16 "
            "epoch=1 seq=3 op=1 parent=0 point=INODE.EVICT.GLOBAL.EXIT "
            "formula=R-IDELTA-EVICT ino=14 group=0 tid=262 actual=0 expected=1 "
            "delta=0 update_delta=0 status=3 comparison_valid=0 reason=failed\n"
            "fault-formula-262 [000] ..... 1.004000: sys_exit: NR 3 = 0\n"
            "fault-formula-262 [000] ..... 1.005000: tracing_mark_write: "
            "CT_SYSCALL phase=END id=fault-262-0 nr=3 name=close "
            "program=fault-formula call_index=0 action=evict-close target_fd=3 "
            "ret=0 errno=0\n")
        (root / "fsck-before-recovery.txt").write_text(
            "e2fsck: Deleted inode 14 has zero dtime. Fix? no\n"
            "Inode bitmap differences: -14\n")
        (root / "fsck-recovery.txt").write_text(
            "Deleted inode 14 has zero dtime. Fix? yes\n"
            "FILE SYSTEM WAS MODIFIED\n")
        (root / "fsck.txt").write_text(
            "Pass 1: Checking inodes, blocks, and sizes\n"
            "Pass 5: Checking group summary information\n"
            "Filesystem clean\n")
        (root / "run-status.txt").write_text(
            "workload_rc=0\nafter_snapshot_rc=0\nunmount_rc=0\n"
            "remount_rc=0\nremount_umount_rc=0\ntrace_loss=0\nworkers=1\n")
        (root / "unmount-status.txt").write_text("unmount_ok\n")
        return temp, root

    def test_raw_trace_fsck_and_disk_support_complete_recovery(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        report = fault_evidence_v2.validate(root)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["classification"], "finding_reproduced_recovered")

    def test_contract_schema_rechecks_image_and_association_artifacts(self):
        """Exercise the strict runner contract with all causal artifacts."""
        temp, root = self.make()
        self.addCleanup(temp.cleanup)

        plan_path = root / "fault-plan.json"
        plan = json.loads(plan_path.read_text())
        generated = get_plan("inode-evict-final-dirty").as_json()
        plan.update({
            "contract_schema": 1,
            "hook_contract": generated["hook_contract"],
            "error_semantics": generated["error_semantics"],
        })
        plan["hook_contract"]["syscall_return_policy"] = plan["return_policy"]
        plan["recovery"]["image_policy"] = {
            "primary": "immutable", "repair_target": "copy"}
        plan_path.write_text(json.dumps(plan) + "\n")

        # The evidence directory is normally a child of the run directory.
        # Keep the two images beside it so the validator must resolve and hash
        # the paths recorded by the runner.
        primary = root.parent / "test.raw"
        recovery = root.parent / "recovery.raw"
        primary.write_bytes(b"primary-evidence")
        recovery.write_bytes(b"repaired-evidence")
        primary_hash = hashlib.sha256(primary.read_bytes()).hexdigest()
        recovery_hash = hashlib.sha256(recovery.read_bytes()).hexdigest()
        (root / "image-integrity.json").write_text(json.dumps({
            "schema": 1,
            "repair_target": "recovery.raw",
            "repair_performed": True,
            "primary_image": {
                "path": "../test.raw",
                "sha256_after_guest": primary_hash,
                "sha256_after_checks": primary_hash,
            },
            "recovery_image": {
                "path": "../recovery.raw",
                "sha256_after_guest": primary_hash,
                "sha256_before_repair": primary_hash,
                "sha256_after_repair": recovery_hash,
                "sha256_after_checks": recovery_hash,
            },
        }) + "\n")

        rows = collect_fault_formula_evidence.read_rows(root / "trace.txt")
        workload = json.loads((root / "workload-result.json").read_text())
        hits = [row for row in rows if "ext4_fault_inject:" in row["body"]]
        events = json.loads((root / "formula-events.json").read_text())
        snapshots = {
            name: json.loads((root / name).read_text())
            for name in ("stable-before.json", "stable-after.json",
                         "stable-oracle.json", "remount.json")
        }
        association = collect_fault_formula_evidence.build_association_index(
            plan, workload, rows, hits, events, snapshots)
        (root / "association-index.json").write_text(
            json.dumps(association) + "\n")

        report = fault_evidence_v2.validate(root)
        self.assertTrue(report["passed"], report)
        self.assertTrue(report["plan_valid"])
        self.assertTrue(report["association_valid"])
        self.assertEqual(report["image_integrity"]["repair_target"],
                         "recovery.raw")

    def test_dirty_metadata_error_with_clean_raw_image_is_not_persistent_finding(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        plan_path = root / "fault-plan.json"
        plan = json.loads(plan_path.read_text())
        plan["error_semantics"] = {
            "injected_errno": "EFSCORRUPTED", "handle_abort": True,
            "journal_abort": "error-policy-dependent", "forced_shutdown": False,
            "scope": "jbd2_journal_dirty_metadata_return_-EUCLEAN",
            "ext4_error_handler": "ext4_handle_dirty_metadata_error",
            "persistence_expectation": "source_path_must_be_checked"}
        plan["recovery"] = {
            "status": "specified",
            "pre_recovery": {"fsck": {"state": "clean"},
                             "disk": {"state": "verified"},
                             "remount_counters": {"state": "must_match"}},
            "repair": {"kind": "none", "allowed_exit": [0]},
            "post_recovery": {"fsck": "clean", "disk": "verified"},
            "allowed_delta": {"free_clusters": 0, "free_inodes": 0, "dirs": 0}}
        plan["expected_stable_states"] = {
            "stable-before": {"valid": True, "relations_valid": True,
                              "namespace_valid": True},
            "stable-after": {"valid": False, "relations_valid": False,
                             "namespace_valid": False},
            "remount": {"valid": False, "relations_valid": False,
                        "namespace_valid": False}}
        plan_path.write_text(json.dumps(plan) + "\n")

        for name in ("stable-after.json", "remount.json", "stable-oracle.json"):
            path = root / name
            snapshot = json.loads(path.read_text())
            snapshot.update(valid=False, reason="journal_aborted",
                            relations_valid=False,
                            relations_reason="journal_aborted",
                            namespace_valid=False,
                            namespace_reason="journal_aborted")
            path.write_text(json.dumps(snapshot) + "\n")
            for suffix in ("a", "b"):
                pair_path = root / f"{name}.{suffix}.json"
                pair = json.loads(pair_path.read_text())
                pair.update(valid=False, reason="journal_aborted",
                            relations_valid=False,
                            relations_reason="journal_aborted",
                            namespace_valid=False,
                            namespace_reason="journal_aborted")
                pair_path.write_text(json.dumps(pair) + "\n")

        disk_path = root / "disk.json"
        disk = json.loads(disk_path.read_text())
        disk["descriptor_totals"]["free_inodes"] = 10
        disk["superblock"]["free_inodes"] = 10
        disk_path.write_text(json.dumps(disk) + "\n")
        (root / "fsck-before-recovery.txt").write_text("Filesystem clean\n")
        (root / "fsck-recovery.txt").write_text("Filesystem clean\n")

        meta_path = root / "run-meta.json"
        meta = json.loads(meta_path.read_text())
        meta.update(pre_recovery_fsck_exit=0, recovery_exit=0)
        meta_path.write_text(json.dumps(meta) + "\n")
        persistence_path = root / "persistence.json"
        persistence = json.loads(persistence_path.read_text())
        persistence.update(valid=True, pre_recovery_fsck_exit=0,
                           recovery_exit=0, matches_remount=True,
                           remount_counter_state="must_match",
                           disk_counters={"free_clusters": 100,
                                          "free_inodes": 10, "dirs": 3})
        persistence_path.write_text(json.dumps(persistence) + "\n")

        (root / "fsck-before-recovery.txt").write_text("Filesystem clean\n")
        report = fault_evidence_v2.validate(root)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["classification"],
                         "injected_journal_abort_no_persistent_damage")
        self.assertTrue(report["runtime_formula_mismatch"])
        self.assertFalse(report["persistent_postcondition_mismatch"])
        self.assertFalse(report["finding_reproduced"])

    def test_inspect_recovery_contract_accepts_clean_image_without_repair(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        plan_path = root / "fault-plan.json"
        plan = json.loads(plan_path.read_text())
        plan["recovery"] = {
            "status": "specified",
            "pre_recovery": {
                "fsck": {"state": "inspect", "allowed_exit": [0, 1, 2, 4, 8]},
                "disk": {"state": "inspect"},
                "remount_counters": {"state": "must_match"}},
            "repair": {"kind": "e2fsck-fy", "allowed_exit": [0, 1]},
            "post_recovery": {"fsck": "clean", "disk": "verified"},
            "allowed_delta": {"free_clusters": 0, "free_inodes": 1,
                              "dirs": 0}}
        plan_path.write_text(json.dumps(plan) + "\n")

        clean_text = ("Pass 1: Checking inodes, blocks, and sizes\n"
                      "Pass 5: Checking group summary information\n"
                      "Filesystem clean\n")
        (root / "fsck-before-recovery.txt").write_text(clean_text)
        (root / "fsck-recovery.txt").write_text(clean_text)
        run_meta_path = root / "run-meta.json"
        run_meta = json.loads(run_meta_path.read_text())
        run_meta.update(pre_recovery_fsck_exit=0, pre_recovery_disk_exit=0,
                        recovery_exit=0)
        run_meta_path.write_text(json.dumps(run_meta) + "\n")
        persistence_path = root / "persistence.json"
        persistence = json.loads(persistence_path.read_text())
        persistence.update(pre_recovery_fsck_exit=0, pre_recovery_disk_exit=0,
                           recovery_exit=0)
        persistence_path.write_text(json.dumps(persistence) + "\n")

        report = fault_evidence_v2.validate(root)
        self.assertTrue(report["passed"], report)
        self.assertTrue(report["stages"]["persistence_checked"])

    def test_native_negative_errno_matches_libc_syscall_failure(self):
        self.assertTrue(fault_evidence_v2.native_syscall_result_matches(-5, -1, 5))
        self.assertTrue(fault_evidence_v2.native_syscall_result_matches(0, 0, 0))
        self.assertFalse(fault_evidence_v2.native_syscall_result_matches(-5, -1, 4))

    def test_capture_only_contract_records_partial_post_hit_capture(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        plan_path = root / "fault-plan.json"
        plan = json.loads(plan_path.read_text())
        plan["expected_objects"] = {"capture_only": True, "present": {}}
        plan_path.write_text(json.dumps(plan) + "\n")
        workload_path = root / "workload-result.json"
        workload = json.loads(workload_path.read_text())
        workload["after_capture_ok"] = False
        workload_path.write_text(json.dumps(workload) + "\n")

        report = fault_evidence_v2.validate(root)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["object_capture"], {
            "before_complete": True,
            "after_complete": False,
            "after_required": False,
        })

    def test_explicit_object_contract_still_requires_post_hit_capture(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        workload_path = root / "workload-result.json"
        workload = json.loads(workload_path.read_text())
        workload["after_capture_ok"] = False
        workload_path.write_text(json.dumps(workload) + "\n")

        report = fault_evidence_v2.validate(root)
        self.assertFalse(report["passed"])
        self.assertIn("objects:state_capture_failed", report["issues"])

    def test_stable_oracle_raw_pair_is_checked_using_json_filename(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        report = fault_evidence_v2.validate(root)
        self.assertFalse(any(issue.startswith("stable:stable-oracle") and
                             "raw_pair_missing" in issue
                             for issue in report["issues"]))

    def test_explicit_null_stable_expectations_are_unspecified(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        plan_path = root / "fault-plan.json"
        plan = json.loads(plan_path.read_text())
        plan["expected_stable_states"] = {
            "stable-before": {"valid": True, "relations_valid": True,
                              "namespace_valid": True},
            "stable-after": {"valid": None, "relations_valid": None,
                             "namespace_valid": None},
            "remount": {"valid": True, "relations_valid": True,
                        "namespace_valid": False},
        }
        # A legacy namespace expectation also exists. An explicit null in the
        # more specific stable contract must suppress that fallback.
        plan["expected_namespace_states"] = {
            "stable-before": True, "stable-after": False, "remount": False}
        plan_path.write_text(json.dumps(plan) + "\n")

        # The stable-after snapshot is structurally stable, but its result
        # fields intentionally differ from the generic true defaults.
        after_path = root / "stable-after.json"
        after = json.loads(after_path.read_text())
        after.update(valid=False, relations_valid=False, namespace_valid=True)
        after_path.write_text(json.dumps(after) + "\n")
        for suffix in ("a", "b"):
            raw_path = root / f"stable-after.json.{suffix}.json"
            raw = json.loads(raw_path.read_text())
            raw.update(valid=False, relations_valid=False, namespace_valid=True)
            raw_path.write_text(json.dumps(raw) + "\n")

        report = fault_evidence_v2.validate(root)
        self.assertTrue(report["stages"]["stable"], report["issues"])
        self.assertFalse(any(issue.startswith("stable:stable-after:") and
                             issue.endswith("_mismatch")
                             for issue in report["issues"]))

    def test_resize_contract_checks_stage_specific_capacity_delta(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        plan_path = root / "fault-plan.json"
        plan = json.loads(plan_path.read_text())
        plan["expected_resize"] = {"after": {
            "fs_blocks": {"delta": 4096},
            "fs_free_blocks": {"delta": {"min": 3000, "max": 4096}},
            "groups": {"same": True}, "free_clusters": {"delta": 0},
            "free_inodes": {"delta": 0}, "dirs": {"delta": 0}}}
        plan_path.write_text(json.dumps(plan) + "\n")
        workload_path = root / "workload-result.json"
        workload = json.loads(workload_path.read_text())
        workload["resize_before"] = {
            "captured": True, "fs_blocks": 40000, "fs_free_blocks": 30000,
            "groups": 2, "free_clusters": 30000, "free_inodes": 16000,
            "dirs": 3, "version": 10, "snapshot_id": 20}
        workload["resize_after"] = {
            "captured": True, "fs_blocks": 44096, "fs_free_blocks": 34000,
            "groups": 2, "free_clusters": 30000, "free_inodes": 16000,
            "dirs": 3, "version": 11, "snapshot_id": 30}
        workload_path.write_text(json.dumps(workload) + "\n")

        report = fault_evidence_v2.validate(root)
        self.assertTrue(report["passed"], report)

    def test_resize_contract_rejects_unpublished_group_capacity(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        plan_path = root / "fault-plan.json"
        plan = json.loads(plan_path.read_text())
        plan["expected_resize"] = {"after": {
            "fs_blocks": {"delta": 0}, "groups": {"same": True}}}
        plan_path.write_text(json.dumps(plan) + "\n")
        workload_path = root / "workload-result.json"
        workload = json.loads(workload_path.read_text())
        workload["resize_before"] = {
            "captured": True, "fs_blocks": 40000, "fs_free_blocks": 30000,
            "groups": 2, "free_clusters": 30000, "free_inodes": 16000,
            "dirs": 3, "version": 10, "snapshot_id": 20}
        workload["resize_after"] = {
            "captured": True, "fs_blocks": 40000, "fs_free_blocks": 30000,
            "groups": 2, "free_clusters": 30000, "free_inodes": 16000,
            "dirs": 3, "version": 11, "snapshot_id": 30}
        workload_path.write_text(json.dumps(workload) + "\n")

        report = fault_evidence_v2.validate(root)
        self.assertTrue(report["passed"], report)
        workload["resize_after"]["fs_blocks"] = 44096
        workload_path.write_text(json.dumps(workload) + "\n")
        report = fault_evidence_v2.validate(root)
        self.assertFalse(report["passed"])
        self.assertIn("resize:after:fs_blocks_mismatch", report["issues"])

    def test_invalid_remount_counters_are_not_compared_to_pre_recovery_disk(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        plan_path = root / "fault-plan.json"
        plan = json.loads(plan_path.read_text())
        plan["recovery"] = {
            "status": "specified",
            "pre_recovery": {
                "fsck": {"state": "inspect", "allowed_exit": [0, 1, 2, 4, 8]},
                "disk": {"state": "inspect"},
                "remount_counters": {"state": "unverified"}},
            "repair": {"kind": "e2fsck-fy", "allowed_exit": [0, 1]},
            "post_recovery": {"fsck": "clean", "disk": "verified"},
            "allowed_delta": {"free_clusters": 0, "free_inodes": 1,
                              "dirs": 0}}
        plan["expected_stable_states"] = {
            "stable-before": {"valid": True, "relations_valid": True,
                              "namespace_valid": True},
            "stable-after": {"valid": True, "relations_valid": True,
                             "namespace_valid": False},
            "remount": {"valid": False, "relations_valid": False,
                        "namespace_valid": False}}
        plan_path.write_text(json.dumps(plan) + "\n")

        remount_path = root / "remount.json"
        remount = json.loads(remount_path.read_text())
        remount.update(valid=False, relations_valid=False, namespace_valid=False,
                       reason="corrupt_or_missing_group", free_inodes=8192)
        remount_path.write_text(json.dumps(remount) + "\n")
        for suffix in ("a", "b"):
            raw_path = root / f"remount.json.{suffix}.json"
            raw = json.loads(raw_path.read_text())
            raw.update(valid=False, relations_valid=False, namespace_valid=False,
                       reason="corrupt_or_missing_group", free_inodes=8192)
            raw_path.write_text(json.dumps(raw) + "\n")

        persistence_path = root / "persistence.json"
        persistence = json.loads(persistence_path.read_text())
        persistence.update(matches_remount=None,
                           remount_counter_state="unverified")
        persistence_path.write_text(json.dumps(persistence) + "\n")

        report = fault_evidence_v2.validate(root)
        self.assertTrue(report["passed"], report)
        self.assertTrue(report["stages"]["remount_counters"])

    def test_invalid_remount_counter_contract_requires_unverified_marker(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        plan_path = root / "fault-plan.json"
        plan = json.loads(plan_path.read_text())
        plan["recovery"] = {
            "status": "specified",
            "pre_recovery": {
                "fsck": {"state": "inspect", "allowed_exit": [0, 1, 2, 4, 8]},
                "disk": {"state": "inspect"},
                "remount_counters": {"state": "unverified"}},
            "repair": {"kind": "e2fsck-fy", "allowed_exit": [0, 1]},
            "post_recovery": {"fsck": "clean", "disk": "verified"},
            "allowed_delta": {"free_clusters": 0, "free_inodes": 1,
                              "dirs": 0}}
        plan_path.write_text(json.dumps(plan) + "\n")
        persistence_path = root / "persistence.json"
        persistence = json.loads(persistence_path.read_text())
        persistence.update(matches_remount=True,
                           remount_counter_state="must_match")
        persistence_path.write_text(json.dumps(persistence) + "\n")

        report = fault_evidence_v2.validate(root)
        self.assertFalse(report["passed"])
        self.assertIn("persistence:remount_counters_should_be_unverified",
                      report["issues"])

    def test_trace_hit_fields_are_checked_against_plan_and_collected_hit(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        trace = (root / "trace.txt").read_text().replace(
            "errno=-117 errno_choice_index=0", "errno=-12 errno_choice_index=0")
        (root / "trace.txt").write_text(trace)
        report = fault_evidence_v2.validate(root)
        self.assertFalse(report["passed"])
        self.assertIn("hit:trace_errno_mismatch", report["issues"])
        self.assertIn("hit:collector_trace_errno_mismatch", report["issues"])

    def test_kernel_program_and_match_index_prove_selected_call(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        hit = json.loads((root / "hits.json").read_text())
        hit[0]["program"] = "other-process"
        hit[0]["match_index"] = 1
        (root / "hits.json").write_text(json.dumps(hit) + "\n")
        report = fault_evidence_v2.validate(root)
        self.assertFalse(report["passed"])
        self.assertIn("hit[0]:program_mismatch", report["issues"])
        self.assertIn("hit[0]:call_index_mismatch", report["issues"])

    def test_stability_is_recomputed_from_archived_snapshot_pair(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        second_path = root / "stable-after.json.b.json"
        second = json.loads(second_path.read_text())
        second["free_inodes"] += 1
        second_path.write_text(json.dumps(second) + "\n")
        report = fault_evidence_v2.validate(root)
        self.assertFalse(report["passed"])
        self.assertIn("stable:stable-after.json:raw_pair_free_inodes_mismatch",
                      report["issues"])

    def test_stable_snapshot_allows_unchanged_version_with_increasing_id(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        first_path = root / "stable-after.json.a.json"
        second_path = root / "stable-after.json.b.json"
        first = json.loads(first_path.read_text())
        second = json.loads(second_path.read_text())
        first.update(snapshot_id=50, version=12)
        second.update(snapshot_id=51, version=12)
        first_path.write_text(json.dumps(first) + "\n")
        second_path.write_text(json.dumps(second) + "\n")
        report = fault_evidence_v2.validate(root)
        self.assertTrue(report["stages"]["stable"], report["issues"])
        self.assertFalse(any("snapshot_id_not_increasing" in issue
                             for issue in report["issues"]))

    def test_stable_snapshot_rejects_unchanged_snapshot_id(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        first_path = root / "stable-after.json.a.json"
        second_path = root / "stable-after.json.b.json"
        first = json.loads(first_path.read_text())
        second = json.loads(second_path.read_text())
        first.update(snapshot_id=50, version=12)
        second.update(snapshot_id=50, version=12)
        first_path.write_text(json.dumps(first) + "\n")
        second_path.write_text(json.dumps(second) + "\n")
        report = fault_evidence_v2.validate(root)
        self.assertFalse(report["stages"]["stable"])
        self.assertIn("stable:stable-after.json:raw_pair_snapshot_id_not_increasing",
                      report["issues"])

    def test_stable_snapshot_rejects_changed_version(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        first_path = root / "stable-after.json.a.json"
        second_path = root / "stable-after.json.b.json"
        first = json.loads(first_path.read_text())
        second = json.loads(second_path.read_text())
        first.update(snapshot_id=50, version=12)
        second.update(snapshot_id=51, version=13)
        first_path.write_text(json.dumps(first) + "\n")
        second_path.write_text(json.dumps(second) + "\n")
        report = fault_evidence_v2.validate(root)
        self.assertFalse(report["stages"]["stable"])
        self.assertIn("stable:stable-after.json:raw_pair_version_mismatch",
                      report["issues"])

    def test_fixture_object_state_is_checked_against_plan(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        workload_path = root / "workload-result.json"
        workload = json.loads(workload_path.read_text())
        workload["target_after"] = {"exists": True, "stat_ok": True, "ino": 14}
        workload_path.write_text(json.dumps(workload) + "\n")
        report = fault_evidence_v2.validate(root)
        self.assertFalse(report["passed"])
        self.assertIn("objects:target_after:presence_mismatch", report["issues"])

    def make_object_transition(self, after_changes=None):
        temp, root = self.make()
        plan_path = root / "fault-plan.json"
        plan = json.loads(plan_path.read_text())
        plan["expected_objects"] = {
            "present": {"target_before": True, "target_after": True},
            "state_transitions": [{
                "before": "target_before",
                "after": "target_after",
                "unchanged_fields": [
                    "exists", "stat_ok", "size", "ino", "nlink",
                    "extent_count", "extent_hash", "xattr_present",
                    "xattr_size", "xattr_hash",
                ],
                "delta_fields": {"blocks": 8},
            }],
        }
        plan_path.write_text(json.dumps(plan) + "\n")
        workload_path = root / "workload-result.json"
        workload = json.loads(workload_path.read_text())
        before = dict(workload["target_before"])
        before.update(xattr_present=False, xattr_size=0, xattr_hash=0)
        after = dict(before, blocks=before["blocks"] + 8)
        after.update(after_changes or {})
        workload["target_before"] = before
        workload["target_after"] = after
        workload_path.write_text(json.dumps(workload) + "\n")
        return temp, root

    def test_object_transition_accepts_exact_eight_sector_metadata_delta(self):
        temp, root = self.make_object_transition()
        self.addCleanup(temp.cleanup)
        report = fault_evidence_v2.validate(root)
        self.assertTrue(report["passed"], report)

    def test_object_transition_rejects_wrong_sector_delta_and_mapping_change(self):
        for changes, expected_issue in (
                ({"blocks": 9}, "objects:target_before:target_after:blocks_delta_mismatch"),
                ({"extent_hash": 43}, "objects:target_before:target_after:unchanged_field_changed")):
            with self.subTest(changes=changes):
                temp, root = self.make_object_transition(
                    {"blocks": 9} if changes.get("blocks") else changes)
                try:
                    report = fault_evidence_v2.validate(root)
                    self.assertFalse(report["passed"])
                    self.assertIn(expected_issue, report["issues"])
                finally:
                    temp.cleanup()

    def test_collector_persistence_flags_cannot_override_bad_raw_disk_json(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        disk = json.loads((root / "disk.json").read_text())
        disk["r_link"]["valid"] = False
        (root / "disk.json").write_text(json.dumps(disk) + "\n")
        report = fault_evidence_v2.validate(root)
        self.assertFalse(report["passed"])
        self.assertIn("persistence:post_recovery_disk_not_independently_verified",
                      report["issues"])

    def test_collector_persistence_flags_cannot_override_dirty_final_fsck(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        (root / "fsck.txt").write_text("Filesystem still has errors\n")
        report = fault_evidence_v2.validate(root)
        self.assertFalse(report["passed"])
        self.assertIn("persistence:final_fsck_not_clean", report["issues"])

    def test_collector_exit_codes_must_match_raw_run_metadata(self):
        temp, root = self.make()
        self.addCleanup(temp.cleanup)
        persistence = json.loads((root / "persistence.json").read_text())
        persistence["recovery_exit"] = 0
        (root / "persistence.json").write_text(json.dumps(persistence) + "\n")
        report = fault_evidence_v2.validate(root)
        self.assertFalse(report["passed"])
        self.assertIn("persistence:collector_exit_codes_disagree_with_run_meta",
                      report["issues"])


if __name__ == "__main__":
    unittest.main()
