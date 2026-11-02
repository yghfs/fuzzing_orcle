#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import re
import sys
import unittest

PROJECT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
import hook_formula_matrix as matrix  # noqa: E402


class HookFormulaMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = json.loads((PROJECT / "fault-errno-catalog.json").read_text())

    def test_dirty_metadata_errno_uses_dirty_stage(self) -> None:
        for operation, definition in self.catalog["hooks"].items():
            if definition["native_errnos"][0]["name"] == "EFSCORRUPTED":
                with self.subTest(operation=operation):
                    self.assertEqual(matrix.mutation_stage(operation), "dirty")

    def test_directory_rename_prepare_uses_directory_fixture(self) -> None:
        self.assertEqual(matrix.fixture_action("rename_dir_prepare_access"),
                         "rename-dir")

    def test_every_source_anchor_names_a_symbol(self) -> None:
        for operation, definition in self.catalog["hooks"].items():
            with self.subTest(operation=operation):
                self.assertTrue(definition["source"])
                self.assertTrue(all(":" in anchor
                                    for anchor in definition["source"]))

    def test_fast_commit_claim_fault_precedes_bitmap_read(self) -> None:
        source = (PROJECT / "dev/linux-6.12.1/fs/ext4/ialloc.c").read_text()
        match = re.search(r"int ext4_mark_inode_used\(.*?\n}\n", source, re.S)
        self.assertIsNotNone(match)
        body = match.group(0)
        fault = body.index("EXT4_FAULT_FAST_COMMIT_INODE_CLAIM")
        bitmap_read = body.index("ext4_read_inode_bitmap(sb, group)")
        self.assertLess(fault, bitmap_read)
        self.assertIn("inode_bitmap_bh = err ? ERR_PTR(err)", body)

    def test_inline_write_begin_documents_both_source_states(self) -> None:
        source = (PROJECT / "dev/linux-6.12.1/fs/ext4/inline.c").read_text()
        conversion_start = source.index(
            "static int ext4_convert_inline_data_to_extent(")
        delayed_start = source.index(
            "static int ext4_da_convert_inline_data_to_extent(", conversion_start)
        conversion = source[conversion_start:delayed_start]
        delayed_end = source.index("/*\n * Prepare the write for the inline data.",
                                   delayed_start)
        delayed = source[delayed_start:delayed_end]
        self.assertLess(conversion.index("ext4_destroy_inline_data_nolock"),
                        conversion.index("ext4_block_write_begin_faultable"))
        self.assertNotIn("ext4_destroy_inline_data_nolock", delayed)
        self.assertIn("ext4_block_write_begin_faultable", delayed)

    def test_extent_status_allocation_error_is_consumed_by_caller(self) -> None:
        definition = self.catalog["hooks"]["es_insert_alloc"]
        self.assertEqual(matrix.return_contract(definition),
                         "helper_consumed_or_void")
        self.assertIn("does not retry", definition["return_path"])
        self.assertIn("does not retry this allocation or propagate an error",
                      definition["return_path"])
        source = (PROJECT / "dev/linux-6.12.1/fs/ext4/extents_status.c").read_text()
        start = source.index("void ext4_es_insert_extent(")
        end = source.index("void ext4_es_cache_extent(", start)
        body = source[start:end]
        self.assertLess(body.index("__es_remove_extent("),
                        body.index("__es_insert_extent("))
        self.assertIn("err2 == -ENOMEM && !ext4_es_must_keep(&newes)", body)

    def test_inode_evict_group_desc_fault_preserves_source_bitmap_transition(self) -> None:
        source = (PROJECT / "dev/linux-6.12.1/fs/ext4/ialloc.c").read_text()
        match = re.search(r"void ext4_free_inode\(.*?\n}\n", source, re.S)
        self.assertIsNotNone(match)
        body = match.group(0)
        fault = body.index("EXT4_FAULT_INODE_EVICT_GROUP_DESC_ACCESS")
        transition = body.index("cleared = ext4_test_and_clear_bit", fault)
        self.assertLess(fault, transition)
        self.assertIn("if (fatal || !cleared)", body[transition:])
        self.assertNotRegex(body[fault:transition], r"if\s*\(fatal\)\s*goto error_return")

    def test_migration_hook_keeps_unconditional_dirty_call_and_credit_budget(self) -> None:
        dev_path = PROJECT / "dev/linux-6.12.1/fs/ext4/migrate.c"
        pristine_path = PROJECT / "kernel/linux-6.12.1/fs/ext4/migrate.c"
        dev = dev_path.read_text()
        pristine = pristine_path.read_text()
        function_pattern = r"static int ext4_ext_swap_inode_data\(.*?\n}\n"
        dev_match = re.search(function_pattern, dev, re.S)
        self.assertIsNotNone(dev_match)
        swap = dev_match.group(0)
        self.assertRegex(
            swap,
            r"retval = free_ind_block\(handle, inode, i_data\);\s*"
            r"retval2 = ext4_mark_inode_dirty_faultable\(",
        )
        after_free = swap[swap.index("retval = free_ind_block("):]
        self.assertNotIn("ext4_journal_ensure_credits", after_free)

        def migration_credits(source: str) -> list[str]:
            body = re.search(
                r"(?:static )?int (?:ct_ext_migrate_impl|ext4_ext_migrate)\(.*?\n}\n",
                source, re.S)
            self.assertIsNotNone(body)
            return [re.sub(r"\s+", " ", item).strip()
                    for item in re.findall(
                        r"ext4_journal_start\(inode, EXT4_HT_MIGRATE,\s*(.*?)\);",
                        body.group(0), re.S)]

        self.assertEqual(migration_credits(dev), migration_credits(pristine))


if __name__ == "__main__":
    unittest.main()
