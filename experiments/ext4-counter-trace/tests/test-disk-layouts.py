#!/usr/bin/env python3
"""Independent inline regions, malformed xattrs and extent-index disk layouts."""
import importlib.util
import pathlib
import struct
import unittest

spec=importlib.util.spec_from_file_location('checker',pathlib.Path(__file__).resolve().parents[1]/'scripts/check-disk.py')
checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)


class DiskLayouts(unittest.TestCase):
    def setUp(self):
        self.view=object.__new__(checker.Image)
        self.view.inodes=100
        self.view.block_size=4096
        self.raw=bytearray(256)
        struct.pack_into('<I',self.raw,32,checker.EXT4_INLINE_DATA_FL)
        struct.pack_into('<I',self.raw,40,2)
        struct.pack_into('<IHBB',self.raw,44,13,56,1,1)
        self.raw[52]=ord('a')
        struct.pack_into('<H',self.raw,128,32)
        struct.pack_into('<I',self.raw,160,0xEA020000)
        struct.pack_into('<BBHIII',self.raw,164,4,7,44,0,24,0)
        self.raw[180:184]=b'data'
        struct.pack_into('<IHBB',self.raw,208,14,24,1,1)
        self.raw[216]=ord('b')

    def test_both_inline_regions_and_synthetic_dots(self):
        self.assertEqual(self.view.directory_refs(12,bytes(self.raw)),
                         [(12,'.',2),(2,'..',2),(13,'a',1),(14,'b',1)])

    def test_inline_second_region_is_not_required_when_value_empty(self):
        struct.pack_into('<I',self.raw,172,0)
        self.assertEqual(len(self.view.directory_refs(12,bytes(self.raw))),3)

    def test_inline_region_cannot_cross_into_the_other_area(self):
        struct.pack_into('<H',self.raw,48,60)
        with self.assertRaises(ValueError):self.view.directory_refs(12,bytes(self.raw))

    def test_xattr_bounds_overlap_and_external_inode_rejected(self):
        for offset,size,inode in [(100,24,0),(0,24,0),(44,24,3)]:
            raw=bytearray(self.raw)
            struct.pack_into('<HII',raw,166,offset,inode,size)
            with self.assertRaises(ValueError):self.view.directory_refs(12,bytes(raw))

    def test_invalid_inline_parent_and_dirent_name_rejected(self):
        raw=bytearray(self.raw);struct.pack_into('<I',raw,40,101)
        with self.assertRaises(ValueError):self.view.directory_refs(12,bytes(raw))
        raw=bytearray(self.raw);raw[50]=255
        with self.assertRaises(ValueError):self.view.directory_refs(12,bytes(raw))

    def test_extent_index_uses_leaf_lo_at_four_and_hi_at_eight(self):
        raw=bytearray(256);struct.pack_into('<I',raw,32,checker.EXT4_EXTENTS_FL)
        struct.pack_into('<HHHHI',raw,40,0xF30A,1,4,1,0)
        struct.pack_into('<IIHH',raw,52,0,17,0,0)
        child=bytearray(4096)
        struct.pack_into('<HHHHI',child,0,0xF30A,1,340,0,0)
        struct.pack_into('<IHHI',child,12,0,1,0,99)
        seen=[]
        def read(block):
            seen.append(block);return bytes(child)
        self.view.read_block=read
        self.assertEqual(list(self.view.inode_extents(bytes(raw))),[(0,99)])
        self.assertEqual(seen,[17])

    def test_cyclic_depth_rejected(self):
        raw=bytearray(256);struct.pack_into('<I',raw,32,checker.EXT4_EXTENTS_FL)
        struct.pack_into('<HHHHI',raw,40,0xF30A,1,4,1,0)
        struct.pack_into('<IIHH',raw,52,0,17,0,0)
        self.view.read_block=lambda block:bytes(raw[40:100])
        with self.assertRaises(ValueError):list(self.view.inode_extents(bytes(raw)))


if __name__=='__main__':unittest.main()
