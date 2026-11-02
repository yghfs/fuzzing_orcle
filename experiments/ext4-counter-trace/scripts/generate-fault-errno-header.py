#!/usr/bin/env python3
"""Generate the kernel errno whitelist from fault-errno-catalog.json."""
import argparse
import pathlib
import sys

from fault_errno_catalog import PROJECT, generate_kernel_header

path = PROJECT / 'dev/linux-6.12.1/fs/ext4/fault_errno_catalog.h'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
expected = generate_kernel_header()
if args.check:
    if not path.is_file() or path.read_text() != expected:
        print('fault_errno_catalog_header_out_of_date', file=sys.stderr)
        sys.exit(1)
else:
    path.write_text(expected)
    print(path)
