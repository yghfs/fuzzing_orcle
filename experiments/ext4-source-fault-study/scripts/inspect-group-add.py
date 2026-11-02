#!/usr/bin/env python3
"""Read the 4 KiB ext4 superblock and group descriptors used in this study."""

import argparse
import math
import struct


def u16(data, offset):
    return struct.unpack_from("<H", data, offset)[0]


def u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def descriptor(data, offset):
    return {
        "block_bitmap": u32(data, offset) | (u32(data, offset + 32) << 32),
        "inode_bitmap": u32(data, offset + 4) | (u32(data, offset + 36) << 32),
        "inode_table": u32(data, offset + 8) | (u32(data, offset + 40) << 32),
        "free_blocks": u16(data, offset + 12) | (u16(data, offset + 44) << 16),
        "free_inodes": u16(data, offset + 14) | (u16(data, offset + 46) << 16),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    args = parser.parse_args()

    with open(args.image, "rb") as image:
        image.seek(1024)
        sb = image.read(1024)
        block_size = 1024 << u32(sb, 24)
        assert block_size == 4096, f"expected 4096-byte blocks, got {block_size}"
        desc_size = u16(sb, 254) or 32
        assert desc_size == 64, f"expected 64-byte descriptors, got {desc_size}"
        first_data_block = u32(sb, 20)
        blocks_per_group = u32(sb, 32)
        blocks_count = u32(sb, 4) | (u32(sb, 336) << 32)
        free_blocks = u32(sb, 12) | (u32(sb, 344) << 32)
        inodes_count = u32(sb, 0)
        free_inodes = u32(sb, 16)
        groups = math.ceil((blocks_count - first_data_block) / blocks_per_group)
        gdt_start = (first_data_block + 1) * block_size
        image.seek(gdt_start)
        gdt = image.read(2 * desc_size)

    group0 = descriptor(gdt, 0)
    group1 = descriptor(gdt, desc_size)
    print(f"super.blocks_count={blocks_count}")
    print(f"super.free_blocks={free_blocks}")
    print(f"super.inodes_count={inodes_count}")
    print(f"super.free_inodes={free_inodes}")
    print(f"super.groups_from_blocks={groups}")
    for group_no, group in enumerate((group0, group1)):
        print(
            f"group{group_no}.block_bitmap={group['block_bitmap']} "
            f"inode_bitmap={group['inode_bitmap']} "
            f"inode_table={group['inode_table']} "
            f"free_blocks={group['free_blocks']} "
            f"free_inodes={group['free_inodes']}"
        )
    print(f"descriptor_sum.free_blocks={group0['free_blocks'] + group1['free_blocks']}")
    print(f"descriptor_sum.free_inodes={group0['free_inodes'] + group1['free_inodes']}")


if __name__ == "__main__":
    main()
