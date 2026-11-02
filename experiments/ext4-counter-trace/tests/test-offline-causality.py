#!/usr/bin/env python3
import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/analyze-offline-causality.py"
SPEC = importlib.util.spec_from_file_location("offline_causality", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class OfflineCausalityTests(unittest.TestCase):
    def test_fsck_parser_handles_english_and_chinese(self):
        text = ("已删除 inode 31 的删除时间为零。\n"
                "已删除 inode 35 的删除时间为零。\n"
                "Inode 位图的差异：  -31 -35 -38\n")
        self.assertEqual(MODULE.fsck_inodes(text), {
            "deleted_dtime_zero": [31, 35],
            "bitmap_clear_candidates": [31, 35, 38],
        })

    def test_first_anomaly_requires_a_healthy_to_degraded_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            for segment_id, healthy in ((0, True), (1, False), (2, False)):
                folder = run / "segments" / f"segment-{segment_id:06d}"
                write_json(folder / "manifest.json", {
                    "segment_id": segment_id, "kind": "inline", "status": "complete"})
                snapshot = {
                    "valid": True, "namespace_valid": healthy,
                    "relations_valid": True, "observed_mismatches": 0,
                    "namespace_mismatched": 0, "relations_mismatches": 0,
                    "free_clusters": 10, "free_inodes": 20, "dirs": 2,
                    "snapshot_id": segment_id,
                    "namespace_reason": "stable" if healthy else "inode_read_failed",
                }
                before = dict(snapshot)
                if segment_id == 1:
                    before.update(namespace_valid=True, namespace_reason="stable")
                write_json(folder / "inline-before.json", before)
                write_json(folder / "inline-after.json", snapshot)
            result = MODULE.first_anomaly(run, "inline")
            self.assertEqual(result["segment_id"], 1)
            self.assertEqual(result["previous_segment_id"], 0)
            self.assertEqual(result["transition"], "healthy_to_degraded")

    def test_control_requires_no_fault_and_matching_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "target"
            control = pathlib.Path(directory) / "control"
            target_meta = {"status": "complete", "clean_shutdown": True,
                           "limits": {"mode": "single", "workers": 1,
                                      "scenario": "baseline", "seed": 7,
                                      "random_fault_injection": True},
                           "build_sha256": {key: key for key in MODULE.BUILD_ARTIFACTS},
                           "dependencies": {"rootfs_sha256": "rootfs"},
                           "start_images": {"inline": {"sha256": "image"}}}
            write_json(target / "campaign.json", target_meta)
            write_json(control / "campaign.json", {
                "status": "complete", "clean_shutdown": True,
                "limits": {"mode": "single", "workers": 1,
                           "scenario": "baseline", "seed": 7,
                           "random_fault_injection": False},
                "build_sha256": {key: key for key in MODULE.BUILD_ARTIFACTS},
                "dependencies": {"rootfs_sha256": "rootfs"},
                "start_images": {"inline": {"sha256": "image"}},
                "disk": {"inline": {"fsck_exit": 0}}})
            write_json(control / "inline-disk.json", {
                "valid": True, "status": "verified"})
            write_json(control / "checkpoint/inline-final.json", {
                "valid": True, "namespace_valid": True,
                "relations_valid": True, "observed_mismatches": 0,
                "namespace_mismatched": 0, "relations_mismatches": 0})
            result = MODULE.control_summary(control, control, "inline",
                                            target_meta["limits"], target_meta)
            self.assertEqual(result["interpretation"], "matched_control")
            self.assertFalse(result["fault_injection_enabled"])
            self.assertTrue(result["parameters_match"])
            self.assertTrue(result["build_match"])
            self.assertTrue(result["start_images_match"])
            self.assertTrue(result["rootfs_match"])

    def test_control_rejects_different_kernel_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "target"
            control = pathlib.Path(directory) / "control"
            target_meta = {"limits": {"mode": "single", "workers": 1},
                           "build_sha256": {key: "target" for key in MODULE.BUILD_ARTIFACTS},
                           "dependencies": {"rootfs_sha256": "rootfs"},
                           "start_images": {"inline": {"sha256": "image"}}}
            control_meta = {"status": "complete", "clean_shutdown": True,
                            "limits": {"mode": "single", "workers": 1,
                                       "random_fault_injection": False},
                            "build_sha256": {key: "control" for key in MODULE.BUILD_ARTIFACTS},
                            "dependencies": {"rootfs_sha256": "rootfs"},
                            "start_images": {"inline": {"sha256": "image"}},
                            "disk": {"inline": {"fsck_exit": 0}}}
            write_json(target / "campaign.json", target_meta)
            write_json(control / "campaign.json", control_meta)
            write_json(control / "inline-disk.json", {"valid": True, "status": "verified"})
            write_json(control / "checkpoint/inline-final.json", {
                "valid": True, "namespace_valid": True,
                "relations_valid": True, "observed_mismatches": 0,
                "namespace_mismatched": 0, "relations_mismatches": 0})
            result = MODULE.control_summary(control, control, "inline",
                                            target_meta["limits"], target_meta)
            self.assertEqual(result["interpretation"], "control_observed_but_not_matched")
            self.assertIn("kernel_or_probe_binding_differs", result["blockers"])

    def test_fault_without_envelope_is_not_nearest_syscall(self):
        fault = {"pid": 9, "line": 30}
        calls = [{"pid": 9, "id": "call-1", "marker_begin_line": 1,
                  "marker_end_line": 20}]
        result = MODULE._syscall_attribution(calls, fault)
        self.assertEqual(result["method"], "no_ct_syscall_envelope")
        self.assertEqual(result["candidates"], [])


if __name__ == "__main__":
    unittest.main()
