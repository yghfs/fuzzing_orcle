"""Logical-byte quota ledger: sealed subtrees are scanned only on reconciliation."""
from __future__ import annotations
import os
import pathlib
import time


class QuotaLedger:
    def __init__(self, root, interval=300):
        self.root = pathlib.Path(root)
        self.interval = interval
        self.frozen = {}
        self.frozen_logical = self.frozen_allocated = 0
        self.active_segments = set((self.root / 'segments').glob('segment-*'))
        self.logical = self.allocated = 0
        self.full_scans = self.full_scan_files = self.routine_scans = self.routine_scan_files = 0
        self.full_scan_seconds = self.routine_scan_seconds = 0.0
        self.corrections = []
        self.last_reconcile = 0.0
        self.reconcile()

    def scan(self, path, excluded=()):
        logical = allocated = files = 0
        for base, dirs, names in os.walk(path):
            dirs[:] = [n for n in dirs if pathlib.Path(base) / n not in excluded]
            for name in names:
                try:
                    st = (pathlib.Path(base) / name).stat()
                except FileNotFoundError:
                    continue
                logical += st.st_size
                allocated += st.st_blocks * 512
                files += 1
        return logical, allocated, files

    def activate(self, folder):
        self.active_segments.add(pathlib.Path(folder))

    def seal(self, folder):
        folder = pathlib.Path(folder)
        old = self.frozen.get(folder, (0, 0))
        logical, allocated, _ = self.scan(folder)
        self.frozen[folder] = (logical, allocated)
        self.frozen_logical += logical - old[0]
        self.frozen_allocated += allocated - old[1]
        self.active_segments.discard(folder)
        self.refresh()

    def refresh(self):
        start = time.monotonic()
        # Root files (logs, sparse images, atomic replacement temporaries) and
        # active segments are mutable. Never enumerate the sealed segment tree.
        excluded = set(self.frozen) | {self.root / 'segments'}
        logical, allocated, files = self.scan(self.root, excluded)
        for folder in self.active_segments:
            size, blocks, count = self.scan(folder)
            logical += size; allocated += blocks; files += count
        self.logical = logical + self.frozen_logical
        self.allocated = allocated + self.frozen_allocated
        self.routine_scans += 1
        self.routine_scan_files += files
        self.routine_scan_seconds += time.monotonic() - start
        return self.logical

    def reconcile(self):
        start = time.monotonic()
        before = self.refresh()
        logical, allocated, files = self.scan(self.root)
        if before != logical:
            self.corrections.append(dict(logical_before=before, logical_after=logical,
                                         delta=logical-before))
        # Correct stale frozen subtrees as well as totals, otherwise the next
        # routine refresh would reintroduce the discrepancy.
        for folder in self.frozen:
            size, blocks, _ = self.scan(folder)
            self.frozen[folder] = (size, blocks)
        self.frozen_logical = sum(v[0] for v in self.frozen.values())
        self.frozen_allocated = sum(v[1] for v in self.frozen.values())
        self.logical, self.allocated = logical, allocated
        self.full_scans += 1
        self.full_scan_files += files
        self.full_scan_seconds += time.monotonic() - start
        self.last_reconcile = time.monotonic()
        return logical

    def check(self):
        if time.monotonic() - self.last_reconcile >= self.interval:
            return self.reconcile()
        return self.refresh()

    def summary(self):
        return dict(logical_bytes=self.logical, allocated_bytes=self.allocated,
                    full_scans=self.full_scans, full_scan_files=self.full_scan_files,
                    full_scan_seconds=round(self.full_scan_seconds, 6),
                    routine_scans=self.routine_scans, routine_scan_files=self.routine_scan_files,
                    routine_scan_seconds=round(self.routine_scan_seconds, 6),
                    reconciliation_interval_seconds=self.interval, corrections=list(self.corrections))
