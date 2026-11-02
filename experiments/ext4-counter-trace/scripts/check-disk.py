#!/usr/bin/env python3
"""Independent ext4 disk totals and inode-link checker.

This intentionally reads the on-disk format directly.  It is used on an
offline image after unmount/recovery and does not trust e2fsck output as its
expected value.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import struct
import sys


SB = 1024
EXT4_SUPER_MAGIC = 0xEF53
EXT4_FEATURE_INCOMPAT_64BIT = 0x80
EXT4_FEATURE_INCOMPAT_BIGALLOC = 0x200
EXT4_FEATURE_RO_COMPAT_GDT_CSUM = 0x0010
EXT4_FEATURE_RO_COMPAT_DIR_NLINK = 0x0020
EXT4_FEATURE_RO_COMPAT_METADATA_CSUM = 0x0400
EXT4_BG_INODE_UNINIT = 0x0001
EXT4_BG_BLOCK_UNINIT = 0x0002
EXT4_EXTENTS_FL = 0x00080000
EXT4_INLINE_DATA_FL = 0x10000000
EXT4_EA_INODE_FL = 0x00200000
EXT4_ROOT_INO = 2


class Image:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.fp = path.open("rb")
        self.sb = self.fp.read(SB + 1024)[SB:SB + 1024]
        if len(self.sb) != 1024 or self.u16(56) != EXT4_SUPER_MAGIC:
            raise ValueError("not an ext4 superblock")
        self.block_size = 1024 << self.u32(24)
        cluster_log = struct.unpack_from("<i", self.sb, 28)[0]
        self.cluster_size = 1024 << cluster_log
        self.bigalloc = bool(self.u32(96) & EXT4_FEATURE_INCOMPAT_BIGALLOC)
        self.cluster_ratio = self.cluster_size // self.block_size
        self.desc_size = max(32, self.u16(254))
        if not (self.u32(96) & EXT4_FEATURE_INCOMPAT_64BIT):
            self.desc_size = 32
        self.blocks = self.u32(4) | (self.u32(336) << 32)
        self.first_data_block = self.u32(20)
        if self.u32(96) & 0x10:
            raise ValueError("unsupported META_BG descriptor placement")
        self.inodes = self.u32(0)
        self.group_desc_csum = bool(self.u32(100) &
                                    (EXT4_FEATURE_RO_COMPAT_GDT_CSUM |
                                     EXT4_FEATURE_RO_COMPAT_METADATA_CSUM))
        self.blocks_per_group = self.u32(32)
        self.clusters_per_group = self.u32(36)
        self.inodes_per_group = self.u32(40)
        self.inode_size = self.u16(88)
        self.first_ino = self.u32(84)
        self.dir_nlink = bool(self.u32(100) & EXT4_FEATURE_RO_COMPAT_DIR_NLINK)
        if self.bigalloc:
            total_clusters = math.ceil((self.blocks-self.first_data_block) / self.cluster_ratio)
            self.groups = max(math.ceil(total_clusters / self.clusters_per_group),
                              math.ceil(self.inodes / self.inodes_per_group))
        else:
            self.groups = max(math.ceil((self.blocks-self.first_data_block) / self.blocks_per_group),
                              math.ceil(self.inodes / self.inodes_per_group))
        self.gdt_block = 2 if self.block_size == 1024 else 1
        self.gdt_offset = self.gdt_block * self.block_size

    def u16(self, off: int, data: bytes | None = None) -> int:
        return struct.unpack_from("<H", self.sb if data is None else data, off)[0]

    def u32(self, off: int, data: bytes | None = None) -> int:
        return struct.unpack_from("<I", self.sb if data is None else data, off)[0]

    def read_block(self, block: int) -> bytes:
        self.fp.seek(block * self.block_size)
        data = self.fp.read(self.block_size)
        if len(data) != self.block_size:
            raise ValueError(f"short block read {block}")
        return data

    def descriptor(self, group: int) -> bytes:
        self.fp.seek(self.gdt_offset + group * self.desc_size)
        data = self.fp.read(self.desc_size)
        if len(data) != self.desc_size:
            raise ValueError(f"short group descriptor {group}")
        return data

    def group_info(self, group: int) -> dict:
        d = self.descriptor(group)
        hi = self.desc_size >= 64
        get32 = lambda off: self.u32(off, d)
        get16 = lambda off: self.u16(off, d)
        block_bitmap = get32(0) | ((get32(32) << 32) if hi else 0)
        inode_bitmap = get32(4) | ((get32(36) << 32) if hi else 0)
        inode_table = get32(8) | ((get32(40) << 32) if hi else 0)
        free_blocks = get16(12) | ((get16(44) << 16) if hi else 0)
        free_inodes = get16(14) | ((get16(46) << 16) if hi else 0)
        used_dirs = get16(16) | ((get16(48) << 16) if hi else 0)
        itable_unused = get16(28) | ((get16(50) << 16) if hi else 0)
        return {"block_bitmap": block_bitmap, "inode_bitmap": inode_bitmap,
                "inode_table": inode_table, "free_blocks": free_blocks,
                "free_inodes": free_inodes, "used_dirs": used_dirs,
                "itable_unused": itable_unused, "flags": get16(18)}

    def valid_blocks(self, group: int) -> int:
        return min(self.blocks_per_group, self.blocks - self.first_data_block - group * self.blocks_per_group)

    def valid_clusters(self, group: int) -> int:
        blocks = self.valid_blocks(group)
        return math.ceil(blocks / self.cluster_ratio) if self.bigalloc else blocks

    def has_super(self, group: int) -> bool:
        if group == 0:
            return True
        if self.u32(92) & 0x200:  # sparse_super2
            return group in (self.u32(588), self.u32(592))
        if not self.u32(100) & 1:  # sparse_super
            return True
        if group == 1:
            return True
        for base in (3, 5, 7):
            value = group
            while value > 1 and value % base == 0:
                value //= base
            if value == 1:
                return True
        return False

    def uninit_free_clusters(self, group: int, info: dict) -> int:
        # Independent logical bitmap model, including flex placement, short
        # final groups and clusters shared by several metadata blocks. Disk
        # bytes in a BLOCK_UNINIT bitmap have no allocation authority.
        start = self.first_data_block + group * self.blocks_per_group
        end = start + self.valid_blocks(group)
        occupied = set()
        def mark(block):
            if start <= block < end:
                occupied.add((block-start) // self.cluster_ratio)
        if self.has_super(group):
            base = 1 + math.ceil(self.groups*self.desc_size/self.block_size) + self.u16(206)
            for block in range(start, min(start+base, end)):
                mark(block)
        mark(info['block_bitmap'])
        mark(info['inode_bitmap'])
        table_blocks = math.ceil(self.inodes_per_group*self.inode_size/self.block_size)
        for block in range(info['inode_table'], info['inode_table']+table_blocks):
            mark(block)
        return self.valid_clusters(group)-len(occupied)

    def valid_inodes(self, group: int) -> int:
        return min(self.inodes_per_group, self.inodes - group * self.inodes_per_group)

    @staticmethod
    def zero_bits(data: bytes, count: int) -> int:
        used = sum(byte.bit_count() for byte in data[: (count + 7) // 8])
        if count & 7:
            used -= (data[count // 8] >> (count & 7)).bit_count()
        return count - used

    def bitmap_free(self, block: int, count: int) -> int:
        return self.zero_bits(self.read_block(block), count)

    def inode_raw(self, group: int, index: int) -> bytes:
        info = self.group_info(group)
        byte_offset = index * self.inode_size
        block = info["inode_table"] + byte_offset // self.block_size
        off = byte_offset % self.block_size
        data = self.read_block(block)
        return data[off:off + self.inode_size]

    def inode_extents(self, raw: bytes):
        if len(raw) < 100 or not (struct.unpack_from("<I", raw, 32)[0] & EXT4_EXTENTS_FL):
            raise ValueError("inode has no extent tree")

        def walk(node: bytes, expected_depth=None):
            magic, entries, max_entries, depth = struct.unpack_from("<HHHH", node, 0)
            if (magic != 0xF30A or entries > max_entries or depth > 5 or
                    12*(entries+1) > len(node) or expected_depth is not None and depth != expected_depth):
                raise ValueError("bad extent header")
            if depth == 0:
                for n in range(entries):
                    off = 12 * (n + 1)
                    logical, length = struct.unpack_from("<I H", node, off)
                    start_hi = struct.unpack_from("<H", node, off + 6)[0]
                    start_lo = struct.unpack_from("<I", node, off + 8)[0]
                    physical = (start_hi << 32) | start_lo
                    length &= 0x7FFF
                    for i in range(length):
                        yield logical + i, physical + i
            else:
                for n in range(entries):
                    off = 12 * (n + 1)
                    logical = struct.unpack_from("<I", node, off)[0]
                    child = struct.unpack_from("<I", node, off + 4)[0]
                    child |= struct.unpack_from("<H", node, off + 8)[0] << 32
                    yield from walk(self.read_block(child), depth-1)

        yield from walk(raw[40:100])

    def inode_data_blocks(self, raw: bytes):
        size = struct.unpack_from("<I", raw, 4)[0]
        if len(raw) >= 112:
            size |= struct.unpack_from("<I", raw, 108)[0] << 32
        needed = math.ceil(size / self.block_size)
        for logical, physical in self.inode_extents(raw):
            if logical < needed:
                yield logical, physical

    def directory_region_refs(self, directory: int, data: bytes):
        """Each inline region has its own rec_len boundary; never concatenate."""
        refs, pos = [], 0
        while pos < len(data):
            if pos + 8 > len(data):
                raise ValueError(f"short directory entry inode {directory}")
            ino, rec_len, name_len, file_type = struct.unpack_from("<I H B B", data, pos)
            if rec_len < 8 or rec_len % 4 or pos + rec_len > len(data) or name_len > rec_len-8:
                raise ValueError(f"bad directory entry inode {directory}")
            if ino:
                if ino > self.inodes or not name_len:
                    raise ValueError(f"bad directory target inode {directory}")
                name = data[pos+8:pos+8+name_len]
                refs.append((ino, name.decode("utf-8", "replace"), file_type))
            pos += rec_len
        return refs

    def inline_xattr(self, raw: bytes):
        if len(raw) < 132:
            raise ValueError("short inline inode")
        extra = self.u16(128, raw)
        header = 128 + extra
        if extra < 4 or header + 8 > len(raw) or self.u32(header, raw) != 0xEA020000:
            raise ValueError("missing inline ibody xattr header")
        first = pos = header + 4
        found, values = None, []
        while pos + 4 <= len(raw) and self.u32(pos, raw):
            if pos + 16 > len(raw):
                raise ValueError("short inline xattr entry")
            name_len, index, offset, value_inode, size, _hash = struct.unpack_from("<BBHIII", raw, pos)
            end = pos + ((16 + name_len + 3) & ~3)
            if end + 4 > len(raw):
                raise ValueError("bad inline xattr name")
            if index == 7 and raw[pos+16:pos+16+name_len] == b'data':
                if found is not None or value_inode:
                    raise ValueError("duplicate or external inline system.data")
                start = first + offset
                if size and (start % 4 or start + size > len(raw)):
                    raise ValueError("bad inline system.data bounds")
                found = raw[start:start+size] if size else b''
                if size:
                    values.append((start, start+size))
            pos = end
        if pos + 4 > len(raw) or self.u32(pos, raw) or found is None:
            raise ValueError("missing inline system.data or xattr terminator")
        if any(start < pos + 4 for start, _end in values):
            raise ValueError("inline system.data overlaps xattr entries")
        return found

    def directory_refs(self, directory: int, raw: bytes):
        refs = []
        if self.u32(32, raw) & EXT4_INLINE_DATA_FL:
            parent = self.u32(40, raw)
            if not 1 <= parent <= self.inodes:
                raise ValueError(f"bad inline parent inode {directory}")
            refs += [(directory, '.', 2), (parent, '..', 2)]
            refs += self.directory_region_refs(directory, raw[44:100])
            refs += self.directory_region_refs(directory, self.inline_xattr(raw))
            return refs
        for logical, physical in self.inode_data_blocks(raw):
            data = self.read_block(physical)
            refs += self.directory_region_refs(directory, data)
        return refs

    def check(self) -> dict:
        groups = []
        disk_free_clusters = disk_free_inodes = disk_dirs = 0
        link_refs = {}
        allocated = {}
        lifecycle_mismatches = []
        unverified = []
        link_unverified = []
        itable_mismatches = []
        itable_checked_groups = 0
        itable_uninit_groups = 0
        itable_unsupported_groups = 0
        for group in range(self.groups):
            info = self.group_info(group)
            flags = info["flags"]
            valid_c = self.valid_clusters(group)
            valid_i = self.valid_inodes(group)
            if flags & EXT4_BG_BLOCK_UNINIT:
                logical_c = self.uninit_free_clusters(group, info)
            else:
                logical_c = self.bitmap_free(info["block_bitmap"], valid_c)
            inode_uninit = bool(flags & EXT4_BG_INODE_UNINIT)
            itable_unused = info["itable_unused"]
            itable_prefix = (self.inodes_per_group - itable_unused
                             if self.group_desc_csum else None)
            itable_highwater_valid = True
            first_allocated_past_prefix = None
            if not self.group_desc_csum:
                itable_unsupported_groups += 1
            elif itable_unused > self.inodes_per_group:
                itable_highwater_valid = False
                itable_mismatches.append({"group": group,
                                          "itable_unused": itable_unused,
                                          "inodes_per_group": self.inodes_per_group,
                                          "reason": "unused_count_exceeds_group"})
            logical_i = valid_i if inode_uninit else self.bitmap_free(
                info["inode_bitmap"], valid_i)
            inode_count = 0
            dir_count = 0
            if inode_uninit:
                # Linux clears/ignores the inode table of an INODE_UNINIT group
                # until allocation, so every valid slot is logically free.
                itable_uninit_groups += 1
            else:
                if self.group_desc_csum:
                    itable_checked_groups += 1
                bitmap = self.read_block(info["inode_bitmap"])
                for index in range(valid_i):
                    if bitmap[index // 8] & (1 << (index & 7)):
                        if (self.group_desc_csum and itable_highwater_valid and
                                index >= itable_prefix and
                                first_allocated_past_prefix is None):
                            first_allocated_past_prefix = index
                        ino = group * self.inodes_per_group + index + 1
                        raw = self.inode_raw(group, index)
                        mode, links = struct.unpack_from("<H", raw, 0)[0], struct.unpack_from("<H", raw, 26)[0]
                        dtime = struct.unpack_from("<I", raw, 20)[0]
                        flags_raw = struct.unpack_from("<I", raw, 32)[0]
                        if mode:
                            inode_count += 1
                            allocated[ino] = (mode, links, flags_raw, raw)
                            if (ino >= self.first_ino and links == 0 and
                                    dtime == 0):
                                lifecycle_mismatches.append({
                                    "ino": ino,
                                    "stored_nlink": links,
                                    "dtime": dtime,
                                    "valid": False,
                                })
                            if mode & 0xF000 == 0x4000:
                                dir_count += 1
                if first_allocated_past_prefix is not None:
                    itable_highwater_valid = False
                    itable_mismatches.append({
                        "group": group,
                        "itable_unused": itable_unused,
                        "initialized_prefix": itable_prefix,
                        "allocated_index": first_allocated_past_prefix,
                        "reason": "allocated_inode_beyond_initialized_prefix"})
            disk_free_clusters += info["free_blocks"]
            disk_free_inodes += info["free_inodes"]
            disk_dirs += info["used_dirs"]
            groups.append({"group": group, "flags": flags,
                           "inode_uninit": inode_uninit,
                           "descriptor_free_clusters": info["free_blocks"],
                           "bitmap_free_clusters": logical_c,
                           "descriptor_free_inodes": info["free_inodes"],
                           "bitmap_free_inodes": logical_i,
                           "itable_unused": itable_unused,
                           "itable_initialized_prefix": itable_prefix,
                           "itable_highwater_checked": self.group_desc_csum and
                               not inode_uninit,
                           "itable_highwater_valid": itable_highwater_valid,
                           "descriptor_dirs": info["used_dirs"],
                           "inode_dirs": dir_count, "allocated_inodes": inode_count})

        for ino, (mode, links, flags_raw, raw) in allocated.items():
            if not (mode & 0xF000 == 0x4000):
                continue
            # A directory whose link count is already zero is an unlinked
            # directory in the inode table.  Its inline or data blocks may
            # still contain stale `.`/`..` records until inode eviction
            # completes; those records are not namespace references.  Keep
            # scanning other directories so an actual external reference to
            # this inode remains visible to R-LINK.
            if links == 0:
                continue
            try:
                for target, _name, _file_type in self.directory_refs(ino, raw):
                    link_refs[target] = link_refs.get(target, 0) + 1
            except ValueError as exc:
                link_unverified.append(f"directory {ino}: {exc}")

        # Link-count validation only needs directory contents.  A regular or
        # special file without the EXTENTS flag is not an error here.
        referenced = set(link_refs)

        link_checks = []
        link_mismatches = []
        for ino, (mode, links, flags_raw, _raw) in sorted(allocated.items()):
            if ino < self.first_ino and ino != EXT4_ROOT_INO:
                continue
            if flags_raw & EXT4_EA_INODE_FL:
                continue
            if not (mode & 0xF000 == 0x4000) and ino not in referenced:
                # Only ordinary name entries can be verified without loading
                # VFS state.  Special inodes are outside the formula domain.
                if mode & 0xF000 in (0x8000, 0xA000, 0x6000, 0x2000, 0x1000):
                    continue
            # dir_nlink stores 1 for a directory whose real link count is
            # greater than the field can represent; it is a sentinel, not a
            # count that can be compared to ordinary directory entries.
            if self.dir_nlink and mode & 0xF000 == 0x4000 and links == 1:
                continue
            # An open-unlinked inode legitimately has no namespace entry.
            if links == 0 and not link_refs.get(ino, 0):
                continue
            observed = link_refs.get(ino, 0)
            item = {"ino": ino, "stored": links, "directory_refs": observed,
                    "valid": links == observed}
            link_checks.append(item)
            if not item["valid"]:
                link_mismatches.append(item)

        sb_free_blocks = self.u32(12) | (self.u32(344) << 32)
        # s_free_inodes_count is a single 32-bit field; the 64-bit feature
        # extends block counts and descriptor fields, not this counter.
        sb_free_inodes = self.u32(16)
        # s_free_blocks_count is stored in blocks.  Descriptor
        # bg_free_blocks_count is in clusters when BIGALLOC is enabled.
        sb_free_clusters = sb_free_blocks // self.cluster_ratio
        descriptor_ok = (sb_free_clusters == disk_free_clusters and
                         sb_free_inodes == disk_free_inodes)
        group_ok = all(g["bitmap_free_clusters"] is None or
                       g["bitmap_free_clusters"] == g["descriptor_free_clusters"]
                       for g in groups)
        inode_group_ok = all(g["bitmap_free_inodes"] == g["descriptor_free_inodes"]
                             for g in groups)
        dir_group_ok = all(g["inode_dirs"] == g["descriptor_dirs"] for g in groups)
        itable_highwater_ok = all(g["itable_highwater_valid"] for g in groups)
        link_valid = not link_mismatches and not link_unverified
        block_uninit = any("BLOCK_UNINIT" in x for x in unverified)
        lifecycle_valid = not lifecycle_mismatches
        disk_valid = (descriptor_ok and group_ok and inode_group_ok and dir_group_ok and
                      itable_highwater_ok and lifecycle_valid and not block_uninit)
        return {"valid": disk_valid and link_valid,
                "status": ("unverified" if block_uninit or link_unverified else
                           "verified" if disk_valid and link_valid else "mismatch"),
                "image": str(self.path), "block_size": self.block_size,
                "cluster_size": self.cluster_size, "cluster_ratio": self.cluster_ratio,
                "descriptor_size": self.desc_size,
                "groups": groups,
                "superblock": {"free_blocks": sb_free_blocks,
                                "free_block_units": "block",
                                "free_inodes": sb_free_inodes,
                                "free_clusters": sb_free_clusters},
                "descriptor_totals": {"free_clusters": disk_free_clusters,
                                       "free_inodes": disk_free_inodes, "dirs": disk_dirs},
                "bitmap_descriptor_match": group_ok and inode_group_ok,
                "superblock_descriptor_match": descriptor_ok,
                "r_ug": {"checked": True,
                         "stored": disk_dirs,
                         "inode_scan": sum(g["inode_dirs"] for g in groups),
                         "valid": dir_group_ok,
                         "unverified_reason": None},
                "r_itable_highwater": {
                    "checked": itable_checked_groups > 0,
                    "checked_groups": itable_checked_groups,
                    "inode_uninit_groups_skipped": itable_uninit_groups,
                    "unsupported_groups_skipped": itable_unsupported_groups,
                    "mismatches": itable_mismatches,
                    "valid": itable_highwater_ok},
                "r_inode_lifecycle": {
                    "checked": True,
                    "mismatches": lifecycle_mismatches,
                    "valid": lifecycle_valid},
                "r_link": {"checked": True, "checked_inodes": len(link_checks),
                           "mismatches": link_mismatches, "valid": link_valid,
                           "unverified": link_unverified},
                "unverified": unverified + link_unverified}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    try:
        report = Image(args.image).check()
    except (OSError, ValueError, struct.error) as exc:
        report = {"valid": False, "status": "error", "image": str(args.image),
                  "error": str(exc)}
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")
    return 0 if report.get("valid") else 1


if __name__ == "__main__":
    sys.exit(main())
