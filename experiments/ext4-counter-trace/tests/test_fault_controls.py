#!/usr/bin/env python3
"""Regression tests for matched controls and blkdebug evidence."""
import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_script(name: str, module_name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


collector = load_script("collect-fault-formula-evidence.py", "control_collector")
control_validator = load_script("validate-fault-control.py", "control_validator")
blkdebug = load_script("run-blkdebug-io-validation.py", "blkdebug_validation")


class ControlEvidenceTests(unittest.TestCase):
    def make_run(self):
        temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(temporary.name)
        run = root / "run"
        evidence = run / "evidence"
        raw = run / "fault-formula"
        evidence.mkdir(parents=True)
        raw.mkdir()
        plan = {
            "schema": 2, "fault_id": "fixture-control",
            "operation": "mballoc_mark", "action": "alloc-mballoc",
            "syscall_nr": 285, "program": "fault-formula", "call_index": 0,
        }
        (evidence / "fault-plan.json").write_text(json.dumps(plan) + "\n")
        workload = {"schema": 2, "pid": 123, "call_performed": True,
                    "arm_verified": False, "syscall_result": 0,
                    "syscall_errno": 0, "target_before": {},
                    "target_after": {}}
        (raw / "workload-result.json").write_text(json.dumps(workload) + "\n")
        trace = (
            "fault-formula-123 [000] ..... 1.000000: tracing_mark_write: "
            "CT_FAULT_ARM phase=ARM_END pid=123 call_index=0 "
            "operation=mballoc_mark errno=-5 program=fault-formula status=disabled\n"
            "fault-formula-123 [000] ..... 1.001000: tracing_mark_write: "
            "CT_SYSCALL phase=BEGIN id=fault-123-0 nr=285 name=fallocate "
            "program=fault-formula call_index=0 action=alloc-mballoc target_fd=-1\n"
            "fault-formula-123 [000] ..... 1.002000: sys_enter: NR 285 (3, 0, 0, 1000, 0, 0)\n"
            "fault-formula-123 [000] ..... 1.003000: sys_exit: NR 285 = 0\n"
            "fault-formula-123 [000] ..... 1.004000: tracing_mark_write: "
            "CT_SYSCALL phase=END id=fault-123-0 nr=285 name=fallocate "
            "program=fault-formula call_index=0 action=alloc-mballoc target_fd=-1 ret=0 errno=0\n"
        )
        (raw / "trace.txt").write_text(trace)
        (raw / "cpu0-stats.txt").write_text("overrun: 0\n")
        snapshot = {"valid": True, "relations_valid": True,
                    "namespace_valid": True, "observation_stable": True,
                    "snapshot_id": 1, "version": 1}
        for name in ("stable-before.json", "stable-after.json",
                     "stable-oracle.json", "remount.json"):
            (raw / name).write_text(json.dumps(snapshot) + "\n")
        (raw / "run-status.txt").write_text(
            "workload_rc=0\nafter_snapshot_rc=0\nunmount_rc=0\n"
            "remount_rc=0\nremount_umount_rc=0\ntrace_loss=0\nworkers=1\n")
        (raw / "unmount-status.txt").write_text("unmount_ok\n")
        (raw / "workload.txt").write_text("control\n")
        (run / "test.raw").write_bytes(b"control-image")
        (run / "recovery.raw").write_bytes(b"control-image")
        import hashlib
        image_hash = hashlib.sha256(b"control-image").hexdigest()
        (run / "image-integrity.json").write_text(json.dumps({
            "schema": 1, "repair_target": "recovery.raw",
            "repair_performed": False,
            "primary_image": {"path": "../test.raw",
                               "sha256_after_guest": image_hash,
                               "sha256_after_checks": image_hash},
            "recovery_image": {"path": "../recovery.raw",
                                "sha256_after_guest": image_hash,
                                "sha256_before_repair": image_hash,
                                "sha256_after_repair": image_hash,
                                "sha256_after_checks": image_hash},
        }) + "\n")
        (run / "fsck-before-recovery.txt").write_text("Filesystem clean\n")
        (run / "fsck-recovery.txt").write_text("Filesystem clean\n")
        (run / "fsck.txt").write_text("Filesystem clean\n")
        disk = {"valid": True, "status": "verified", "unverified": [],
                "bitmap_descriptor_match": True,
                "superblock_descriptor_match": True}
        (run / "disk-before-recovery.json").write_text(json.dumps(disk) + "\n")
        (run / "disk.json").write_text(json.dumps(disk) + "\n")
        return temporary, run, evidence

    def test_matched_control_is_accepted(self):
        temporary, run, evidence = self.make_run()
        self.addCleanup(temporary.cleanup)
        self.assertEqual(collector.collect_control(ROOT, run, 0, 0, 0, 0, 0, 0), 0)
        report = control_validator.validate(evidence)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["classification"], "matched_control")

    def test_blkdebug_propagates_error_without_modifying_image(self):
        report = blkdebug.validate(None, "none", 5)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["classification"], "blkdebug_io_error_propagated")


if __name__ == "__main__":
    unittest.main()
