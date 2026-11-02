#!/usr/bin/env python3
"""Deterministic single-worker plans retained by the evidence inventory."""
from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
import pathlib

from fault_errno_catalog import load_catalog


def clean_recovery() -> dict[str, object]:
    return {
        "status": "specified",
        "image_policy": {"primary": "immutable", "repair_target": "copy"},
        "pre_recovery": {"fsck": {"state": "clean"},
                          "disk": {"state": "verified"}},
        "repair": {"kind": "none", "allowed_exit": [0]},
        "post_recovery": {"fsck": "clean", "disk": "verified"},
        "allowed_delta": {"free_clusters": 0, "free_inodes": 0, "dirs": 0},
    }


def journal_abort_recovery(*, remount_counters: str = "must_match") -> dict[str, object]:
    return {
        "status": "specified",
        "image_policy": {"primary": "immutable", "repair_target": "copy"},
        "pre_recovery": {
            "fsck": {"state": "clean"},
            "disk": {"state": "verified"},
            "remount_counters": {"state": remount_counters},
        },
        "repair": {"kind": "none", "allowed_exit": [0]},
        "post_recovery": {"fsck": "clean", "disk": "verified"},
        "allowed_delta": {"free_clusters": 0, "free_inodes": 0, "dirs": 0},
    }


def journal_abort_stable_states() -> dict[str, object]:
    return {
        "stable-before": {"valid": True, "relations_valid": True,
                          "namespace_valid": True},
        "stable-after": {"valid": False, "relations_valid": False,
                         "namespace_valid": False},
        "remount": {"valid": False, "relations_valid": False,
                    "namespace_valid": False},
    }


def inspect_and_repair(allowed_delta: dict[str, object], *,
                       remount_counters: str = "must_match",
                       fsck_signatures: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "status": "specified",
        "image_policy": {"primary": "immutable", "repair_target": "copy"},
        "pre_recovery": {
            "fsck": {"state": "inspect", "allowed_exit": [0, 1, 2, 4, 8],
                     "required_signatures": list(fsck_signatures)},
            "disk": {"state": "inspect"},
            "remount_counters": {"state": remount_counters},
        },
        "repair": {"kind": "e2fsck-fy", "allowed_exit": [0, 1]},
        "post_recovery": {"fsck": "clean", "disk": "verified"},
        "allowed_delta": allowed_delta,
    }


def evict_final_recovery() -> dict[str, object]:
    return {
        "status": "verified",
        "image_policy": {"primary": "immutable", "repair_target": "copy"},
        "pre_recovery": {
            "fsck": {"state": "known_issue", "allowed_exit": [1, 2, 4, 8],
                     "required_signatures": [
                         "Deleted inode {target_inode} has zero dtime",
                         "Inode bitmap differences:"]},
            "disk": {"state": "verified"},
        },
        "repair": {"kind": "e2fsck-fy", "allowed_exit": [0, 1],
                   "required_signatures": ["FILE SYSTEM WAS MODIFIED"]},
        "post_recovery": {"fsck": "clean", "disk": "verified"},
        "allowed_delta": {"free_clusters": 0, "free_inodes": 1, "dirs": 0},
    }


def dirty_metadata_recovery(*signatures: str) -> dict[str, object]:
    """Contract for dirty metadata rejected after the inode was changed."""
    return {
        "status": "specified",
        "image_policy": {"primary": "immutable", "repair_target": "copy"},
        "pre_recovery": {
            "fsck": {"state": "known_issue", "allowed_exit": [1, 2, 4, 8],
                     "required_signatures": list(signatures)},
            "disk": {"state": "verified"},
            "remount_counters": {"state": "unverified"},
        },
        "repair": {"kind": "e2fsck-fy", "allowed_exit": [0, 1],
                   "required_signatures": ["FILE SYSTEM WAS MODIFIED"]},
        "post_recovery": {"fsck": "clean", "disk": "verified"},
        "allowed_delta": {"free_clusters": 0, "free_inodes": 0, "dirs": 0},
    }


@dataclass(frozen=True)
class FaultPlan:
    fault_id: str
    operation: str
    action: str
    syscall_nr: int
    result: int
    errno: int
    return_policy: str
    formulas: tuple[str, ...]
    oracle: str
    expected_oracle_state: str = "unverified"
    expected_formula_states: dict[str, object] | None = None
    oracle_points: dict[str, tuple[str, ...]] | None = None
    recovery: dict[str, object] | None = None
    expected_namespace_states: dict[str, bool] | None = None
    expected_stable_states: dict[str, dict[str, bool | None]] | None = None
    allowed_unverified_formulas: tuple[str, ...] = ()
    error_semantics: dict[str, object] | None = None
    observed_object_fields: dict[str, dict[str, object]] | None = None
    injected_errno: int = -5
    caller_result: int | None = None
    caller_errno: int | None = None

    def as_json(self) -> dict[str, object]:
        recovery = dict(self.recovery or {"status": "unverified"})
        recovery.setdefault("image_policy", {
            "primary": "immutable", "repair_target": "copy"})
        hook_contract = source_hook_contract(self.operation, self.injected_errno)
        # The source catalog describes how the helper returns.  The fixture
        # plan additionally records whether userspace must observe that return
        # value, consumes it, or only records the helper boundary.  Keep the
        # two contracts in the same signed plan so a validator can reject a
        # plan whose source path and syscall assertion disagree.
        if isinstance(hook_contract, dict) and hook_contract.get("status") == "source_anchored":
            hook_contract = dict(hook_contract)
            hook_contract["syscall_return_policy"] = self.return_policy
        stable = self.expected_stable_states or {
            "stable-before": {"valid": True, "relations_valid": True,
                              "namespace_valid": True},
            "stable-after": {"valid": True, "relations_valid": True,
                             "namespace_valid": True},
            "remount": {"valid": True, "relations_valid": True,
                        "namespace_valid": True},
        }
        return {
            "schema": 2, "contract_schema": 1, "fault_id": self.fault_id,
            "operation": self.operation, "action": self.action,
            "syscall_nr": self.syscall_nr, "count": 1,
            "return_policy": self.return_policy,
            # The configured errno is the helper's injected return.  Caller
            # cleanup may replace it before the syscall reaches userspace.
            "expected_return": {
                "result": self.result if self.caller_result is None else self.caller_result,
                "errno": self.errno if self.caller_errno is None else self.caller_errno,
            },
            "injected_return": {"result": -1, "errno": abs(self.injected_errno)},
            "formulas": list(self.formulas), "oracle": self.oracle,
            "expected_oracle_state": self.expected_oracle_state,
            "expected_formula_states": self.expected_formula_states or {},
            "allowed_unverified_formulas": list(self.allowed_unverified_formulas),
            "oracle_points": {key: list(value) for key, value in
                              (self.oracle_points or {}).items()},
            "expected_namespace_states": self.expected_namespace_states or {},
            "expected_stable_states": stable,
            "expected_objects": object_expectation(self.fault_id),
            "observed_object_fields": self.observed_object_fields or {},
            "program": "fault-formula", "call_index": 0,
            "errno": self.injected_errno,
            "recovery": recovery,
            "recovery_status": recovery.get("status", "unverified"),
            "hook_contract": hook_contract,
            "error_semantics": self.error_semantics or {},
            "support": "interface_fixture_defined", "vm_verified": False,
        }


STABLE = ("SNAPSHOT.MEMORY.STABLE",)
DELTA_STABLE = ("FIXTURE.STABLE.DELTA",)
UNVERIFIED = "unverified"
CONSISTENT = "consistent"
MISMATCH = "mismatch_reproduced"
JOURNAL_ABORT = {
    "injected_errno": "EIO",
    "journal_abort": True,
    "abort_scope": "journal",
    "scope": "fault-injected-error-at-ext4-jbd2-helper",
    "forced_shutdown": False,
    "persistence_expectation": "source_path_must_be_checked",
}
JBD2_DIRTY_ERROR = {
    "injected_errno": "EFSCORRUPTED",
    "handle_abort": True,
    # ext4_handle_dirty_metadata_error() aborts the current journal handle.
    # ext4_error_inode() applies the filesystem's configured error policy,
    # which is distinct from unconditionally aborting the JBD2 journal.
    "journal_abort": False,
    "forced_shutdown": False,
    "scope": "jbd2_journal_dirty_metadata_return_-EUCLEAN",
    "ext4_error_handler": "ext4_handle_dirty_metadata_error",
    "abort_scope": "current_handle",
    "persistence_expectation": "source_path_must_be_checked",
}


PLANS: dict[str, FaultPlan] = {
    "mballoc-mark": FaultPlan(
        "mballoc-mark-eio", "mballoc_mark", "alloc-mballoc", 285, -1, 5,
        "exact_error", ("R-BG", "R-BSUM", "R-BDELTA"),
        "allocation_rejected_before_bitmap_transition", UNVERIFIED,
        {"R-BG": UNVERIFIED, "R-BSUM": UNVERIFIED,
         "R-BDELTA": UNVERIFIED},
        {"R-BG": STABLE, "R-BSUM": STABLE, "R-BDELTA": DELTA_STABLE},
        journal_abort_recovery(),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("R-BG", "R-BSUM", "R-BDELTA"),
        error_semantics=JOURNAL_ABORT),
    "mballoc-free-mark": FaultPlan(
        "mballoc-free-mark-eio", "mballoc_free_mark", "free-punch", 285, -1, 30,
        "exact_error", ("R-BG", "R-BSUM", "R-BDELTA"),
        "write_access_rejected_before_bitmap_transition", UNVERIFIED,
        {"R-BG": UNVERIFIED, "R-BSUM": UNVERIFIED, "R-BDELTA": UNVERIFIED},
        {"R-BG": STABLE, "R-BSUM": STABLE, "R-BDELTA": DELTA_STABLE},
        journal_abort_recovery(remount_counters="unverified"),
        allowed_unverified_formulas=("R-BG", "R-BSUM", "R-BDELTA"),
        expected_stable_states=journal_abort_stable_states(),
        error_semantics={
            "injected_errno": "EROFS",
            "journal_abort": False,
            "handle_abort": True,
            "abort_scope": "current_handle",
            "forced_shutdown": False,
            "caller_contract": "helper_consumed_or_void",
            "scope": "mballoc-mark-write-access",
            "persistence_expectation": "source_path_must_be_checked",
        },
        observed_object_fields={"target_after": {"exists": True,
                                                  "extent_count": 0,
                                                  "blocks": 2048}}),
    "inode-evict-final-dirty": FaultPlan(
        "inode-evict-final-dirty-efscorrupted", "inode_evict_final_dirty", "evict-close",
        3, 0, 0, "exact_success", ("R-IEVICT", "R-IDELTA-EVICT"),
        "final_eviction_bit_release", MISMATCH,
        {"R-IEVICT": MISMATCH, "R-IDELTA-EVICT": UNVERIFIED},
        {"R-IEVICT": ("INODE.EVICT.TARGET.EXIT",),
         "R-IDELTA-EVICT": ("INODE.EVICT.DESCRIPTOR.EXIT", "INODE.EVICT.GLOBAL.EXIT")},
        journal_abort_recovery(),
        {"stable-before": True, "stable-after": False, "remount": False},
        journal_abort_stable_states(),
        allowed_unverified_formulas=("R-IDELTA-EVICT",),
        error_semantics=JBD2_DIRTY_ERROR, injected_errno=-117),
    "orphan-file-delete-access": FaultPlan(
        "orphan-file-delete-access-eio", "orphan_file_delete_access", "evict-close",
        3, 0, 0, "exact_success", ("O-LIFE",),
        "orphan_slot_access_failure", UNVERIFIED,
        {"O-LIFE": UNVERIFIED}, {"O-LIFE": STABLE},
        journal_abort_recovery(),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("O-LIFE",),
        error_semantics=JOURNAL_ABORT),
    "xattr-release-access": FaultPlan(
        "xattr-release-access-eio", "xattr_release_access", "xattr-evict-close",
        3, 0, 0, "exact_success", ("XATTR-REF", "XATTR-REF-DELTA"),
        "xattr_release_access_failure", UNVERIFIED,
        {"XATTR-REF": UNVERIFIED, "XATTR-REF-DELTA": MISMATCH},
        {"XATTR-REF": STABLE, "XATTR-REF-DELTA": ("XATTR.INODE.DETACH.EXIT",)},
        journal_abort_recovery(),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("XATTR-REF",),
        error_semantics=JOURNAL_ABORT),
    "xattr-release-free-block": FaultPlan(
        "xattr-release-free-block-eio", "xattr_release_bitmap_access",
        "xattr-single-evict-close", 3, 0, 0, "exact_success",
        ("XATTR-REF", "XATTR-REF-DELTA"), "xattr_final_reference_free_failure",
        UNVERIFIED,
        {"XATTR-REF": UNVERIFIED, "XATTR-REF-DELTA": MISMATCH},
        {"XATTR-REF": STABLE,
         "XATTR-REF-DELTA": ("XATTR.INODE.DETACH.EXIT",)},
        journal_abort_recovery(),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("XATTR-REF",),
        error_semantics=JOURNAL_ABORT),
    "rmdir-child-dirty": FaultPlan(
        "rmdir-child-dirty-efscorrupted", "rmdir_child_dirty", "rmdir", 84, -1, 117,
        "exact_error", ("R-LINK", "R-UG"),
        "rmdir_child_nlink_dirty_failure", UNVERIFIED,
        {"R-LINK": UNVERIFIED, "R-UG": UNVERIFIED},
        {"R-LINK": STABLE, "R-UG": STABLE}, journal_abort_recovery(),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("R-LINK", "R-UG"),
        error_semantics=JBD2_DIRTY_ERROR, injected_errno=-117,
        observed_object_fields={"target_after": {"exists": True, "nlink": 0,
                                                  "size": 0}}),
    "rmdir-parent-dirty": FaultPlan(
        "rmdir-parent-dirty-efscorrupted", "rmdir_parent_dirty", "rmdir", 84, -1, 117,
        "exact_error", ("R-LINK", "R-UG"),
        "rmdir_parent_nlink_dirty_failure", UNVERIFIED,
        {"R-LINK": UNVERIFIED, "R-UG": UNVERIFIED},
        {"R-LINK": STABLE, "R-UG": STABLE}, journal_abort_recovery(),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("R-LINK", "R-UG"),
        error_semantics=JBD2_DIRTY_ERROR, injected_errno=-117,
        observed_object_fields={"target_after": {"exists": True, "nlink": 0,
                                                  "size": 0}}),
    "unlink-dirent-dirty": FaultPlan(
        "unlink-dirent-dirty-efscorrupted", "unlink_dirent_dirty", "unlink", 263, -1, 117,
        "exact_error", ("R-LINK",), "unlink_dirent_removed_inode_unchanged",
        UNVERIFIED, {"R-LINK": UNVERIFIED}, {"R-LINK": STABLE},
        journal_abort_recovery(),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("R-LINK",),
        error_semantics=JBD2_DIRTY_ERROR, injected_errno=-117,
        observed_object_fields={"target_after": {"exists": True, "nlink": 1}}),
    "rename-dir-finish": FaultPlan(
        "rename-dir-finish-efscorrupted", "rename_dir_finish", "rename-dir", 316, -1, 117,
        "exact_error", ("R-LINK", "R-UG"),
        "cross_directory_rename_dotdot_dirty_failure", UNVERIFIED,
        {"R-LINK": UNVERIFIED, "R-UG": UNVERIFIED},
        {"R-LINK": STABLE, "R-UG": STABLE}, journal_abort_recovery(),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("R-LINK", "R-UG"),
        error_semantics=JBD2_DIRTY_ERROR, injected_errno=-117),
    "rename-delete-old": FaultPlan(
        "rename-delete-old-eio", "rename_delete_old", "rename-replace", 316, -1, 30,
        "exact_error", ("R-LINK",), "journal_aborted_old_rename_delete",
        UNVERIFIED, {"R-LINK": UNVERIFIED}, {"R-LINK": STABLE},
        journal_abort_recovery(),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("R-LINK",),
        error_semantics=JOURNAL_ABORT,
        observed_object_fields={"source_after": {"exists": True, "nlink": 1},
                                "destination_after": {"exists": True, "nlink": 0}}),
    "migrate-swap-inode-data": FaultPlan(
        "migrate-swap-inode-data-efscorrupted", "migrate_swap_inode_data",
        "migrate-extents", 16, -1, 117, "exact_error", ("R-MIGRATE",),
        "migration_dirty_return", UNVERIFIED, {"R-MIGRATE": UNVERIFIED},
        {"R-MIGRATE": ("FAULT.MIGRATE.INODE.DIRTY",)},
        dirty_metadata_recovery("checksum does not match inode."),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("R-MIGRATE",),
        error_semantics=JBD2_DIRTY_ERROR, injected_errno=-117,
        caller_result=-1, caller_errno=30),
    "xattr-set-inode-dirty": FaultPlan(
        "xattr-set-inode-dirty-efscorrupted", "xattr_set_inode_dirty",
        "xattr-set", 188, -1, 117, "exact_error", ("R-XATTR-ATOMIC",),
        "xattr_dirty_return", UNVERIFIED, {"R-XATTR-ATOMIC": UNVERIFIED},
        {"R-XATTR-ATOMIC": ("FAULT.XATTR.INODE.DIRTY",)},
        journal_abort_recovery(remount_counters="unverified"),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("R-XATTR-ATOMIC",),
        error_semantics=JBD2_DIRTY_ERROR, injected_errno=-117),
    "ea-inode-ref-dirty": FaultPlan(
        "ea-inode-ref-dirty-efscorrupted", "ea_inode_ref_dirty",
        "ea-ref-set", 188, -1, 117, "exact_error", ("EA-REF-DELTA",),
        "ea_reference_dirty_return", UNVERIFIED, {"EA-REF-DELTA": UNVERIFIED},
        {"EA-REF-DELTA": ("FAULT.EA.INODE.REF.DIRTY",)},
        {
            "status": "specified",
            "pre_recovery": {
                "fsck": {
                    "state": "known_issue", "allowed_exit": [1, 2, 4, 8],
                    "required_signatures": [
                        "Deleted inode ", "Inode bitmap differences:",
                        "Orphan file (inode 12) block 0 is not clean.",
                    ],
                },
                "disk": {"state": "mismatch"},
                "remount_counters": {"state": "unverified"},
            },
            "repair": {"kind": "e2fsck-fy", "allowed_exit": [0, 1],
                       "required_signatures": ["FILE SYSTEM WAS MODIFIED"]},
            "post_recovery": {"fsck": "clean", "disk": "verified"},
            "allowed_delta": {"free_clusters": 2, "free_inodes": 1,
                              "dirs": 0},
        },
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("EA-REF-DELTA",),
        error_semantics=JBD2_DIRTY_ERROR, injected_errno=-117,
        caller_errno=30),
    "acl-mode-dirty": FaultPlan(
        "acl-mode-dirty-efscorrupted", "acl_mode_dirty", "acl-mode",
        188, -1, 117, "exact_error", ("R-ACL-MODE",),
        "acl_mode_dirty_return", UNVERIFIED, {"R-ACL-MODE": UNVERIFIED},
        {"R-ACL-MODE": ("FAULT.ACL.MODE.DIRTY",)},
        journal_abort_recovery(remount_counters="unverified"),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("R-ACL-MODE",),
        error_semantics=JBD2_DIRTY_ERROR, injected_errno=-117),
    "inline-dir-block-dirty": FaultPlan(
        "inline-dir-block-dirty-efscorrupted", "inline_dir_block_dirty",
        "inline-dir", 258, -1, 117, "exact_error", ("R-INLINE-DIR",),
        "inline_dir_block_dirty_return", UNVERIFIED,
        {"R-INLINE-DIR": UNVERIFIED},
        {"R-INLINE-DIR": ("FAULT.INLINE.DIR.BLOCK.DIRTY",)},
        journal_abort_recovery(remount_counters="unverified"),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("R-INLINE-DIR",),
        error_semantics=JBD2_DIRTY_ERROR, injected_errno=-117),
    "inline-dir-inode-dirty": FaultPlan(
        "inline-dir-inode-dirty-efscorrupted", "inline_dir_inode_dirty",
        "inline-dir", 258, -1, 117, "exact_error", ("R-INLINE-DIR",),
        "inline_dir_inode_dirty_return", UNVERIFIED,
        {"R-INLINE-DIR": UNVERIFIED},
        {"R-INLINE-DIR": ("FAULT.INLINE.DIR.INODE.DIRTY",)},
        journal_abort_recovery(remount_counters="unverified"),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("R-INLINE-DIR",),
        error_semantics=JBD2_DIRTY_ERROR, injected_errno=-117),
    "inline-data-block-write-begin-eio": FaultPlan(
        "inline-data-block-write-begin-eio", "inline_data_block_write_begin",
        "inline-file-write", 1, -1, 5, "exact_error", ("R-INLINE",),
        "inline_data_representation", CONSISTENT,
        {"R-INLINE": (CONSISTENT, MISMATCH)},
        {"R-INLINE": ("FAULT.INLINE.DATA.BLOCK.WRITE_BEGIN",)},
        clean_recovery(),
        error_semantics={
            "injected_errno": "EIO",
            "journal_abort": False,
            "forced_shutdown": False,
            "failed_write_cleanup": True,
            "scope": "inline-data-to-extent-block-write-begin",
            "persistence_expectation": "source_path_must_be_checked",
        },
        injected_errno=-5),
    "fileattr-flags-dirty": FaultPlan(
        "fileattr-flags-dirty-efscorrupted", "fileattr_flags_dirty",
        "fileattr-flags", 16, -1, 117, "exact_error", ("R-FILEATTR",),
        "fileattr_flags_dirty_return", UNVERIFIED,
        {"R-FILEATTR": UNVERIFIED},
        {"R-FILEATTR": ("FAULT.FILEATTR.FLAGS.DIRTY",)},
        journal_abort_recovery(remount_counters="unverified"),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("R-FILEATTR",),
        error_semantics=JBD2_DIRTY_ERROR, injected_errno=-117),
    "fileattr-project-dirty": FaultPlan(
        "fileattr-project-dirty-efscorrupted", "fileattr_project_dirty",
        "fileattr-project", 16, -1, 117, "exact_error", ("R-FILEATTR",),
        "fileattr_project_dirty_return", UNVERIFIED,
        {"R-FILEATTR": UNVERIFIED},
        {"R-FILEATTR": ("FAULT.FILEATTR.PROJECT.DIRTY",)},
        journal_abort_recovery(remount_counters="unverified"),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("R-FILEATTR",),
        error_semantics=JBD2_DIRTY_ERROR, injected_errno=-117),
    "es-insert-alloc": FaultPlan(
        "es-insert-alloc-enomem", "es_insert_alloc", "extent-split", 285,
        0, 0, "exact_success", ("R-ES",),
        "extent_status_allocation_discarded", UNVERIFIED,
        {"R-ES": UNVERIFIED},
        {"R-ES": ("FAULT.ES.INSERT.ALLOC",)},
        clean_recovery(),
        allowed_unverified_formulas=("R-ES",),
        error_semantics={"injected_errno": "ENOMEM", "journal_abort": False,
                         "forced_shutdown": False,
                         "caller_contract": "void_helper_discards_optional_cache_error",
                         "scope": "extent-status-node-allocation"},
        injected_errno=-12),
    "delalloc-reserve": FaultPlan(
        "delalloc-reserve-enospc", "delalloc_reserve", "inline-file-write", 1,
        # ext4_da_reserve_space() returns -ENOSPC, but the write path may
        # consume it and retry through its non-delalloc fallback.  The hook
        # return is therefore recorded and checked independently of write().
        0, 0, "record_only", ("R-RESV", "R-DELALLOC"),
        "delalloc_reservation_rejected", CONSISTENT,
        {"R-RESV": CONSISTENT, "R-DELALLOC": CONSISTENT},
        {"R-RESV": ("FAULT.DELALLOC.RESERVE",),
         "R-DELALLOC": ("FAULT.DELALLOC.RESERVE.GLOBAL",)},
        clean_recovery(),
        error_semantics={"injected_errno": "ENOSPC", "journal_abort": False,
                         "forced_shutdown": False,
                         "caller_contract": "caller_consumed_fallback",
                         "scope": "delalloc-reservation-before-counter-update"},
        injected_errno=-28),
    "extent-insert-dirty": FaultPlan(
        "extent-insert-dirty-efscorrupted", "extent_insert_dirty", "extent-split",
        285, -1, 117, "exact_error", ("R-EXTENT",),
        "extent_tree_dirty_publish_failure", UNVERIFIED,
        {"R-EXTENT": UNVERIFIED},
        {"R-EXTENT": ("FAULT.EXTENT.INSERT.DIRTY",)},
        journal_abort_recovery(remount_counters="unverified"),
        expected_stable_states=journal_abort_stable_states(),
        allowed_unverified_formulas=("R-EXTENT",),
        error_semantics=JBD2_DIRTY_ERROR, injected_errno=-117),
    "mb-buddy-load": FaultPlan(
        "mb-buddy-load-eio", "mb_buddy_load", "alloc-mballoc", 285,
        -1, 5, "exact_error", ("R-MB-BUDDY",),
        "buddy_cache_load_rejected", UNVERIFIED,
        {"R-MB-BUDDY": UNVERIFIED},
        {"R-MB-BUDDY": ("FAULT.MB.BUDDY.LOAD",)},
        clean_recovery(),
        allowed_unverified_formulas=("R-MB-BUDDY",),
        error_semantics={"injected_errno": "EIO", "journal_abort": False,
                         "forced_shutdown": False,
                         "caller_contract": "allocation_returns_error",
                         "scope": "mballoc-buddy-cache-load"},
        injected_errno=-5),
}


def object_expectation(fault_id: str) -> dict[str, object]:
    present = {
        "inode-evict-truncate-start-eio": {"target_before": True, "target_after": False},
        "inode-evict-final-dirty-efscorrupted": {"target_before": True, "target_after": False},
        "orphan-file-delete-access-eio": {"target_before": True, "target_after": False},
        "rmdir-child-dirty-efscorrupted": {"target_before": True, "target_after": True},
        "rmdir-parent-dirty-efscorrupted": {"target_before": True, "target_after": True},
        "unlink-dirent-dirty-efscorrupted": {"target_before": True, "target_after": True},
    }
    if fault_id == "mballoc-mark-eio":
        return {"present": {"target_before": True, "target_after": True},
                "same_state": [["target_before", "target_after"]]}
    if fault_id == "mballoc-free-mark-eio":
        return {"present": {"target_before": True, "target_after": True},
                "same_inode": [["target_before", "target_after"]],
                "decreased_extent_count": [["target_before", "target_after"]]}
    if fault_id in {"es-insert-alloc-enomem", "delalloc-reserve-enospc",
                    "extent-insert-dirty-efscorrupted", "mb-buddy-load-eio"}:
        return {"present": {"target_before": True, "target_after": True}}
    if fault_id in present:
        return {"present": present[fault_id]}
    if fault_id == "xattr-release-access-eio":
        return {"present": {"target_before": True, "target_after": False,
                             "destination_before": True, "destination_after": True},
                "same_state": [["destination_before", "destination_after"]]}
    if fault_id == "xattr-release-free-block-eio":
        return {"present": {"target_before": True, "target_after": False,
                             "destination_before": False, "destination_after": False}}
    if fault_id == "rename-dir-finish-efscorrupted":
        return {"present": {"source_before": True, "source_after": True,
                             "destination_before": False, "destination_after": False},
                "same_state": [["source_before", "source_after"],
                               ["destination_before", "destination_after"]]}
    if fault_id == "rename-delete-old-eio":
        return {"present": {"source_before": True, "source_after": True,
                             "destination_before": True, "destination_after": True},
                "state_transitions": [{
                    "before": "source_before", "after": "source_after",
                    "unchanged_fields": ["exists", "stat_ok", "size", "blocks",
                                         "ino", "nlink", "extent_count", "extent_hash",
                                         "xattr_present", "xattr_size", "xattr_hash"],
                    "delta_fields": {"nlink": 0}}, {
                    "before": "destination_before", "after": "destination_after",
                    "unchanged_fields": ["exists", "stat_ok", "size", "blocks",
                                         "ino", "extent_count", "extent_hash",
                                         "xattr_present", "xattr_size", "xattr_hash"],
                    "delta_fields": {"nlink": -1}}]}
    if fault_id in {
            "migrate-swap-inode-data-efscorrupted",
            "xattr-set-inode-dirty-efscorrupted",
            "ea-inode-ref-dirty-efscorrupted",
            "acl-mode-dirty-efscorrupted",
            "fileattr-flags-dirty-efscorrupted",
            "fileattr-project-dirty-efscorrupted"}:
        return {"present": {"target_before": True, "target_after": True}}
    if fault_id in {"inline-dir-block-dirty-efscorrupted",
                    "inline-dir-inode-dirty-efscorrupted"}:
        return {"present": {"target_before": False, "target_after": False}}
    if fault_id == "inline-data-block-write-begin-eio":
        return {"present": {"target_before": True, "target_after": True}}
    # Catalog-generated plans still require the fixture to capture both
    # states, but a generic action cannot promise whether the source cleanup
    # removes or preserves each named object. The validator treats this
    # explicit capture-only contract as an intentional source-defined
    # postcondition rather than silently accepting a missing contract.
    return {"capture_only": True, "present": {}}


def _catalog_matrix() -> dict[str, dict[str, object]]:
    """Load the authoritative one-fixture-per-hook matrix lazily."""
    path = pathlib.Path(__file__).resolve().parents[1] / "hook-formula-matrix.json"
    try:
        entries = json.loads(path.read_text()).get("entries", [])
    except (OSError, ValueError, TypeError):
        return {}
    return {entry["operation"]: entry for entry in entries
            if isinstance(entry, dict) and entry.get("operation")}


def source_hook_contract(operation: str, injected_errno: int) -> dict[str, object]:
    """Expand a hook into the source-backed contract consumed by validators."""
    matrix = _catalog_matrix().get(operation)
    definition = load_catalog().get("hooks", {}).get(operation)
    if not matrix or not definition:
        return {"status": "missing", "operation": operation}
    native = next((item for item in definition.get("native_errnos", [])
                   if -int(item.get("code", 0)) == injected_errno), None)
    if native is None:
        return {"status": "invalid_errno", "operation": operation}
    caller = str(matrix.get("caller_return_contract", ""))
    caller_behavior = {
        "caller_error_return": "hook_caller_returns_error",
        "helper_consumed_or_void": "hook_caller_consumes_error_or_is_void",
        "source_defined": "source_path_reviewed_with_recorded_syscall_result",
        "no_native_fault_return": "no_native_return_value",
    }.get(caller, "unknown")
    return {
        "status": "source_anchored" if definition.get("source") and
        definition.get("return_path") else "incomplete",
        "operation": operation,
        "kernel_operation": definition.get("kernel_operation"),
        "mutation_stage": matrix.get("mutation_stage"),
        "source_anchors": list(matrix.get("source_anchors", [])),
        "catalog_sources": list(definition.get("source", [])),
        "native_errno": {"name": native.get("name"),
                         "code": int(native.get("code", 0)),
                         "source": native.get("source")},
        "native_errno_choices": [
            {"name": item.get("name"), "code": int(item.get("code", 0)),
             "source": item.get("source")}
            for item in definition.get("native_errnos", [])],
        "hook_caller_contract": caller,
        "hook_caller_behavior": caller_behavior,
        "return_path": definition.get("return_path"),
        "syscall_return_policy": "record_only",
    }


def _catalog_plan(operation: str, errno_override: str | None = None) -> FaultPlan:
    """Create a deterministic fixture plan for every catalog hook.

    Hand-written plans retain their stronger hook-specific oracle contracts.
    This fallback derives the operation, native errno, action and formula
    list from the reviewed catalog/matrix, so every registered hook is also
    independently armable and traceable. Its formula state is deliberately
    ``unverified`` until a source-specific terminal oracle is available.
    """
    matrix = _catalog_matrix().get(operation)
    catalog = load_catalog()
    definition = catalog.get("hooks", {}).get(operation)
    if not matrix or not definition:
        raise SystemExit(f"unsupported formula fault plan: {operation}")
    native = definition["native_errnos"][0]
    if errno_override is not None:
        normalized = errno_override.upper()
        if normalized.startswith("-") and normalized[1:].isdigit():
            normalized = str(abs(int(normalized)))
        elif normalized.isdigit():
            normalized = str(int(normalized))
        selected = next((item for item in definition["native_errnos"]
                         if item["name"] == normalized or
                         str(item["code"]) == normalized), None)
        if selected is None:
            allowed = ", ".join(item["name"] for item in definition["native_errnos"])
            raise SystemExit(
                f"unsupported errno override {errno_override!r} for {operation}; "
                f"allowed: {allowed}")
        native = selected
    errno = -int(native["code"])
    formulas = tuple(str(item) for item in matrix.get("formula_ids", []))
    contract = str(matrix.get("caller_return_contract", "source_defined"))
    stage = str(matrix.get("mutation_stage", "source"))
    dirty = stage == "dirty"
    recovery = journal_abort_recovery(remount_counters="unverified") if dirty else clean_recovery()
    errno_name = str(native["name"])
    # Keep the generated plan faithful to the native helper contract.  The
    # evidence validator intentionally rejects a string such as
    # ``error-policy-dependent``: a fixture must say whether the selected
    # source path aborts the handle, returns an allocation error, or lets its
    # caller consume the error.
    semantics = {
        "injected_errno": errno_name,
        "journal_abort": False,
        "forced_shutdown": False,
        "caller_contract": ("source_path_reviewed" if contract == "source_defined"
                            else contract),
        "scope": f"catalog-generated-{operation}",
        "persistence_expectation": "source_path_must_be_checked",
    }
    write_access = (stage == "access" or stage == "delete" or operation in {
        "mballoc_mark", "mballoc_free_mark", "resize_extend_add_blocks",
        "rename_delete_old",
    })
    if dirty:
        semantics["journal_abort"] = False
        semantics["handle_abort"] = True
        semantics["ext4_error_handler"] = "ext4_handle_dirty_metadata_error"
        semantics["abort_scope"] = "current_handle"
    elif write_access:
        # ext4_journal_get_write_access_faultable() mirrors native JBD2
        # behavior: EIO aborts the journal; EROFS records/aborts this handle.
        if errno_name == "EIO":
            semantics["journal_abort"] = True
            semantics["abort_scope"] = "journal"
        elif errno_name == "EROFS":
            semantics["journal_abort"] = False
            semantics["handle_abort"] = True
            semantics["abort_scope"] = "current_handle"
        else:
            semantics["journal_abort"] = False
            semantics["source_return"] = True
    elif errno_name == "EIO":
        # Read, allocation and replay hooks return their native helper error;
        # they do not imply a JBD2 abort merely because errno is EIO.
        semantics["journal_abort"] = False
        semantics["source_return"] = True
    elif errno_name == "EROFS":
        semantics["journal_abort"] = False
        semantics["source_return"] = True
    elif errno_name == "ENOMEM":
        semantics["journal_abort"] = False
        semantics["source_return"] = True
        semantics["caller_contract"] = (
            "void_helper_discards_optional_cache_error"
            if contract == "helper_consumed_or_void" else
            "allocation_returns_error")
    elif errno_name == "ENOSPC":
        semantics["journal_abort"] = False
        semantics["source_return"] = True
        semantics["caller_contract"] = ("caller_consumed_fallback"
                                          if contract != "caller_error_return"
                                          else "write_returns_error")
    else:
        semantics["journal_abort"] = False
        semantics["source_return"] = True
    action_syscalls = {
        "alloc-mballoc": 285, "free-punch": 285, "mkdir": 258,
        "rmdir": 84, "unlink": 263, "rename": 316,
        "rename-exchange": 316, "rename-replace": 316, "rename-dir": 316,
        "rename-dir-inline": 316,
        "evict-close": 3, "evict-close-prev": 3,
        "xattr-evict-close": 3, "xattr-single-evict-close": 3,
        "extent-split": 285, "resize-extend": 16,
        "resize-add-group": 16, "migrate-extents": 16,
        "xattr-set": 188, "ea-ref-set": 188, "acl-mode": 188,
        "inline-dir": 258, "inline-file-write": 1,
        "fileattr-flags": 16, "fileattr-project": 16,
        "fast-commit-replay": 165,
    }
    action = str(matrix["fixture_action"])
    expected_formula_states = {formula: UNVERIFIED for formula in formulas}
    allowed_unverified = list(formulas)
    # These operations have a stable aggregate counter terminal event in the
    # single-worker fixture. Require that comparison instead of downgrading a
    # real, independently captured delta to ``unverified``.
    if operation in {"mkdir_add_entry_read", "mkdir_append_alloc"}:
        expected_formula_states["R-IDELTA"] = CONSISTENT
        if "R-IDELTA" in allowed_unverified:
            allowed_unverified.remove("R-IDELTA")
    if operation in {"mb_buddy_load", "group_add_groupinfo_alloc"}:
        expected_formula_states["R-BDELTA"] = CONSISTENT
        if "R-BDELTA" in allowed_unverified:
            allowed_unverified.remove("R-BDELTA")
    return FaultPlan(
        f"{operation}-{native['name'].lower()}", operation, action,
        action_syscalls.get(action, 0), 0, 0, "record_only", formulas,
        f"catalog_hook_{operation}", UNVERIFIED,
        expected_formula_states,
        {formula: () for formula in formulas}, recovery=recovery,
        expected_stable_states={
            "stable-before": {"valid": None, "relations_valid": None,
                              "namespace_valid": None},
            "stable-after": {"valid": None, "relations_valid": None,
                             "namespace_valid": None},
            "remount": {"valid": None, "relations_valid": None,
                        "namespace_valid": None}},
        allowed_unverified_formulas=tuple(allowed_unverified),
        error_semantics=semantics, injected_errno=errno)


def get_plan(name: str, errno_override: str | None = None) -> FaultPlan:
    if name in PLANS and errno_override is None:
        return PLANS[name]
    matrix = _catalog_matrix()
    if name in PLANS:
        return _catalog_plan(PLANS[name].operation, errno_override)
    if name in matrix:
        return _catalog_plan(name, errno_override)
    for operation, entry in matrix.items():
        if entry.get("fixture_id") == name:
            return _catalog_plan(operation, errno_override)
    raise SystemExit(f"unsupported formula fault plan: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fault_id", help="fault plan key, catalog operation, or fixture id")
    parser.add_argument("--output")
    parser.add_argument("--errno", help="select a source-listed native errno by name or number")
    args = parser.parse_args()
    payload = json.dumps(get_plan(args.fault_id, args.errno).as_json(), indent=2) + "\n"
    if args.output:
        with open(args.output, "w") as stream:
            stream.write(payload)
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
