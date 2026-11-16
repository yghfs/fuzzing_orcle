#!/usr/bin/env python3
"""Bounded validation harness: fault only segment zero, then reset and healthy work.

Pass normal campaign_runner arguments, including --random-fault-injection.
Use a disposable QEMU campaign, a reviewed hook, and at least three segments.
An injected finding remains a finding: this harness does not override reports.
"""
import pathlib
import sys

PROJECT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'scripts'))
import campaign_runner
from campaign import atomic_json, read_json


def main():
    original = campaign_runner.Runner.new_segment

    def first_segment_fault(self, index):
        folder = original(self, index)
        if index:
            manifest = read_json(folder / 'manifest.json')
            manifest.pop('fault_injection', None)
            manifest['validation_fault_schedule'] = 'fault_only_in_segment_0'
            atomic_json(folder / 'manifest.json', manifest)
        return folder

    campaign_runner.Runner.new_segment = first_segment_fault
    try:
        return campaign_runner.main()
    finally:
        campaign_runner.Runner.new_segment = original


if __name__ == '__main__':
    sys.exit(main())
