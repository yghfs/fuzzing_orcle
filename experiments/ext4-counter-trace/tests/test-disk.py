#!/usr/bin/env python3
"""Negative checks for the independent offline ext4 checker."""
import importlib.util
import json
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile

project = pathlib.Path(__file__).resolve().parents[1]
checker = project / "scripts/check-disk.py"
spec = importlib.util.spec_from_file_location("disk_checker", checker)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
source = pathlib.Path(sys.argv[1])
assert subprocess.run([sys.executable, str(checker), str(source)],
                      capture_output=True).returncode == 0
with tempfile.TemporaryDirectory() as temp:
    image = pathlib.Path(temp) / source.name
    shutil.copy2(source, image)
    data = bytearray(image.read_bytes())
    view = module.Image(image)
    if view.groups > 1:
        # Opposite descriptor directory-count errors must not cancel globally.
        original = bytes(data)
        import struct
        first = view.gdt_offset + 16
        second = first + view.desc_size
        a, b = struct.unpack_from('<H', data, first)[0], struct.unpack_from('<H', data, second)[0]
        if a:
            struct.pack_into('<H', data, first, a-1)
            struct.pack_into('<H', data, second, b+1)
            image.write_bytes(data)
            result = subprocess.run([sys.executable, str(checker), str(image)], capture_output=True, text=True)
            assert result.returncode != 0 and not json.loads(result.stdout)['r_ug']['valid']
        data = bytearray(original)
        image.write_bytes(data)
    # A deleted inode that remains allocated with no deletion timestamp is
    # the on-disk signature of an incomplete eviction.  It must be rejected
    # independently of directory link scanning.
    lifecycle_image = pathlib.Path(temp) / ("lifecycle-" + source.name)
    shutil.copy2(source, lifecycle_image)
    lifecycle_view = module.Image(lifecycle_image)
    lifecycle_info = lifecycle_view.group_info(0)
    lifecycle_inode = (lifecycle_info["inode_table"] * lifecycle_view.block_size +
                       (11 - 1) * lifecycle_view.inode_size)
    lifecycle_data = bytearray(lifecycle_image.read_bytes())
    struct.pack_into("<H", lifecycle_data, lifecycle_inode, 0x4000)
    struct.pack_into("<H", lifecycle_data, lifecycle_inode + 26, 0)
    struct.pack_into("<I", lifecycle_data, lifecycle_inode + 20, 0)
    lifecycle_image.write_bytes(lifecycle_data)
    result = subprocess.run([sys.executable, str(checker), str(lifecycle_image)],
                            capture_output=True, text=True)
    lifecycle_report = json.loads(result.stdout)
    assert result.returncode != 0
    assert not lifecycle_report["r_inode_lifecycle"]["valid"]
    # A cluster-ratio-sized change survives the bigalloc conversion.
    data[1024 + 12] ^= module.Image(image).cluster_ratio
    image.write_bytes(data)
    result = subprocess.run([sys.executable, str(checker), str(image)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert not json.loads(result.stdout)["superblock_descriptor_match"]
    view = module.Image(image)
    info = view.group_info(0)
    root_offset = info["inode_table"] * view.block_size + view.inode_size + 26
    data[root_offset] ^= 1
    image.write_bytes(data)
    result = subprocess.run([sys.executable, str(checker), str(image)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert not json.loads(result.stdout)["r_link"]["valid"]
    highwater_image = pathlib.Path(temp) / ("highwater-" + source.name)
    shutil.copy2(source, highwater_image)
    highwater_view = module.Image(highwater_image)
    descriptor = highwater_view.gdt_offset
    ipg = highwater_view.inodes_per_group
    assert highwater_view.group_desc_csum
    highwater_view.fp.close()
    data = bytearray(highwater_image.read_bytes())
    struct.pack_into("<H", data, descriptor + 28, ipg & 0xffff)
    if highwater_view.desc_size >= 64:
        struct.pack_into("<H", data, descriptor + 50, ipg >> 16)
    highwater_image.write_bytes(data)
    result = subprocess.run([sys.executable, str(checker), str(highwater_image)],
                            capture_output=True, text=True)
    highwater_report = json.loads(result.stdout)
    assert result.returncode != 0
    assert not highwater_report["r_itable_highwater"]["valid"]
print("disk_checker_negative_checks=passed superblock_free_blocks root_links itable_highwater")
