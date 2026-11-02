#!/usr/bin/env python3
"""Run bounded ordinary-syscall batches with a continuous trace_pipe collector."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
import pathlib
import re
import resource
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import functools
import tempfile
import uuid
from quota_ledger import QuotaLedger
from campaign import KINDS, LOSS_KEYS, aggregate, atomic_json, compress, digest, raw_identity, read_json, stats
from trace_lifecycle import parse_trace
from syz_workload import generate_program, execution_issues
from kcov_feedback import FeedbackCorpus, parse_symbol_ranges
from fault_errno_catalog import (errno_choices, errno_codes, generated_header_matches,
                                 load_catalog, normalize_errno_choices,
                                 normalize_errno_selection, random_admission_hooks,
                                 render_errno_selection, validate_selection)

PROJECT = pathlib.Path(__file__).resolve().parents[1]
BASE = '/var/lib/ext4-counter-campaign'
DISPLAY_TZ = timezone(timedelta(hours=8))
MIN_FINALIZATION_RESERVE = 8 * 1024**2
RANDOM_FAULT_OPERATIONS = tuple(
    name for name, definition in load_catalog()['hooks'].items()
    if definition.get('runtime_random'))
RANDOM_FAULT_ADMITTED_OPERATIONS = tuple(random_admission_hooks())

# A mount-scoped random hook can fire while the launcher is preparing its
# private worker tree.  In that narrow case there is a real, timestamped hook
# event and a clean counter trace, but no generated syscall envelope exists to
# attribute it to.  Only this exact class of evidence loss may be quarantined
# and carried forward; trace/lifecycle loss remains fail-closed.
HOOK_ONLY_WORKLOAD_EXIT_CODES = frozenset((90, 91))


class CampaignError(RuntimeError):
    pass


def directory_bytes(path):
    total = 0
    for base, _, files in os.walk(path):
        for name in files:
            try:
                total += (pathlib.Path(base) / name).stat().st_size
            except FileNotFoundError:
                pass
    return total


def program_input_plan(object_sharing, ordinal, resource_every):
    if object_sharing == 'shared':
        return 'resource-valid', 'shared-contention'
    if resource_every and ordinal % resource_every == 0:
        template = 'namespace' if (ordinal // resource_every) % 2 == 0 else 'file-state'
        return 'resource-valid', template
    return 'ordinary', 'auto'


def validate_guest_trace_cpus(requested, cpu_stats):
    """Require every requested vCPU to be online and represented in tracefs."""
    actual = len(cpu_stats)
    if actual != requested:
        raise CampaignError(f'guest_cpu_count_mismatch:requested={requested}:online={actual}')
    return actual


def random_fault_policy(args, segment_seed, segment_id):
    operation_option = args.random_fault_operations.strip()
    if operation_option == 'all':
        operations = list(RANDOM_FAULT_OPERATIONS)
    else:
        operations = [name.strip() for name in operation_option.split(',') if name.strip()]
    if not operations or len(set(operations)) != len(operations):
        raise CampaignError('invalid_random_fault_operations')
    unknown = sorted(set(operations) - set(RANDOM_FAULT_OPERATIONS))
    if unknown:
        raise CampaignError('unsupported_random_fault_operations:' + ','.join(unknown))
    seed = (segment_seed * 0x9E3779B97F4A7C15 + segment_id + 1) & ((1 << 63) - 1)
    seed = seed or 1
    probability = args.random_fault_probability_ppm
    try:
        errno_option = getattr(args, 'random_fault_errnos', 'native')
        selected_errnos = normalize_errno_choices(errno_option) \
            if '=' not in errno_option else ['per_hook']
        errno_by_operation = normalize_errno_selection(errno_option, operations)
        if selected_errnos != ['per_hook']:
            validate_selection(operations, selected_errnos)
        else:
            for operation, choices in errno_by_operation.items():
                if choices == ['native']:
                    continue
                validate_selection([operation], choices)
    except ValueError as error:
        raise CampaignError(str(error)) from error
    errno_config = (render_errno_selection(errno_by_operation)
                    if selected_errnos == ['per_hook'] else
                    ('native' if selected_errnos == ['native'] else ','.join(selected_errnos)))
    admission = {name: ('reviewed' if name in RANDOM_FAULT_ADMITTED_OPERATIONS
                        else 'experimental') for name in operations}
    isolation = bool(getattr(args, 'fault_isolation', True))
    config = (f'mode=random seed={seed} probability_ppm={probability} max_hits=1 '
              f'operations={",".join(operations)} errnos={errno_config}')
    return dict(mode='seeded_random', seed=seed, probability_ppm=probability,
                max_hits=1, operations=operations, errnos=selected_errnos,
                errno_by_operation=errno_by_operation,
                isolate_after_hit=isolation,
                admission=admission,
                supported_errnos={name: errno_choices(name) for name in operations},
                supported_errno_codes={name: errno_codes(name) for name in operations},
                config=config)


def validate_random_fault_mode(args):
    """Validate campaign shapes supported by the mount-scoped injector."""
    if not getattr(args, 'random_fault_injection', False):
        return
    if args.mode not in ('single', 'concurrent'):
        raise CampaignError('--random-fault-injection requires --mode single or --mode concurrent')
    if args.mode == 'concurrent' and args.workers < 2:
        raise CampaignError('--random-fault-injection concurrent mode requires --workers >= 2')
    if args.max_batches != 1:
        raise CampaignError('--random-fault-injection requires --max-batches 1 so each segment has one reproducible workload window')


def stable_snapshot_health(path):
    path = pathlib.Path(path)
    if not path.is_file():
        return dict(status='missing', path=path.name)
    try:
        snapshot = read_json(path)
    except (ValueError, OSError, EOFError):
        return dict(status='invalid_evidence', path=path.name)
    counts = {key: int(snapshot.get(key, 0) or 0) for key in
              ('observed_mismatches', 'namespace_mismatched', 'relations_mismatches')}
    observation = dict(path=path.name, snapshot_id=snapshot.get('snapshot_id'),
                       reason=snapshot.get('reason'), valid=snapshot.get('valid'),
                       namespace_valid=snapshot.get('namespace_valid'),
                       relations_valid=snapshot.get('relations_valid'), **counts)
    checks = ('valid', 'namespace_valid', 'relations_valid')
    reason = str(snapshot.get('reason') or '')
    # A false validity bit can mean either a measured formula mismatch or an
    # interrupted/unsupported observation.  Only the former is a candidate.
    observation_failure = any(token in reason.lower()
                              for token in ('unverified', 'interrupted', 'timeout',
                                            'missing', 'read_failed', 'unsupported',
                                            'busy', 'unstable',
                                            'concurrent_or_unhealthy'))
    measured_mismatch = (any(counts.values()) or
                         (any(snapshot.get(key) is False for key in checks) and
                          not observation_failure) or
                         any(token in reason.lower()
                             for token in ('mismatch', 'disagree', 'violation')))
    if measured_mismatch:
        observation['status'] = 'candidate'
    elif not all(snapshot.get(key) is True for key in checks):
        observation['status'] = 'unverified'
    else:
        observation['status'] = 'clear'
    return observation


def fault_status_matches_arm(status, arm, policy):
    """Accept an injection only when its status belongs to this segment arm."""
    if not isinstance(status, dict) or not isinstance(arm, dict):
        return False
    arm_target_pid = int(arm.get('target_pid', 0) or 0)
    arm_target_pgid = int(arm.get('target_pgid', 0) or 0)
    if int(arm.get('generation', 0) or 0) <= 0 or (arm_target_pid <= 0 and arm_target_pgid <= 0):
        return False
    if status.get('enabled') is not True or status.get('mode') != 'random':
        return False
    if status.get('operation') != 'random' or int(status.get('injected', 0) or 0) <= 0:
        return False
    if int(status.get('random_seed', 0) or 0) != int(policy.get('seed', 0) or 0):
        return False
    if int(status.get('probability_ppm', 0) or 0) != int(policy.get('probability_ppm', 0) or 0):
        return False
    if int(status.get('max_hits', 0) or 0) != int(policy.get('max_hits', 0) or 0):
        return False
    for key in ('enabled', 'mode', 'operation', 'generation', 'target_pid', 'target_pgid',
                'random_seed', 'probability_ppm', 'max_hits'):
        if status.get(key) != arm.get(key):
            return False
    if (int(status.get('target_pid', 0) or 0) <= 0 and
            int(status.get('target_pgid', 0) or 0) <= 0):
        return False
    return status.get('errno_operation') in policy.get('operations', [])


def random_fault_trace_event(path, target_pid, target_pgid=0):
    path = pathlib.Path(path)
    if not path.is_file():
        return None
    pattern = re.compile(r'ext4_fault_inject:.*?operation=(\S+).*?injection=1\b')
    with path.open(errors='replace') as stream:
        for number, line in enumerate(stream, 1):
            match = pattern.search(line)
            if not match:
                continue
            fields = dict(re.findall(r'\b([a-z_]+)=([^\s]+)', line))
            event_pid = int(fields.get('pid', 0) or 0)
            event_target_pid = int(fields.get('target_pid', 0) or 0)
            event_target_pgid = int(fields.get('target_pgid', 0) or 0)
            if target_pid and (event_pid != target_pid or event_target_pid != target_pid):
                continue
            if target_pgid and event_target_pgid != target_pgid:
                continue
            if not target_pid and not target_pgid:
                continue
            stamp = re.search(r'\s(\d+)\.(\d+):\s+ext4_fault_inject:', line)
            timestamp_ns = (int(stamp.group(1)) * 10**9 +
                            int(stamp.group(2).ljust(9, '0'))) if stamp else None
            return dict(trace_line=number, operation=fields.get('operation'), pid=event_pid,
                        target_pid=event_target_pid, target_pgid=event_target_pgid,
                        timestamp_ns=timestamp_ns,
                        errno=int(fields['errno']),
                        errno_choice_index=int(fields.get('errno_choice_index', -1)),
                        errno_choice_draw=int(fields.get('errno_choice_draw', -1)),
                        random_draw=int(fields.get('random_draw', -1)), fields=fields)
    return None


def verify_random_fault_errno_choice(event, status, policy):
    if not event or event.get('operation') not in policy.get('operations', []):
        return False, []
    operation = event['operation']
    operation_policy = policy.get('errno_by_operation', {}).get(operation)
    if operation_policy == ['native'] or (operation_policy is None and
                                          policy.get('errnos') == ['native']):
        choices = errno_codes(operation)
        expected_status_choices = 'native'
    else:
        code_by_name = dict(zip(policy['supported_errnos'][operation],
                                policy['supported_errno_codes'][operation]))
        names = operation_policy or policy['errnos']
        choices = [code_by_name[name] for name in names]
        expected_status_choices = ','.join(str(-code) for code in choices)
    index = event.get('errno_choice_index', -1)
    draw = event.get('errno_choice_draw', -1)
    verified = (
        0 <= index < len(choices) and
        event.get('errno') == -choices[index] and
        isinstance(draw, int) and draw >= 0 and draw % len(choices) == index and
        event.get('fields', {}).get('randomized') == '1' and
        int(status.get('errno', 0)) == event.get('errno') and
        int(status.get('errno_choice_index', -1)) == index and
        status.get('errno_operation', operation) == operation and
        status.get('errno_choices') == expected_status_choices)
    return verified, [-code for code in choices]


def carry_start_line(trace_path, starts):
    """Include a stable-snapshot phase BEGIN with the bounded event carry.

    CT_PHASE markers are emitted before the first counter event in a snapshot.
    A carry that starts at that event otherwise keeps the later END marker and
    makes a valid cross-segment phase look unmatched.
    """
    if not starts:
        return None
    first = min(line for line, _ in starts)
    pids = {pid for line, pid in starts if line == first}
    latest = {}
    phase = re.compile(rb'tracing_mark_write: CT_PHASE (?P<body>.*)')
    target = re.compile(rb'\btarget_pid=(\d+)\b')
    with pathlib.Path(trace_path).open('rb') as stream:
        for number, raw in enumerate(stream, 1):
            if number > first:
                break
            match = phase.search(raw)
            if not match or b'phase=BEGIN' not in match['body'] or \
                    b'source=stable_snapshot' not in match['body']:
                continue
            pid = target.search(match['body'])
            if pid and int(pid[1]) in pids:
                latest[int(pid[1])] = number
    return min([first] + [line for line in latest.values() if line < first])


def snapshot_event_identity(event):
    """Keep the counter identity needed to disambiguate reset epochs."""
    return {key: event.get(key) for key in
            ('epoch', 'seq', 'op', 'dev', 'pid', 'line')}


def select_snapshot_event(events, snapshot, dev, identity=None):
    """Select one snapshot BEGIN from a possibly carried trace.

    Snapshot operation ids are local to a counter epoch and are reused after
    a filesystem reset.  A carried trace can therefore contain several
    BEGIN events with the same operation id.  Prefer the persisted epoch/seq
    identity; for legacy manifests, the latest matching event is the current
    segment boundary and is the least surprising fallback.
    """
    try:
        snapshot_id = int(snapshot.get('snapshot_id'))
    except (AttributeError, TypeError, ValueError):
        return None
    candidates = [event for event in events
                  if event.get('formula') == 'BEGIN'
                  and event.get('dev') == dev
                  and event.get('op') == snapshot_id]
    if not candidates:
        return None
    if isinstance(identity, dict):
        required = [(key, identity.get(key)) for key in ('epoch', 'seq')
                    if identity.get(key) is not None]
        if required:
            candidates = [event for event in candidates
                          if all(event.get(key) == value for key, value in required)]
            if not candidates:
                return None
    return max(candidates, key=lambda event: event.get('line', -1))


def timed_stage(name):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            start = time.monotonic()
            try:
                return fn(self, *args, **kwargs)
            finally:
                self.record_time(name, time.monotonic()-start)
        return wrapper
    return decorate


class Collector:
    def __init__(self, owner):
        self.owner = owner
        self.proc = subprocess.Popen(owner.ssh_args + ['cat /sys/kernel/tracing/trace_pipe'],
                                     stdout=subprocess.PIPE, stderr=(owner.run / 'reader-stderr.txt').open('wb'),
                                     start_new_session=True)
        self.sealed = set()
        self.current = None
        self.stream = None
        self.bytes = 0
        self.preamble = bytearray()
        self.terminal = False
        self.error = None
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def close_stream(self):
        if self.stream:
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()
            self.sealed.add(self.current)
            self.stream = None

    def read(self):
        try:
            while True:
                line = self.proc.stdout.readline(32768)
                if not line:
                    if not self.terminal:
                        raise CampaignError('trace_reader_eof')
                    return
                if len(line) == 32768 and not line.endswith(b'\n'):
                    raise CampaignError('trace_line_too_long')
                marker = re.search(rb'tracing_mark_write: CT_CAMPAIGN ROTATE id=(segment-\d+)\b', line)
                if marker:
                    with self.lock:
                        ident = marker[1].decode()
                        # SSH trace_pipe can replay a marker when a buffered
                        # read crosses a transport boundary.  The first
                        # marker already owns this segment; reopening its
                        # trace with ``xb`` would terminate the collector and
                        # make a long campaign fail at the next rotation.
                        if self.current != ident:
                            self.close_stream()
                            self.current = ident
                            folder = self.owner.run / 'segments' / self.current
                            folder.mkdir(parents=True, exist_ok=True)
                            self.stream = (folder / 'trace.txt').open('xb')
                            self.bytes = len(self.preamble)
                            if self.bytes:
                                self.stream.write(self.preamble)
                                self.preamble.clear()
                if self.stream:
                    if self.bytes + len(line) > self.owner.args.max_trace_mb * 1024**2:
                        raise CampaignError('max_trace_bytes')
                    self.stream.write(line)
                    self.bytes += len(line)
                elif self.current is None:
                    if len(self.preamble) + len(line) > self.owner.args.max_trace_mb * 1024**2:
                        raise CampaignError('max_trace_bytes')
                    self.preamble.extend(line)
                if b'tracing_mark_write: CT_CAMPAIGN TERMINAL' in line:
                    with self.lock:
                        self.terminal = True
                        self.close_stream()
                    return
        except Exception as error:
            self.error = str(error)
        finally:
            with self.lock:
                self.close_stream()

    def wait(self, ident, sealed=False):
        limit = time.monotonic() + 20
        while time.monotonic() < limit:
            self.owner.check()
            with self.lock:
                ready = ident in self.sealed if sealed else self.current == ident
                if ready:
                    return
            time.sleep(0.1)
        detail = self.error or ('collector_current=' + str(self.current))
        raise CampaignError('trace_rotation_timeout:' + detail)

    def stop(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
        self.thread.join(timeout=5)


class Runner:
    def __init__(self, args):
        self.args = args
        self.run = pathlib.Path(__import__('tempfile').mkdtemp(prefix='campaign-', dir=PROJECT / 'results'))
        self.root = PROJECT.parents[1]
        self.started = time.monotonic()
        self.deadline = self.started + args.duration_seconds + args.boot_timeout + args.analysis_timeout + args.max_batches*(args.batch_timeout+2) + 900
        self.qemu = None
        self.collector = None
        self.current = None
        self.cancelled = None
        # The first host signal is a bounded stop request.  A second signal
        # escalates to cancellation so the matrix can still force a stuck VM.
        self.signal_stop_requested = False
        self.last_quota_check = 0
        # A quota hit is a normal bounded-stop request.  The active segment
        # must finish collecting before checkpoint/FS shutdown starts.
        self.quota_graceful = True
        self.stop_requested = None
        self.generated_syscalls = 0
        self.verified_syscalls = 0
        self.pending_segment = None
        self.discard_segment = None
        # A fault may leave the mounted filesystem in a state that is useful
        # for diagnosis but unsafe for another independent workload.  Keep
        # the evidence and quarantine that filesystem for the rest of the VM.
        self.tainted_kinds = set()
        # Once collection has stopped, checkpointing and offline evidence
        # retrieval may legitimately take longer than the collection wall
        # budget.  Keep the watchdog active during workload collection, but
        # let the bounded finalization path complete its evidence transaction.
        self.finalizing = False
        self.previous_loss = {}
        self.carry = None
        self.feedback = FeedbackCorpus(self.run, scheduler_policy=args.scheduler_policy,
                                       feedback_mode=args.feedback_mode, seed=args.seed,
                                       target_scope=args.target_scope)
        self.ledger = QuotaLedger(self.run)
        self.timings = {}
        self.segment_timings = {}
        self._progress_phase_starts = {}
        self.meta = dict(schema=1, run_id=self.run.name, status='preparing', boot_id=None,
                         segments_started=0, devices={}, initial_states={}, final_states={},
                         limits=vars(args).copy(), clean_shutdown=False, disk={},
                         fault_tainted_kinds=[],
                         started_wall_ns=time.time_ns(), predecessor=args.predecessor)
        self.save()
        print(f'run_dir={self.run}', flush=True)
        self.progress(f'CAMPAIGN START mode={args.mode} duration_limit={args.duration_seconds}s')

    def record_time(self, stage, seconds):
        for attr in ('timings', 'segment_timings'):
            if not hasattr(self, attr):
                setattr(self, attr, {})
            row = getattr(self, attr).setdefault(stage, dict(seconds=0.0, calls=0))
            row['seconds'] = round(row['seconds'] + seconds, 6)
            row['calls'] += 1

    def progress(self, message, wall_ns=None):
        stamp = datetime.fromtimestamp((wall_ns or time.time_ns()) / 1e9, DISPLAY_TZ)
        line = f'[{stamp.isoformat(sep=" ", timespec="milliseconds")}] {message}'
        with (self.run / 'progress.log').open('a') as stream:
            stream.write(line + '\n')
        print(line, flush=True)

    def request_strict_kcov_stop(self):
        if getattr(self.args, 'strict_kcov_feedback', False) and not self.stop_requested:
            self.stop_requested = 'kcov_feedback_gap'
            self.progress('GRACEFUL STOP REQUEST reason=kcov_feedback_gap')

    def live_line(self, line):
        message = line.decode('utf-8', errors='replace').rstrip('\r\n')
        wall = re.search(r'\bwall_ns=(\d+)\b', message) if message.startswith('CT_PROGRESS ') else None
        if message.startswith('CT_PROGRESS '):
            message = message.removeprefix('CT_PROGRESS ')
            message = re.sub(r' wall_ns=\d+\b', '', message)
            match = re.search(r'\bphase=([^ ]+)', message)
            if match:
                if not hasattr(self, '_progress_phase_starts'):
                    self._progress_phase_starts = {}
                name = match[1]
                if name.endswith('_START'):
                    self._progress_phase_starts[name[:-6].lower()] = time.monotonic()
                elif name.endswith('_END'):
                    key = name[:-4].lower()
                    start = self._progress_phase_starts.pop(key, None)
                    if start is not None:
                        self.record_time(key, time.monotonic() - start)
        self.progress(message, int(wall[1]) if wall else None)

    def save(self):
        self.meta['elapsed_seconds'] = round(time.monotonic()-self.started, 3)
        self.meta['stage_timings'] = getattr(self, 'timings', {})
        if hasattr(self, 'ledger'):
            self.meta['quota_ledger'] = self.ledger.summary()
        atomic_json(self.run / 'campaign.json', self.meta)

    def persist_feedback_summary(self):
        """Keep campaign.json aligned with the post-augmentation corpus state."""
        self.meta['kcov_feedback'] = self.feedback.summary()

    def kernel_diagnostics(self):
        """Return a bounded diagnosis when the guest emitted a kernel fault.

        A kernel BUG/Oops can terminate the trace_pipe SSH reader before the
        guest has a chance to write its normal segment/checkpoint markers.  In
        that case reporting only ``trace_reader_eof`` loses the primary
        failure cause.  The serial log is the authoritative boot console for
        this campaign, so retain the matching kind and a short evidence tail.
        """
        serial = self.run / 'serial.log'
        if not serial.is_file():
            return None
        try:
            text = serial.read_text(errors='replace')
        except OSError:
            return None
        text = self._filter_expected_fault_warnings(text)
        patterns = (
            ('kernel_bug', r'kernel BUG at |BUG: unable to handle|BUG: KASAN'),
            ('kernel_oops', r'\bOops:|general protection fault|invalid opcode'),
            ('kernel_panic', r'Kernel panic - not syncing|\bpanic:'),
            ('kernel_warning', r'WARNING:|possible circular locking dependency'),
        )
        matches = []
        for kind, pattern in patterns:
            if re.search(pattern, text):
                matches.append(kind)
        if not matches:
            return None
        lines = text.splitlines()
        evidence = [line[-400:] for line in lines if any(
            re.search(pattern, line) for _, pattern in patterns)]
        return {'kind': matches[0], 'kinds': matches, 'evidence': evidence[-20:]}

    def _filter_expected_fault_warnings(self, text):
        """Remove only the ext4 warning emitted by an observed injected return.

        ``ext4_handle_dirty_metadata_error()`` deliberately emits a warning
        when the injected helper error aborts a journal handle.  That warning
        is evidence of the selected error path, not a kernel BUG/Oops.  Keep
        every other warning and all BUG/Oops/panic text fail-closed.
        """
        if not getattr(getattr(self, 'args', None), 'random_fault_injection', False):
            return text
        hit = False
        for manifest_path in sorted((self.run / 'segments').glob('segment-*/manifest.json')):
            try:
                manifest = read_json(manifest_path)
            except (OSError, ValueError, EOFError):
                continue
            if (manifest.get('fault_injection') or {}).get('hit'):
                hit = True
                break
        if not hit:
            return text
        kept = []
        expected = []
        for line in text.splitlines(keepends=True):
            if re.search(r'WARNING:.*ext4_handle_dirty_metadata_error', line):
                expected.append(line.rstrip('\n'))
            else:
                kept.append(line)
        if expected:
            self.meta.setdefault('expected_fault_warnings', []).extend(expected[-20:])
        return ''.join(kept)

    def check(self, ignore_cancel=False):
        if not ignore_cancel and self.cancelled:
            raise CampaignError(self.cancelled)
        if (not ignore_cancel and not getattr(self, 'finalizing', False)
                and time.monotonic() > getattr(self, 'deadline', float('inf'))):
            raise CampaignError('campaign_wall_watchdog')
        if not ignore_cancel and self.collector and self.collector.error:
            raise CampaignError(self.collector.error)
        if self.qemu and self.qemu.poll() is not None and not self.meta.get('shutdown_requested'):
            raise CampaignError('qemu_crash')
        now = time.monotonic()
        if not ignore_cancel and now - self.last_quota_check > 1:
            self.last_quota_check = now
            limit = self.args.max_run_mb * 1024**2
            reserve = max(MIN_FINALIZATION_RESERVE,
                          (getattr(self.args, 'max_trace_mb', 64) + 64) * 1024**2,
                          limit // 20)
            if not hasattr(self, 'ledger'):
                self.ledger = QuotaLedger(self.run)
            start = time.monotonic()
            full_scans = self.ledger.full_scans
            used = self.ledger.check()
            if self.ledger.full_scans != full_scans:
                self.record_time('quota_reconciliation', time.monotonic()-start)
            if used >= max(0, limit - reserve):
                if not getattr(self, 'stop_requested', None):
                    self.stop_requested = 'max_run_bytes'
                    self.progress(f'GRACEFUL STOP REQUEST reason=max_run_bytes used={used} '
                                  f'limit={limit} reserve={reserve}')
                if not getattr(self, 'quota_graceful', False):
                    raise CampaignError('max_run_bytes')
            if shutil.disk_usage(self.run).free < self.args.min_free_mb*1024**2:
                raise CampaignError('host_free_disk_limit')

    def command(self, argv, timeout=60, log=None, allowed=(0,), salvage=False, memory_limit=False, live=False, stage=None):
        start = time.monotonic()
        try:
            return self._command(argv, timeout, log, allowed, salvage, memory_limit, live)
        finally:
            if stage:
                self.record_time(stage, time.monotonic()-start)

    def _command(self, argv, timeout=60, log=None, allowed=(0,), salvage=False, memory_limit=False, live=False):
        output = self.run / (log or 'command.txt')
        def child_limits():
            if memory_limit:
                resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
                resource.setrlimit(resource.RLIMIT_CPU, (self.args.analysis_timeout, self.args.analysis_timeout+2))
            resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        with output.open('ab' if log else 'wb') as stream:
            proc = subprocess.Popen([str(a) for a in argv], stdout=subprocess.PIPE if live else stream,
                                    stderr=subprocess.STDOUT,
                                    start_new_session=True, preexec_fn=child_limits)
            selector = selectors.DefaultSelector() if live else None
            pending = b''
            if live:
                os.set_blocking(proc.stdout.fileno(), False)
                selector.register(proc.stdout, selectors.EVENT_READ)
            limit = time.monotonic()+timeout
            try:
                while True:
                    if live:
                        for key, _ in selector.select(0.15 if proc.poll() is None else 0):
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            stream.write(chunk)
                            stream.flush()
                            pending += chunk
                            while b'\n' in pending:
                                line, pending = pending.split(b'\n', 1)
                                self.live_line(line)
                            # Bound terminal buffering even for a line without a newline.
                            if len(pending) >= 32768:
                                self.live_line(pending)
                                pending = b''
                    if proc.poll() is not None:
                        if live and selector.get_map() and selector.select(0):
                            continue
                        break
                    self.check(ignore_cancel=salvage)
                    if time.monotonic() > limit:
                        raise CampaignError('command_timeout:' + str(argv[0]))
                    if not live or not selector.get_map():
                        time.sleep(0.15)
            except BaseException:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise
            finally:
                if live:
                    if pending:
                        self.live_line(pending)
                    selector.close()
                    proc.stdout.close()
        if proc.returncode not in allowed:
            raise CampaignError(f'command_exit_{proc.returncode}:' + str(argv[0]))
        return proc.returncode

    def guest(self, *args, timeout=60, retry_transport=False, **kwargs):
        cmd = shlex.join(['sh', '/tmp/guest-campaign.sh', *map(str, args)])
        attempts = 3 if retry_transport else 1
        last = None
        for attempt in range(attempts):
            try:
                return self.command(self.ssh_args + [cmd], timeout=timeout, **kwargs)
            except CampaignError as error:
                last = error
                if not self._retryable_transport(error) or attempt + 1 == attempts:
                    raise
                self.progress(f'TRANSPORT RETRY tool=ssh attempt={attempt + 2}/{attempts} command={args[0]}')
                time.sleep(1 << attempt)
        raise last

    @staticmethod
    def _retryable_transport(error):
        """Only retry a failed SSH transport, never a remote exit status."""
        return bool(re.fullmatch(r'command_exit_255:(?:ssh|scp)', str(error)))

    @timed_stage('fetch')
    def fetch(self, remote, target, salvage=False):
        argv = self.scp_args + ['-r', 'root@127.0.0.1:' + remote, str(target)]
        last = None
        for attempt in range(4):
            try:
                return self.command(argv, timeout=300, salvage=salvage)
            except CampaignError as error:
                last = error
                retryable = self._retryable_transport(error) or str(error) == 'command_timeout:scp'
                if not retryable or attempt == 3:
                    raise
                self.progress(f'TRANSPORT RETRY tool=scp attempt={attempt + 2}/4 remote={remote} '
                              f'reason={error}')
                time.sleep(1 << attempt)
        raise last

    @timed_stage('push')
    def push(self, sources, remote, timeout=60, stage=None):
        """Upload guest inputs with bounded retries for transient SSH setup."""
        argv = self.scp_args + [str(source) for source in sources] + [remote]
        last = None
        for attempt in range(4):
            try:
                return self.command(argv, timeout=timeout, stage=stage)
            except CampaignError as error:
                last = error
                retryable = self._retryable_transport(error) or str(error) == 'command_timeout:scp'
                if not retryable or attempt == 3:
                    raise
                self.progress(f'TRANSPORT RETRY tool=scp direction=push attempt={attempt + 2}/4 '
                              f'remote={remote} reason={error}')
                time.sleep(1 << attempt)
        raise last

    @timed_stage('prepare')
    def prepare(self):
        self.progress('PREFLIGHT START verifying reviewed probes and kernel binding')
        from source_audit import verify
        problems = verify(PROJECT, require_build=True)
        if not generated_header_matches():
            problems.append('fault_errno_catalog_header_out_of_date')
        if problems:
            raise CampaignError('probe_source_preflight_failed:' + ','.join(problems))
        self.progress('PREFLIGHT END reviewed source and kernel binding verified')
        self.progress('PREPARE START building launcher/generator and preparing VM images')
        minimum = 3*132*1024**2 + (PROJECT / 'build/linux-counter-trace/arch/x86/boot/bzImage').stat().st_size + 16*1024**2
        if minimum > self.args.max_run_mb*1024**2:
            raise CampaignError('insufficient_budget_for_images')
        evidence = self.run / 'evidence'
        for sub in ('scripts', 'tests'):
            (evidence / sub).mkdir(parents=True)
        shutil.copy2(PROJECT / 'fault-errno-catalog.json',
                     evidence / 'fault-errno-catalog.json')
        for file in ('campaign.py', 'campaign_runner.py', 'analyze-campaign.py', 'run-campaign.sh',
                     'trace_lifecycle.py', 'formula_relations.py', 'point_report.py', 'analyze-lifecycle.py', 'check-disk.py',
                     'syz_workload.py', 'kcov_feedback.py', 'quota_ledger.py', 'source_audit.py',
                     'performance_report.py', 'run-feedback-study.py'):
            shutil.copy2(PROJECT / 'scripts' / file, evidence / 'scripts' / file)
        for file in ('campaign-workload.c', 'chain-workload.c', 'guest-campaign.sh', 'test-campaign.py', 'test-disk-layouts.py',
                     'syz-runtime.h', 'syz-launcher.c', 'test-syzkaller.py', 'test-kcov-feedback.py'):
            shutil.copy2(PROJECT / 'tests' / file, evidence / 'tests' / file)
        shutil.copytree(PROJECT / 'audit', evidence / 'audit')
        shutil.copytree(PROJECT / 'syzgen', evidence / 'syzgen')
        # The clean checkout uses a relative replacement so the generated API
        # files come from the pinned source checkout. Relocate that replacement
        # for the archived evidence tree without changing the tracked module.
        module_text = (evidence / 'syzgen/go.mod').read_text()
        old_reference = '../../../tools/src/syzkaller-master'
        if old_reference in module_text:
            module_text = module_text.replace(old_reference, str(self.root / 'tools/src/syzkaller-master'))
        (self.run / 'syzgen-build.mod').write_text(module_text)
        shutil.copy2(evidence / 'syzgen/go.sum', self.run / 'syzgen-build.sum')
        shutil.copy2(PROJECT / 'build/linux-counter-trace/arch/x86/boot/bzImage', self.run / 'kernel.bzImage')
        shutil.copy2(PROJECT / 'build/linux-counter-trace/.config', evidence / 'kernel.config')
        for file in ('0001-counter-trace-core-and-probes.patch', 'manifest.json'):
            shutil.copy2(PROJECT / 'patches' / file, evidence / file)
        for name, argv in [('gcc', ['gcc', '--version']), ('qemu', ['qemu-system-x86_64', '--version']),
                           ('e2fsck', ['e2fsck', '-V']), ('python', [sys.executable, '--version'])]:
            self.command(argv, log='evidence/' + name + '-version.txt')
        rootfs_backing = getattr(self.args, 'rootfs_backing', None)
        backing = pathlib.Path(rootfs_backing).resolve() if rootfs_backing else \
            next((path for path in (self.root / 'work/debian-clean-smalljournal.img',
                                    self.root / 'work/debian.img') if path.is_file()),
                 self.root / 'work/debian.img')
        if not backing.is_file():
            raise CampaignError('rootfs_backing_missing:' + str(backing))
        self.meta['dependencies'] = {'rootfs_backing': str(backing), 'rootfs_sha256': digest(backing),
                                     'ssh_key_path': str(self.root / 'work/debian.id_rsa')}
        self.meta['build_sha256'] = {str(p.relative_to(self.run)): digest(p)
                                    for p in evidence.rglob('*') if p.is_file()}
        self.meta['build_sha256']['kernel.bzImage'] = digest(self.run / 'kernel.bzImage')
        self.command(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static', '-o',
                      self.run / 'ct-syz-launcher', evidence / 'tests/syz-launcher.c'])
        go = os.environ.get('FUZZING_ORCLE_GO') or shutil.which('go') or \
            str(self.root / 'tools/go126/bin/go1.26.0')
        # The experiment tree is sometimes exported without .git metadata. Go's
        # automatic VCS stamping must not turn that reproducible local build into
        # a campaign preflight failure.
        self.command(['env', 'GOFLAGS=-buildvcs=false', go,
                      '-C', evidence / 'syzgen',
                      'build', '-modfile', self.run / 'syzgen-build.mod', '-mod=readonly',
                      '-o', self.run / 'ct-syzgen', '.'], timeout=120)
        self.command([go, 'version', '-m', self.run / 'ct-syzgen'],
                     log='evidence/syzkaller-build-info.txt')
        self.meta['dependencies']['syzkaller_source'] = str(self.root / 'tools/src/syzkaller-master')
        self.meta['build_sha256']['ct-syzgen'] = digest(self.run / 'ct-syzgen')
        self.meta['build_sha256']['ct-syz-launcher'] = digest(self.run / 'ct-syz-launcher')
        self.command(['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'raw', '-b', backing,
                      self.run / 'root.qcow2'])
        template_dir = self.root / 'work/ext4-counter-start-images' / f'seed-{self.args.seed}'
        template_dir.mkdir(parents=True, exist_ok=True)
        with (template_dir / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for kind in KINDS:
                template = template_dir / (kind + '.raw')
                if not template.is_file():
                    with tempfile.TemporaryDirectory(prefix='.mkfs-', dir=template_dir) as staging:
                        staged_image = pathlib.Path(staging) / (kind + '.raw')
                        self.command(['qemu-img', 'create', '-q', '-f', 'raw', staged_image, '132M'])
                        image_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                    f'ext4-counter-campaign:{self.args.seed}:{kind}'))
                        options = ['-O', 'orphan_file', '-b', '4096', '-N', '4096',
                                   '-E', 'lazy_itable_init=0,hash_seed=' + image_uuid,
                                   '-U', image_uuid]
                        if kind == 'bigalloc':
                            options += ['-O', 'bigalloc,orphan_file', '-C', '65536']
                        if kind == 'inline':
                            options += ['-O', 'inline_data,orphan_file']
                        self.command(['mke2fs', '-q', '-F', '-t', 'ext4', *options, staged_image])
                        os.replace(staged_image, template)
                image = self.run / (kind + '.raw')
                self.command(['cp', '--sparse=always', template, image])
                self.meta.setdefault('start_images', {})[kind] = dict(
                    path=str(template), sha256=digest(image))
            fcntl.flock(lock, fcntl.LOCK_UN)
        common = ['-i', str(self.root / 'work/debian.id_rsa'), '-o', 'BatchMode=yes',
                  '-o', 'ConnectTimeout=3', '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
                  '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'LogLevel=ERROR']
        self.ssh_args = ['ssh', *common, '-p', str(self.args.port), 'root@127.0.0.1']
        self.scp_args = ['scp', '-q', *common, '-P', str(self.args.port)]
        # Multi-threaded TCG is required for the guest vCPUs to overlap.  A
        # single TCG thread makes the shared-worker barrier deterministic but
        # serializes every ext4 scope, producing no real interleaving evidence.
        # ``-cpu max`` exposes host-dependent feature combinations to TCG.
        # With this KASAN build that can trigger a deterministic wild-memory
        # report in identify_cpu during boot, before ext4 is mounted.  Keep a
        # portable qemu64 default while allowing an explicit max override for
        # CPU-feature experiments.
        qemu_cpu = getattr(self.args, 'qemu_cpu', 'qemu64')
        self.meta['qemu_cpu'] = qemu_cpu
        self.progress(f'VM CONFIG cpu={qemu_cpu} accelerator=tcg,thread=multi')
        qemu = ['qemu-system-x86_64', '-machine', 'q35', '-accel', 'tcg,thread=multi', '-cpu', qemu_cpu,
                '-m', str(self.args.guest_memory_mb), '-smp', str(self.args.cpus),
                '-kernel', str(self.run / 'kernel.bzImage')]
        for filename, format_ in [('root.qcow2', 'qcow2')] + [(k+'.raw', 'raw') for k in KINDS]:
            qemu += ['-drive', f'file={self.run / filename},format={format_},if=virtio']
        qemu += ['-netdev', f'user,id=n0,hostfwd=tcp:127.0.0.1:{self.args.port}-:22',
                 '-device', 'virtio-net-pci,netdev=n0', '-append',
                 'root=/dev/vda rw console=ttyS0 net.ifnames=0', '-display', 'none',
                 '-monitor', 'none', '-serial', 'file:' + str(self.run / 'serial.log')]
        boot_start = time.monotonic()
        self.qemu = subprocess.Popen(qemu, stdout=(self.run / 'qemu.log').open('wb'),
                                     stderr=subprocess.STDOUT, start_new_session=True)
        self.progress(f'VM BOOT START pid={self.qemu.pid} waiting_for_ssh')
        (self.run / 'qemu.pid').write_text(str(self.qemu.pid)+'\n')
        ready = False
        boot_deadline = time.monotonic() + self.args.boot_timeout
        next_boot_progress = time.monotonic() + 15
        for _ in range(180):
            if time.monotonic() >= boot_deadline:
                break
            try:
                rc = self.command(self.ssh_args + ['test -e /sys/fs/ext4/vda/counter_snapshot'],
                                  timeout=15, allowed=(0, 1, 255), log='boot-ssh.txt')
            except CampaignError as error:
                # TCG guests can accept TCP before sshd can finish its banner
                # exchange. Treat that bounded probe timeout like a not-ready
                # response and keep the overall boot deadline authoritative.
                if str(error) != 'command_timeout:ssh':
                    raise
                rc = 255
            if rc == 0:
                ready = True; break
            if time.monotonic() >= next_boot_progress:
                self.progress('VM BOOT WAIT waiting_for_ssh_and_counter_snapshot')
                next_boot_progress = time.monotonic() + 15
            time.sleep(0.5)
        if not ready:
            raise CampaignError('guest_boot_timeout')
        self.record_time('vm_boot', time.monotonic() - boot_start)
        self.push([self.run / 'ct-syz-launcher', evidence / 'tests/guest-campaign.sh'],
                  'root@127.0.0.1:/tmp/', timeout=120)
        self.guest('init', self.args.buffer_kb, getattr(self.args, 'scenario', 'baseline'))
        self.fetch(BASE, self.run / 'initial')
        kallsyms = self.run / 'initial' / 'kallsyms.txt'
        if kallsyms.is_file():
            self.feedback.ext4_ranges = parse_symbol_ranges(kallsyms)
        self.meta['boot_id'] = (self.run / 'initial/boot-id.txt').read_text().strip()
        self.meta['initial_states'] = {k: read_json(self.run / 'initial' / (k+'-initial-state.json')) for k in KINDS}
        cpu_stats = list((self.run / 'initial').glob('cpu*-stats.txt'))
        self.meta['trace_cpus'] = validate_guest_trace_cpus(self.args.cpus, cpu_stats)
        self.previous_loss = {p.name: stats(p) for p in cpu_stats}
        self.meta.update(status='running', ready_wall_ns=time.time_ns())
        self.save()
        self.collector = Collector(self)
        self.progress(f'VM READY boot_id={self.meta["boot_id"]}')

    def new_segment(self, index):
        ident = f'segment-{index:06d}'
        folder = self.run / 'segments' / ident
        folder.mkdir(parents=True, exist_ok=True)
        selection = self.args.mode
        kind_index = (index//2 if selection == 'matrix' else index) % len(KINDS)
        kind = KINDS[kind_index]
        tainted = set(getattr(self, 'tainted_kinds', set()))
        mode = ('single' if index % 2 == 0 else 'concurrent') if selection == 'matrix' else selection
        workers = 1 if mode == 'single' else self.args.workers
        object_sharing = 'shared' if mode == 'concurrent' and workers > 1 else 'private'
        m = dict(schema=1, run_id=self.run.name, boot_id=self.meta['boot_id'],
                 segment_id=index, kind=kind, mode=mode, status='running', issues=[],
                 seed=self.args.seed + index*100000, workers=workers,
                 object_sharing=object_sharing,
                 synchronized_worker_start=object_sharing == 'shared',
                 filesystem_reset_before=kind in tainted,
                 guest_started=False,
                 memory_mb=self.args.memory_mb, scenario=getattr(self.args, 'scenario', 'baseline'),
                 source='syzkaller', start_wall_ns=time.time_ns(), trace_file='trace.txt')
        if getattr(self.args, 'random_fault_injection', False):
            m['fault_injection'] = random_fault_policy(self.args, m['seed'], index)
        atomic_json(folder / 'manifest.json', m)
        self.meta['segments_started'] = index+1
        self.save()
        self.progress(f'SEGMENT START segment={ident} kind={kind} mode={mode} workers={m["workers"]}')
        self.pending_segment = folder
        if hasattr(self, 'ledger'):
            self.ledger.activate(folder)
        return folder

    def collect_segment(self, folder):
        self.current = folder
        segment_start = time.monotonic()
        m = read_json(folder / 'manifest.json')
        programs = folder / 'syz-programs'
        programs.mkdir(exist_ok=True)
        for batch in range(1, self.args.max_batches + 1):
            for worker in range(m['workers']):
                target = programs / f'b{batch}-w{worker}'
                ordinal = m['segment_id'] * self.args.max_batches * m['workers'] + \
                    (batch-1)*m['workers'] + worker
                resource_every = getattr(self.args, 'resource_valid_every', 2)
                fresh_every = getattr(self.args, 'fresh_generate_every', 5)
                input_mode, resource_template = program_input_plan(
                    m.get('object_sharing', 'private'), ordinal, resource_every)
                force_fresh = bool(fresh_every and ordinal % fresh_every == 0)
                candidate = None
                if input_mode == 'resource-valid':
                    task = 'resource_valid'
                elif force_fresh:
                    task = 'fresh_generate'
                else:
                    candidate = self.feedback.parent_candidate()
                    task = 'mutate' if candidate else 'generate_no_corpus'
                previous = candidate['path'] if candidate else None
                budget_candidate = (candidate if self.args.mutation_budget_policy == 'dynamic' else None)
                call_budget = self.feedback.mutation_budget(
                    budget_candidate, self.args.syz_calls, self.args.max_mutation_calls)
                decision = dict(ordinal=ordinal, resource_valid_every=resource_every,
                                fresh_generate_every=fresh_every, input_mode=input_mode,
                                force_fresh=force_fresh, task=task,
                                resource_template=resource_template,
                                scheduler_policy=self.args.scheduler_policy,
                                feedback_mode=self.args.feedback_mode,
                                mutation_budget_policy=self.args.mutation_budget_policy,
                                requested_calls=call_budget,
                                rarity_score=candidate.get('rarity_score') if candidate else None,
                                rarity_components=candidate.get('rarity_components') if candidate else None,
                                rarity_pc=candidate.get('rarity_pc') if candidate else None,
                                rarity_ext4=candidate.get('rarity_ext4') if candidate else None,
                                rarity_edge=candidate.get('rarity_edge') if candidate else None,
                                rarity_semantic=candidate.get('rarity_semantic') if candidate else None,
                                yield_score=candidate.get('yield_score') if candidate else None,
                                yield_ema_pc=candidate.get('yield_ema_pc') if candidate else None,
                                yield_ema_ext4=candidate.get('yield_ema_ext4') if candidate else None,
                                zero_target_yield_streak=candidate.get('zero_target_yield_streak') if candidate else None,
                                zero_any_signal_streak=candidate.get('zero_any_signal_streak') if candidate else None,
                                zero_yield_streak=candidate.get('zero_yield_streak') if candidate else None,
                                parent_candidate_sha256=candidate['sha256'] if candidate else None,
                                selection_index=candidate['selection_index'] if candidate else None)
                seed = m['seed'] + (batch-1) * 1000 + worker
                self.progress(f'GENERATE START segment={folder.name} program={target.name} seed={seed} '
                              f'method={"Mutate" if previous else "Generate"} task={task}')
                program = generate_program(self.command, self.run / 'ct-syzgen', target,
                                           seed, call_budget, mutate=previous,
                                           runtime_path=self.run / 'evidence/tests/syz-runtime.h',
                                           input_mode=input_mode, decision=decision,
                                           resource_template=resource_template)
                actual_mutation = bool(candidate and program.get('method') == 'Mutate')
                if actual_mutation:
                    self.feedback.commit_parent(candidate)
                program['task'] = 'mutate' if actual_mutation else (
                    'generate_fallback' if candidate else task)
                program['scheduler_policy'] = self.args.scheduler_policy
                program['parent_sha256'] = candidate['sha256'] if actual_mutation else None
                program['selection_index'] = candidate['selection_index'] if actual_mutation else None
                atomic_json(target / 'program.json', program)
                self.progress(f'GENERATE END segment={folder.name} program={target.name} '
                              f'calls={len(program["calls"])} task={program["task"]} '
                              f'parent={program["parent_sha256"] or "none"}')
                self.generated_syscalls += len(program['calls'])
                m['generated_syscalls'] = m.get('generated_syscalls', 0) + len(program['calls'])
                m.setdefault('scheduler_decisions', []).append(dict(
                    program=target.name, task=program['task'],
                    parent_sha256=program['parent_sha256'],
                    selection_index=program['selection_index'], seed=seed,
                    requested_calls=call_budget, actual_calls=len(program['calls']),
                    rarity_score=candidate.get('rarity_score') if actual_mutation else None,
                    rarity_components=candidate.get('rarity_components') if actual_mutation else None,
                    rarity_pc=candidate.get('rarity_pc') if actual_mutation else None,
                    rarity_ext4=candidate.get('rarity_ext4') if actual_mutation else None,
                    rarity_edge=candidate.get('rarity_edge') if actual_mutation else None,
                    rarity_semantic=candidate.get('rarity_semantic') if actual_mutation else None,
                    yield_score=candidate.get('yield_score') if actual_mutation else None,
                    yield_ema_pc=candidate.get('yield_ema_pc') if actual_mutation else None,
                    yield_ema_ext4=candidate.get('yield_ema_ext4') if actual_mutation else None,
                    zero_target_yield_streak=candidate.get('zero_target_yield_streak') if actual_mutation else None,
                    zero_any_signal_streak=candidate.get('zero_any_signal_streak') if actual_mutation else None,
                    zero_yield_streak=candidate.get('zero_yield_streak') if actual_mutation else None))
        self.progress(f'UPLOAD START segment={folder.name}')
        self.push(['-r', programs], 'root@127.0.0.1:/tmp/ct-syz-' + folder.name,
                  timeout=60, stage='upload')
        # Persist this marker before entering the remote segment command.  It
        # lets salvage distinguish a segment that only rotated the host trace
        # (and therefore has no remote directory to fetch) from one whose
        # guest workload actually started.
        m['guest_started'] = True
        m['guest_started_wall_ns'] = time.time_ns()
        atomic_json(folder / 'manifest.json', m)
        timeout = self.args.segment_seconds + self.args.max_batches * (self.args.batch_timeout+2) + 60
        rc = self.guest('segment', folder.name, m['kind'], m['mode'], self.args.segment_seconds,
                        m['seed'], m['workers'], m['memory_mb'], self.args.batch_timeout,
                        self.args.max_batches, 'syzkaller', getattr(self.args, 'scenario', 'baseline'),
                        m.get('fault_injection', {}).get('config', 'off'),
                        int(bool(m.get('fault_injection', {}).get('isolate_after_hit', True))),
                        timeout=timeout,
                        # 90 is the launcher's aggregate worker-failure status.
                        # It is a valid remote result that still has snapshots,
                        # trace and workload evidence to fetch and audit.
                        allowed=(0, 1, 2, 70, 86, 90, 91, 124, 137, 152),
                        log=folder.name+'-guest.txt', live=True, stage='guest_execution')
        self.progress(f'FETCH START segment={folder.name} guest_exit={rc}')
        self.fetch(BASE + '/' + folder.name + '/.', folder)
        fault_policy = m.get('fault_injection')
        if fault_policy:
            arm_path = folder / 'fault-status-arm.json'
            arm = read_json(arm_path) if arm_path.exists() else {}
            status_candidates = []
            for status_path in (folder / 'fault-status-hit.json',
                                folder / 'fault-status-after.json'):
                if not status_path.exists():
                    continue
                candidate = read_json(status_path)
                if fault_status_matches_arm(candidate, arm, fault_policy):
                    status_candidates.append(candidate)
            status = status_candidates[0] if status_candidates else {}
            fault_policy['status'] = status
            fault_policy['hit'] = bool(status_candidates)
            kind = m['kind']
            fault_policy['stable_observation'] = dict(
                before=stable_snapshot_health(folder / f'{kind}-before.json'),
                after=stable_snapshot_health(folder / f'{kind}-after.json'))
            if fault_policy['hit']:
                fault_policy['event'] = random_fault_trace_event(
                    folder / 'trace.txt', int(status.get('target_pid', 0)),
                    int(status.get('target_pgid', 0)))
                event = fault_policy['event']
                choice_verified, allowed_errnos = verify_random_fault_errno_choice(
                    event, status, fault_policy)
                fault_policy['errno_choice_verified'] = choice_verified
                fault_policy['errno_choice_allowed'] = allowed_errnos
                if not choice_verified:
                    m['issues'].append('fault_injection_errno_choice_unverified')
                stop_marker = folder / 'fault-stop-after-call'
                isolated = stop_marker.is_file() and m.get('mode') == 'single'
                fault_policy['isolated_after_call'] = isolated
                m['fault_stop_after_call'] = isolated
                offline_status = folder / 'fault-offline-status.txt'
                if offline_status.is_file():
                    values = {}
                    for line in offline_status.read_text(errors='replace').splitlines():
                        key, separator, value = line.partition('=')
                        if separator:
                            values[key] = value
                    fault_policy['offline_check'] = values
                    try:
                        offline_unmount = int(values.get('unmount_exit', '255'))
                        offline_rc = int(values.get('fsck_exit', '0'))
                    except ValueError:
                        offline_unmount = -1
                        offline_rc = -1
                    if offline_unmount != 0:
                        fault_policy['offline_check_unverified'] = True
                    elif offline_rc != 0:
                        fault_policy['offline_fsck_candidate'] = dict(
                            source='fault_offline_fsck', status='candidate',
                            fsck_exit=offline_rc,
                            evidence=f'segments/{folder.name}/fault-fsck.txt')
                fault_policy['filesystem_tainted'] = True
                m['fault_tainted'] = True
                self.tainted_kinds.add(kind)
                self.meta['fault_tainted_kinds'] = sorted(self.tainted_kinds)
                self.progress(f'FAULT FILESYSTEM TAINTED segment={folder.name} kind={kind} '
                              'subsequent_workloads_quarantined=1')
            m['fault_injection'] = fault_policy
        if m['segment_id'] == 0:
            self.establish_measurement_start(folder)
        scenario_evidence, scenario_issues = self.validate_scenario_evidence(
            folder, getattr(self.args, 'scenario', 'baseline'))
        workload_text = (folder / 'workload.txt').read_text(errors='replace') if (folder / 'workload.txt').exists() else ''
        summary_pattern = (r'CT_WORKLOAD_SUMMARY data_calls=(\d+) fd_repairs=(\d+)'
                           r'(?: fallocate_adaptations=(\d+))?'
                           r'(?: kcov_count=(\d+)(?: kcov_raw_count=(\d+))? kcov_max_call_index=(\d+)'
                           r' kcov_max_call_pcs=(\d+) saturation_flags=(\d+))?')
        summaries = []
        for data, repairs, adaptations, total, raw_total, max_index, max_pcs, flags in re.findall(
                summary_pattern, workload_text):
            summaries.append(dict(data_calls=int(data), fd_repairs=int(repairs),
                fallocate_adaptations=int(adaptations) if adaptations else 0,
                kcov_count=int(total) if total else None,
                kcov_raw_count=int(raw_total) if raw_total else None,
                kcov_max_call_index=int(max_index) if max_index else None,
                kcov_max_call_pcs=int(max_pcs) if max_pcs else None,
                kcov_saturation_flags=int(flags) if flags else None))
        m['workload_summaries'] = len(summaries)
        m['data_calls'] = sum(row['data_calls'] for row in summaries)
        m['fd_repairs'] = sum(row['fd_repairs'] for row in summaries)
        m['fallocate_adaptations'] = sum(row['fallocate_adaptations'] for row in summaries)
        m['kcov_workload_summaries'] = [row for row in summaries
                                        if row['kcov_count'] is not None]
        progress = dict(re.findall(r'^(\w+)=(\d+)$', (folder / 'progress.txt').read_text(), re.M)) if (folder / 'progress.txt').exists() else {}
        m['batches'] = int(progress.get('batches', 0))
        m['expected_programs'] = [f'b{b}-w{w}' for b in range(1, m['batches']+1)
                                  for w in range(m['workers'])]
        feedback_start = time.monotonic()
        feedback = self.feedback.collect(folder, m)
        self.record_time('feedback', time.monotonic()-feedback_start)
        self.persist_feedback_summary()
        self.save()
        self.progress(f'KCOV SAMPLE segment={folder.name} pending_new_pcs={feedback["new_coverage"]} '
                      f'complete={feedback["passed"]}')
        if not feedback['passed']:
            # Counter-trace evidence remains independently useful, but strict
            # long matrices stop at this segment boundary instead of hiding
            # incomplete coverage feedback behind a successful campaign.
            self.progress(f'KCOV GAP segment={folder.name} reason={",".join(feedback["errors"])}')
            self.request_strict_kcov_stop()
        if rc == 0:
            segment_status = 'complete'
        elif rc in (124, 137, 152):
            segment_status = 'timeout'
        elif rc == 70:
            segment_status = 'snapshot_invalid'
        else:
            segment_status = 'guest_error'
        m.update(status=segment_status, end_wall_ns=time.time_ns(), guest_exit=rc)
        progress = dict(re.findall(r'^(\w+)=(\d+)$', (folder / 'progress.txt').read_text(), re.M)) if (folder / 'progress.txt').exists() else {}
        m['batches'] = int(progress.get('batches', 0))
        m['segment_elapsed_seconds'] = round(time.monotonic()-segment_start, 6)
        m['stage_timings'] = dict(self.segment_timings)
        m['feedback'] = feedback
        m['kcov_feedback_errors'] = feedback['errors']
        m['scenario_evidence'] = scenario_evidence
        m['issues'].extend(scenario_issues)
        fault_policy = m.get('fault_injection') or {}
        if fault_policy.get('hit'):
            if rc:
                # A fault can make the launcher cleanup return the injected
                # errno even though every generated call and its raw trace
                # completed.  Preserve that exit as a candidate and let the
                # next segment continue; genuine execution/trace issues are
                # still rejected during lifecycle aggregation.
                m['fault_expected_workload_exit'] = True
            hit = dict(segment_id=folder.name, kind=m['kind'], seed=fault_policy['seed'],
                       probability_ppm=fault_policy['probability_ppm'],
                       event=fault_policy.get('event'),
                       stable_observation=fault_policy.get('stable_observation'),
                       workload_exit=rc)
            self.meta.setdefault('fault_injection_hit', hit)
            observations = fault_policy.get('stable_observation') or {}
            stable_candidates = {phase: observation for phase, observation in observations.items()
                                 if observation.get('status') == 'candidate'}
            stable_unverified = {phase: observation for phase, observation in observations.items()
                                 if observation.get('status') not in ('clear', 'candidate')}
            if stable_candidates:
                fault_policy['stable_candidate'] = dict(
                    source='stable_snapshot', observations=stable_candidates)
            elif stable_unverified:
                fault_policy['stable_unverified'] = stable_unverified
            m['fault_injection'] = fault_policy
        atomic_json(folder / 'manifest.json', m)
        if rc and not (fault_policy.get('hit') and m.get('fault_expected_workload_exit')):
            raise CampaignError(m['status'])
        return m

    def establish_measurement_start(self, folder):
        self.meta['prepared_states'] = self.meta['initial_states']
        self.meta['initial_states'] = {
            kind: read_json(folder / f'{kind}-start.json') for kind in KINDS}
        self.meta['measurement_start_wall_ns'] = time.time_ns()
        self.save()

    @staticmethod
    def _read_scenario_values(path):
        values = {}
        for line in path.read_text(errors='replace').splitlines():
            key, separator, value = line.partition('=')
            if separator:
                values[key] = value
        return values

    def validate_scenario_evidence(self, folder, scenario):
        evidence = {}
        issues = []
        initial_dir = self.run / 'initial'
        expected = {'stateful': scenario in ('stateful', 'all'),
                    'large_dir': scenario in ('large-dir', 'all'),
                    'pressure': scenario in ('pressure', 'all')}
        for kind in KINDS:
            current_path = folder / f'{kind}-scenario.txt'
            initial_path = initial_dir / f'{kind}-scenario.txt'
            if not current_path.is_file() or not initial_path.is_file():
                issues.append(f'scenario_missing_evidence:{kind}')
                continue
            current = self._read_scenario_values(current_path)
            initial = self._read_scenario_values(initial_path)
            evidence[kind] = {'initial': initial, 'current': current}
            if current.get('scenario') != scenario or initial.get('scenario') != scenario:
                issues.append(f'scenario_configuration_mismatch:{kind}')
            if expected['stateful'] and (int(initial.get('stateful_files', '0')) < 32 or
                                         int(current.get('stateful_files', '0')) < 16):
                issues.append(f'scenario_stateful_resources_missing:{kind}')
            if expected['large_dir'] and (int(initial.get('large_dir_files', '0')) < 256 or
                                          int(current.get('large_dir_files', '0')) < 128):
                issues.append(f'scenario_large_directory_missing:{kind}')
            if expected['pressure']:
                free_kb = int(current.get('available_kb', '-1'))
                target_kb = int(current.get('pressure_target_free_kb', '-1'))
                if int(current.get('pressure_bytes', '0')) <= 0 or target_kb <= 0 or free_kb < 0 or free_kb > target_kb:
                    issues.append(f'scenario_pressure_target_not_reached:{kind}')
        return evidence, issues

    def merge_pending_trace(self, target):
        """Attach a quota-only rotation tail to the last real segment."""
        if not self.pending_segment:
            return
        tail = self.pending_segment / 'trace.txt'
        raw = target / 'trace.txt'
        if not tail.is_file() or not raw.is_file():
            raise CampaignError('missing_quota_rotation_trace')
        with raw.open('ab') as dest, tail.open('rb') as src:
            shutil.copyfileobj(src, dest, 1024 * 1024)
            dest.flush()
            os.fsync(dest.fileno())

    def count_verified_syscalls(self, folder):
        report = read_json(pathlib.Path(folder) / 'analysis/lifecycle/report.json.gz')
        return sum(bool(call.get('verified')) for call in report.get('native_syscalls', []))

    def count_verified_syscalls_total(self, last_folder):
        """Count verified native calls across all finalized segments.

        ``count_verified_syscalls`` intentionally remains a per-segment
        primitive because it is useful for segment-level diagnostics.  The
        campaign metadata, however, represents the whole run and must not
        silently regress to the count from only the final segment.  Bound the
        walk by the segment being finalized so a stray directory created
        after shutdown cannot inflate the reported total.
        """
        last_folder = pathlib.Path(last_folder)
        segment_root = last_folder.parent
        total = 0
        for folder in sorted(segment_root.glob('segment-*')):
            if not folder.is_dir() or folder.name > last_folder.name:
                continue
            try:
                total += self.count_verified_syscalls(folder)
            except (FileNotFoundError, OSError, EOFError, json.JSONDecodeError):
                # A segment without sealed lifecycle evidence is not a
                # verified syscall.  The campaign report will retain the
                # segment's own completeness issue.
                continue
        return total

    def record_finalized_syscalls(self, folder):
        verified = self.count_verified_syscalls(folder)
        self.verified_syscalls = getattr(self, 'verified_syscalls', 0) + verified
        self.meta['verified_syscalls'] = self.verified_syscalls
        self.save()
        return verified

    def syscall_budget_reached(self):
        target = getattr(self.args, 'target_syscalls', 0)
        return bool(target and self.generated_syscalls >= target and
                    self.verified_syscalls >= target)

    @staticmethod
    def compact_segment_artifacts(folder):
        """Compact artifacts after KCOV and native-call evidence are sealed."""
        folder = pathlib.Path(folder)
        for sample in (folder / 'coverage').glob('*.bin'):
            # coverage.json already records the uncompressed sample hash.
            compress(sample, level=6)
        # The executable is reproducible from the retained generated source;
        # program.json and program.syz remain available for audit and replay.
        for executable in (folder / 'syz-programs').glob('*/workload'):
            executable.unlink(missing_ok=True)

    @timed_stage('analysis')
    def finalize(self, folder, checkpoint=False, salvage=False):
        self.progress(f'ANALYSIS START segment={folder.name} salvage={salvage}')
        m = read_json(folder / 'manifest.json')
        raw = folder / 'trace.txt'
        if not raw.exists():
            # A completed segment is finalized once after rotation and may be
            # finalized again during checkpoint.  The first finalization
            # compresses the immutable raw trace, so the second pass must use
            # that compressed evidence instead of misclassifying it as an
            # empty segment.  Checkpoint also adds the final CPU-loss files;
            # refresh the manifest hashes before returning.
            compressed = folder / 'trace.txt.gz'
            lifecycle = folder / 'analysis/lifecycle/report.json.gz'
            if compressed.exists() and lifecycle.exists():
                m['trace_file'] = 'trace.txt.gz'
                m['evidence_sha256'] = {
                    str(p.relative_to(folder)): digest(p)
                    for p in folder.rglob('*')
                    if p.is_file() and p.name != 'manifest.json'
                }
                atomic_json(folder / 'manifest.json', m)
                self.save()
                return m
            m.setdefault('issues', []).append('no_trace_collected')
            atomic_json(folder / 'manifest.json', m)
            return
        m['trace_bytes'] = raw.stat().st_size
        if (folder / 'boot-id.txt').exists():
            m['boot_id'] = (folder / 'boot-id.txt').read_text().strip()
        m['raw_domains'] = raw_identity(raw)
        for kind, initial in self.meta['initial_states'].items():
            matches = [d['dev'] for d in m['raw_domains'] if d['epoch'] == initial['epoch']]
            if len(matches) == 1:
                self.meta['devices'][kind] = matches[0]
        analysis = folder / 'analysis'
        analysis.mkdir(exist_ok=True)
        # A segment with an invalid/incomplete trace may not produce a bounded
        # carry artifact.  Keep that evidence gap local to the segment: a
        # later segment must not turn it into a host-side ENOENT and abort the
        # whole campaign.  Without a carry trace we also cannot reuse any
        # previous stable anchor safely.
        carry_folder = self.carry
        carry_trace = (carry_folder / 'analysis/carry-out.txt.gz'
                       if carry_folder else None)
        carry_available = bool(carry_trace and carry_trace.is_file())
        if carry_folder and not carry_available:
            m['issues'].append('missing_carry_trace')
        with (analysis / 'trace.txt').open('wb') as out:
            if carry_available:
                import gzip
                with gzip.open(carry_trace, 'rb') as src:
                    shutil.copyfileobj(src, out)
            with raw.open('rb') as src:
                shutil.copyfileobj(src, out)
        # Loss counters are cumulative because collection never stops or clears.
        m['loss_delta'] = {}
        for p in folder.glob('cpu*-stats.txt'):
            current = stats(p)
            before = self.previous_loss.get(p.name, {})
            delta = {key: current.get(key, -1)-before.get(key, 0) for key in LOSS_KEYS}
            m['loss_delta'][p.name] = delta
            (analysis / p.name).write_text(''.join(f'{key}: {value}\n' for key, value in delta.items()))
            self.previous_loss[p.name] = current
        events, _ = parse_trace(analysis / 'trace.txt')
        # Record the current segment's stable snapshot identities before
        # adding it to the carry chain.  The operation id alone is not
        # sufficient because mkfs/reset starts a new counter epoch and can
        # reuse the same id.
        snapshot_events = {}
        for kind in KINDS:
            for suffix in ('before', 'after', 'post-cleanup'):
                sample_path = folder / f'{kind}-{suffix}.json'
                if not sample_path.exists():
                    continue
                sample_event = select_snapshot_event(
                    events, read_json(sample_path), self.meta['devices'].get(kind))
                if sample_event is not None:
                    snapshot_events[f'{kind}-{suffix}'] = snapshot_event_identity(sample_event)
        intervals = []
        for kind in KINDS:
            required = [folder / (kind+'-'+s+'.json') for s in ('start', 'end', 'before', 'after')]
            if not all(p.exists() for p in required):
                m['issues'].append(kind+':missing_interval_files'); continue
            for path in required:
                shutil.copy2(path, analysis / path.name)
            snapshots = [kind+'-before.json', kind+'-after.json']
            post_cleanup = folder / (kind+'-post-cleanup.json')
            if post_cleanup.exists():
                shutil.copy2(post_cleanup, analysis / post_cleanup.name)
                snapshots.append(post_cleanup.name)
            if carry_available:
                previous_anchor = (kind+'-post-cleanup.json'
                                   if (carry_folder / (kind+'-post-cleanup.json')).exists()
                                   else kind+'-after.json')
                anchor_name = kind+'-carry.json'
                previous_anchor_path = carry_folder / previous_anchor
                if not previous_anchor_path.exists():
                    m['issues'].append(kind+':missing_carry_anchor_file')
                    continue
                shutil.copy2(previous_anchor_path, analysis / anchor_name)
                anchor = read_json(analysis / anchor_name)
                carry_manifest = read_json(carry_folder / 'manifest.json')
                anchor_identity = (carry_manifest.get('snapshot_events') or {}).get(
                    pathlib.Path(previous_anchor).stem)
                begin = select_snapshot_event(
                    events, anchor, self.meta['devices'][kind], anchor_identity)
                if begin is None:
                    m['issues'].append(kind+':missing_carry_anchor')
                else:
                    atomic_json(analysis / (kind+'-start.json'),
                                dict(epoch=begin['epoch'], seq=begin['seq']-1, active=0))
                snapshots.insert(0, anchor_name)
            if checkpoint:
                shutil.copy2(self.run / 'checkpoint' / (kind+'-final.json'), analysis / (kind+'-final.json'))
                shutil.copy2(self.run / 'checkpoint' / (kind+'-final-state.json'), analysis / (kind+'-end.json'))
                snapshots.append(kind+'-final.json')
            intervals.append(dict(name=kind+'-'+m['mode'], mode=m['mode'], dev=self.meta['devices'].get(kind),
                                  start=kind+'-start.json', end=kind+'-end.json', snapshots=snapshots))
        atomic_json(analysis / 'chain-manifest.json',
                    dict(schema=2, run_id=self.run.name, boot_id=self.meta['boot_id'],
                         require_native_syscalls=m['status']=='complete',
                         carry_available=carry_available,
                         carry_source=(str(carry_folder.name) if carry_available else None),
                         intervals=intervals))
        # Last complete stable snapshot scopes become the next bounded carry anchor.
        starts = []
        for kind in KINDS:
            path = folder / (kind+'-post-cleanup.json')
            if not path.exists():
                path = folder / (kind+'-after.json')
            if path.exists():
                label = path.stem
                sample_event = snapshot_events.get(label)
                if sample_event:
                    starts.append((sample_event['line'], sample_event['pid']))
        if starts:
            carry_start = carry_start_line(analysis / 'trace.txt', starts)
            with (analysis / 'trace.txt').open('rb') as src, (analysis / 'carry-out.txt').open('wb') as dest:
                for number, line in enumerate(src, 1):
                    if number >= carry_start:
                        dest.write(line)
            compress(analysis / 'carry-out.txt', level=6)
        m['snapshot_events'] = snapshot_events
        del events
        self.command([sys.executable, self.run / 'evidence/scripts/analyze-lifecycle.py', analysis],
                     timeout=self.args.analysis_timeout, log=folder.name+'-analysis.txt',
                     allowed=(0, 1), salvage=salvage, memory_limit=True)
        report = read_json(analysis / 'lifecycle/report.json')
        if m.get('source') == 'syzkaller':
            fault_stop = None
            stop_path = folder / 'fault-stop-after-call'
            if stop_path.is_file():
                fault_stop = {}
                for line in stop_path.read_text(errors='replace').splitlines():
                    key, separator, value = line.partition('=')
                    if separator:
                        fault_stop[key] = value
            m['syzkaller_execution_issues'] = execution_issues(folder / 'syz-programs',
                    report['native_syscalls'], folder.name, m.get('batches', 0), m['workers'],
                    fault_stop=fault_stop)
            m['issues'].extend(m['syzkaller_execution_issues'])
        # Persist fields created before lifecycle analysis before the
        # attribution helper reads and rewrites the manifest.  Otherwise its
        # stale on-disk copy can discard trace_bytes and other sealed fields.
        atomic_json(folder / 'manifest.json', m)
        self.record_fault_attribution(folder, report)
        m = read_json(folder / 'manifest.json')
        native_execution_verified = not m.get('syzkaller_execution_issues')
        m['joint_feedback'] = self.feedback.augment(
            folder, report, native_execution_verified=native_execution_verified,
            timeline_path=analysis / 'lifecycle/timeline.jsonl', mode=m.get('mode', 'single'))
        self.persist_feedback_summary()
        self.compact_segment_artifacts(folder)
        self.progress(f'KCOV FEEDBACK segment={folder.name} native_verified={native_execution_verified} '
                      f'total_pcs={m["joint_feedback"]["campaign_summary"]["total_coverage"]} '
                      f'corpus={m["joint_feedback"]["campaign_summary"]["corpus_size"]} '
                      f'new_semantic={m["joint_feedback"]["new_semantic_features"]} '
                      f'parent_selections={m["joint_feedback"]["campaign_summary"]["parent_selections"]}')
        m['coverage'] = read_json(folder / 'coverage.json')
        m['feedback'].update(m['coverage'])
        m['feedback']['segment_summary'] = m['coverage']
        m['feedback']['campaign_summary'] = m['joint_feedback']['campaign_summary']
        if not native_execution_verified:
            errors = ['native_execution_unverified']
            m['feedback']['native_execution_verified'] = False
            m['feedback']['errors'] = sorted(set(m['feedback'].get('errors', []) + errors))
            m['feedback']['passed'] = False
            m['kcov_feedback_errors'] = m['feedback']['errors']
            self.request_strict_kcov_stop()
        passed = report['passed'] and not m.get('syzkaller_execution_issues')
        m['analysis_passed'] = passed
        if not passed:
            m['issues'].append('analysis_failed')
        for path in [raw, analysis / 'trace.txt',
                     *list((analysis / 'lifecycle').glob('*.json')),
                     analysis / 'lifecycle/timeline.jsonl']:
            if path.exists():
                compress(path, level=6)
        m['trace_file'] = 'trace.txt.gz'
        m['lifecycle_report'] = 'analysis/lifecycle/report.json.gz'
        m['evidence_sha256'] = {str(p.relative_to(folder)): digest(p) for p in folder.rglob('*')
                                if p.is_file() and p.name != 'manifest.json'}
        atomic_json(folder / 'manifest.json', m)
        if hasattr(self, 'ledger'):
            self.ledger.seal(folder)
        self.carry = folder
        self.save()
        self.progress(f'ANALYSIS END segment={folder.name} kind={m["kind"]} mode={m["mode"]} '
                      f'batches={m.get("batches")} bytes={m["trace_bytes"]} '
                      f'analysis={"passed" if passed else "failed"}')
        if not salvage:
            self.guest('discard', folder.name)
        return passed

    def record_lifecycle_fault_candidate(self, folder):
        manifest = read_json(folder / 'manifest.json')
        fault = manifest.get('fault_injection') or {}
        if not fault.get('hit'):
            return
        report_path = folder / manifest.get('lifecycle_report',
                                             'analysis/lifecycle/report.json.gz')
        if not report_path.is_file():
            return
        report = read_json(report_path)
        counts = report.get('classification_counts', {})
        findings = {key: counts.get(key, 0)
                    for key in ('operation_exit_mismatch', 'persistent_mismatch')
                    if counts.get(key, 0)}
        if findings:
            fault['lifecycle_candidate'] = dict(
                classification_counts=findings, issues=report.get('issues', []))
            manifest['fault_injection'] = fault
            atomic_json(folder / 'manifest.json', manifest)

    def record_fault_attribution(self, folder, report):
        """Bind a hook event to at most one verified native syscall envelope."""
        manifest_path = folder / 'manifest.json'
        manifest = read_json(manifest_path)
        fault = manifest.get('fault_injection') or {}
        if not fault.get('hit'):
            return
        event = fault.get('event') or {}
        calls = report.get('native_syscalls', []) if isinstance(report, dict) else []
        line = event.get('trace_line')
        pid = int(event.get('pid', 0) or 0)
        event_ns = event.get('timestamp_ns')
        enclosing = [call for call in calls
                     if call.get('verified') and call.get('pid') == pid and
                     ((isinstance(event_ns, int) and
                       call.get('enter_ns', 0) < event_ns < call.get('exit_ns', 0)) or
                      (event_ns is None and isinstance(line, int) and
                       call.get('enter_line', 0) < line < call.get('exit_line', 0)))]
        attribution = {
            'schema': 1,
            'hook': event.get('operation'),
            'hook_errno': event.get('errno'),
            'hook_trace_line': line,
            'hook_timestamp_ns': event_ns,
            'hook_pid': pid,
            'target_pid': fault.get('status', {}).get('target_pid'),
            'native_envelope_count': len(enclosing),
            'native_envelope': (dict(id=enclosing[0].get('id'), name=enclosing[0].get('name'),
                                     program=enclosing[0].get('program'),
                                     call_index=enclosing[0].get('call_index'),
                                     enter_line=enclosing[0].get('enter_line'),
                                     exit_line=enclosing[0].get('exit_line'),
                                     raw_result=enclosing[0].get('raw_result'),
                                     errno=enclosing[0].get('errno'),
                                     user_result=enclosing[0].get('user_result'),
                                     user_errno=enclosing[0].get('user_errno'),
                                     verified=enclosing[0].get('verified'))
                               if len(enclosing) == 1 else None),
            'hook_trace_verified': bool(event.get('operation') and event.get('errno') is not None),
            'native_envelope_verified': len(enclosing) == 1,
            'userspace_return_verified': bool(len(enclosing) == 1 and enclosing[0].get('verified')),
            'status': ('verified' if len(enclosing) == 1 and enclosing[0].get('verified')
                       else 'hook_only' if event.get('operation') else 'unverified'),
        }
        fault['attribution'] = attribution
        manifest['fault_injection'] = fault
        atomic_json(folder / 'fault-attribution.json', attribution)
        atomic_json(manifest_path, manifest)

    @staticmethod
    def _is_syscall_evidence_gap(issue):
        """Return true for the bounded gaps caused by a pre-syscall exit."""
        if issue.endswith(':missing_or_extra_call_indices'):
            return bool(re.fullmatch(r'b\d+-w\d+', issue.split(':', 1)[0]))
        return bool(re.fullmatch(r'b\d+-w\d+:call_\d+_not_verified', issue))

    def hook_only_workload_exit_allowed(self, folder, integrity_issues=None):
        """Decide whether a hook-only startup failure may be quarantined.

        The random controller is armed before the launcher creates its worker
        root.  A hit during that setup can make the launcher exit before the
        first generated syscall.  We retain that segment as incomplete and
        continue only when the hook event, clean lifecycle report, and the
        userspace error text all agree on that very specific failure shape.
        Any raw trace/lifecycle/CPU-loss issue, verified native envelope, or
        unrelated launcher error remains a hard stop.
        """
        try:
            manifest = read_json(pathlib.Path(folder) / 'manifest.json')
            fault = manifest.get('fault_injection') or {}
            attribution = fault.get('attribution') or {}
            event = fault.get('event') or {}
            if not fault.get('hit') or attribution.get('status') != 'hook_only':
                return False
            if not attribution.get('hook_trace_verified') or attribution.get('native_envelope_count') != 0:
                return False
            if not manifest.get('fault_expected_workload_exit'):
                return False
            try:
                exit_code = int(manifest.get('guest_exit'))
            except (TypeError, ValueError):
                return False
            if exit_code not in HOOK_ONLY_WORKLOAD_EXIT_CODES:
                return False
            if int(manifest.get('batches', 0) or 0) < 1:
                return False

            report_path = pathlib.Path(folder) / manifest.get(
                'lifecycle_report', 'analysis/lifecycle/report.json.gz')
            report = read_json(report_path)
            # A clean lifecycle report with no native calls is what proves
            # this is a syscall attribution gap, rather than trace loss.
            if report.get('passed') is not True or report.get('native_syscalls'):
                return False

            gaps = (list(integrity_issues) if integrity_issues is not None else
                    self.segment_trace_integrity_issues(folder))
            if not gaps or any(not self._is_syscall_evidence_gap(issue) for issue in gaps):
                return False
            execution_gaps = manifest.get('syzkaller_execution_issues') or []
            if not execution_gaps or any(not self._is_syscall_evidence_gap(issue)
                                         for issue in execution_gaps):
                return False

            # The launcher reports the injected errno through its normal
            # strerror path (for example, ENOMEM -> "Cannot allocate memory").
            # Requiring that text prevents an unrelated exit(90/91) after a
            # coincidental hook hit from being treated as fault-caused.
            workload_path = pathlib.Path(folder) / 'workload.txt'
            if not workload_path.is_file():
                return False
            try:
                injected_errno = abs(int(event.get('errno')))
                error_text = os.strerror(injected_errno).casefold()
            except (TypeError, ValueError, OverflowError):
                return False
            workload_text = workload_path.read_text(errors='replace').casefold()
            if not error_text or error_text not in workload_text:
                return False
            return True
        except (OSError, ValueError, EOFError, KeyError, TypeError):
            return False

    def segment_trace_integrity_issues(self, folder):
        manifest = read_json(folder / 'manifest.json')
        prefixes = ('missing_or_empty_trace', 'invalid_raw_trace:',
                    'raw_identity_manifest_disagrees', 'raw_sequence_loss_or_duplicate:',
                    'cross_segment_epoch_or_sequence_discontinuity:',
                    'trace_loss:', 'missing_cpu_loss_evidence',
                    'missing_interval_files', 'missing_carry_anchor',
                    'missing_lifecycle_report', 'invalid_lifecycle_report:',
                    'unexpected_syzkaller_program_executed',
                    'no_syzkaller_batch_executed', 'missing_or_extra_call_indices')
        return [issue for issue in manifest.get('issues', [])
                if issue.startswith(prefixes) or ':call_' in issue]

    @timed_stage('finish_disk')
    def finish_disk(self):
        self.progress('SHUTDOWN START followed_by_fsck_and_disk_checks')
        self.meta['shutdown_requested'] = True
        self.save()
        self.command(self.ssh_args + ['systemctl poweroff'], timeout=15, allowed=(0, 255))
        try:
            self.qemu.wait(timeout=60)
        except subprocess.TimeoutExpired:
            raise CampaignError('shutdown_timeout_refusing_offline_fsck')
        if self.qemu.returncode != 0:
            raise CampaignError('abnormal_qemu_exit_refusing_offline_fsck')
        self.meta['clean_shutdown'] = True
        for kind in KINDS:
            rc = self.command(['e2fsck', '-fn', self.run / (kind+'.raw')], timeout=90,
                              stage='disk_fsck', log=kind+'-fsck.txt', allowed=(0, 1, 2, 4, 8, 16, 32, 128))
            self.command([sys.executable, self.run / 'evidence/scripts/check-disk.py',
                          self.run / (kind+'.raw'), '--output', self.run / (kind+'-disk.json')],
                         timeout=120, stage='disk_checker', log=kind+'-disk.txt', allowed=(0, 1))
            disk = read_json(self.run / (kind+'-disk.json'))
            snap = read_json(self.run / 'checkpoint' / (kind+'-final.json'))
            observed = dict(free_clusters=disk['superblock']['free_clusters'],
                            free_inodes=disk['superblock']['free_inodes'], dirs=disk['descriptor_totals']['dirs'])
            self.meta['disk'][kind] = dict(valid=disk['valid'], fsck_exit=rc,
                matches_final_stable_snapshot=snap['valid'] and snap['namespace_valid'] and
                all(snap[key] == value for key, value in observed.items()),
                sha256=digest(self.run / (kind+'-disk.json')),
                snapshot_sha256=digest(self.run / 'checkpoint' / (kind+'-final.json')),
                fsck_sha256=digest(self.run / (kind+'-fsck.txt')))
            if kind in getattr(self, 'tainted_kinds', set()):
                self.meta['disk'][kind]['fault_tainted'] = True
            disk_candidate_reasons = []
            if not disk.get('valid'):
                disk_candidate_reasons.append('disk_checker_invalid')
            if rc != 0:
                disk_candidate_reasons.append('fsck_not_clean')
            if not self.meta['disk'][kind]['matches_final_stable_snapshot']:
                disk_candidate_reasons.append('final_snapshot_disagrees')
            if disk_candidate_reasons and kind not in getattr(self, 'tainted_kinds', set()):
                hits = []
                for manifest_path in sorted((self.run / 'segments').glob('segment-*/manifest.json')):
                    segment = read_json(manifest_path)
                    if segment.get('kind') == kind and (segment.get('fault_injection') or {}).get('hit'):
                        hits.append(segment['segment_id'])
                if hits:
                    self.meta.setdefault('fault_injection_candidates', []).append(dict(
                        source='offline_disk', kind=kind,
                        segment_ids=hits,
                        reasons=disk_candidate_reasons,
                        disk_status=disk.get('status'), fsck_exit=rc,
                        r_link_mismatches=disk.get('r_link', {}).get('mismatches', []),
                        disk_evidence=kind+'-disk.json', fsck_evidence=kind+'-fsck.txt'))
        self.save()

    def execute(self):
        try:
            self.prepare()
            active_start = time.monotonic()
            previous = None
            for index in range(self.args.segments):
                # Do not create a new manifest after a bounded resource stop.
                # The last collected segment remains the one that is
                # checkpointed and audited below.
                self.check()
                if self.stop_requested:
                    if previous is None:
                        raise CampaignError(self.stop_requested)
                    self.meta['stop_reason'] = self.stop_requested
                    break
                folder = self.new_segment(index)
                self.segment_timings = {}
                self.current = folder
                wait_start = time.monotonic()
                self.guest('rotate', folder.name)
                self.collector.wait(folder.name)
                self.record_time('trace_wait', time.monotonic() - wait_start)
                segment_manifest = read_json(folder / 'manifest.json')
                if segment_manifest.get('filesystem_reset_before'):
                    reset_start = time.monotonic()
                    self.progress(f'FAULT RESET START segment={folder.name} kind={segment_manifest["kind"]}')
                    self.guest('reset', segment_manifest['kind'], timeout=180,
                               log=folder.name+'-fault-reset.txt', stage='fault_reset')
                    self.fetch(BASE + '/fault-reset-' + segment_manifest['kind'] + '.json',
                               folder / 'fault-reset.json')
                    segment_manifest['filesystem_reset_completed'] = True
                    segment_manifest['filesystem_reset_evidence'] = 'fault-reset.json'
                    atomic_json(folder / 'manifest.json', segment_manifest)
                    self.tainted_kinds.discard(segment_manifest['kind'])
                    self.meta['fault_tainted_kinds'] = sorted(self.tainted_kinds)
                    self.record_time('fault_reset', time.monotonic() - reset_start)
                    self.progress(f'FAULT RESET END segment={folder.name} kind={segment_manifest["kind"]}')
                if previous:
                    wait_start = time.monotonic()
                    self.collector.wait(previous.name, sealed=True)
                    self.record_time('trace_wait', time.monotonic() - wait_start)
                    passed = self.finalize(previous)
                    self.record_lifecycle_fault_candidate(previous)
                    if not passed:
                        integrity_issues = self.segment_trace_integrity_issues(previous)
                        hook_only_exit = (
                            getattr(self.args, 'random_fault_injection', False) and
                            self.hook_only_workload_exit_allowed(previous, integrity_issues))
                        if hook_only_exit:
                            recovered = read_json(previous / 'manifest.json')
                            recovered['hook_only_workload_exit_recovered'] = True
                            recovered['hook_only_workload_exit_issues'] = sorted(integrity_issues)
                            recovered['issues'].append('hook_only_workload_exit_quarantined')
                            atomic_json(previous / 'manifest.json', recovered)
                            self.progress(f'FAULT WORKLOAD EXIT segment={previous.name} '
                                          'attribution=hook_only syscall_evidence=absent '
                                          'filesystem=quarantined continuing_to_next_workload')
                        elif (not getattr(self.args, 'random_fault_injection', False) or
                              integrity_issues):
                            detail = ','.join(integrity_issues) if integrity_issues else 'lifecycle_validation_failed'
                            raise CampaignError(detail)
                        else:
                            self.progress(f'FAULT CANDIDATE segment={previous.name} '
                                          'lifecycle_mismatch=1 continuing_to_next_workload')
                    self.record_finalized_syscalls(previous)
                    # If the quota is reached during analysis, this segment
                    # has already been rotated and is bounded by the reserved
                    # per-segment headroom. Complete it so the checkpoint
                    # still belongs to a fully captured segment.
                segment_manifest = self.collect_segment(folder)
                previous = folder
                self.pending_segment = None
                fault = (segment_manifest or {}).get('fault_injection', {}) or {}
                if fault.get('hit'):
                    event = fault.get('event') or {}
                    self.progress(f'RANDOM FAULT OBSERVED segment={folder.name} '
                                  f'operation={event.get("operation", "unresolved")} '
                                  f'injected={fault["status"].get("injected")} '
                                  f'seed={fault["seed"]}; workload_completed=1')
                    if fault.get('stable_candidate'):
                        phases = ','.join(fault['stable_candidate']['observations'])
                        self.progress(f'FAULT CANDIDATE segment={folder.name} '
                                      f'source=stable_snapshot phases={phases}; '
                                      'continuing_to_next_workload')
                target_syscalls = getattr(self.args, 'target_syscalls', 0)
                if self.stop_requested:
                    self.meta['stop_reason'] = self.stop_requested
                    break
                if target_syscalls and self.syscall_budget_reached():
                    self.meta['stop_reason'] = 'syscall_budget'
                    break
                if time.monotonic()-active_start >= self.args.duration_seconds:
                    self.meta['stop_reason'] = 'duration_limit'; break
            else:
                self.meta['stop_reason'] = 'segment_limit'
            self.meta['collection_elapsed_seconds'] = round(time.monotonic()-active_start, 3)
            # The collection deadline is intentionally a workload bound.  Set
            # this before checkpoint so transport retries, final analysis and
            # disk evidence cannot be mistaken for an overlong collection.
            self.finalizing = True
            self.meta['finalizing'] = True
            self.save()
            self.progress(f'CHECKPOINT START stop_reason={self.meta["stop_reason"]}')
            self.guest('checkpoint', timeout=90, retry_transport=True)
            self.collector.wait(previous.name, sealed=True)
            self.collector.stop()
            if self.discard_segment is not None:
                shutil.rmtree(self.discard_segment, ignore_errors=True)
                self.discard_segment = None
            if self.pending_segment and self.pending_segment.exists():
                self.merge_pending_trace(previous)
                shutil.rmtree(self.pending_segment)
                self.pending_segment = None
            self.fetch(BASE, self.run / 'checkpoint')
            for p in (self.run / 'checkpoint').glob('cpu*-stats.txt'):
                shutil.copy2(p, previous / p.name)
            self.meta['final_states'] = {k: read_json(self.run / 'checkpoint' / (k+'-final-state.json')) for k in KINDS}
            self.finalize(previous, checkpoint=True)
            self.record_lifecycle_fault_candidate(previous)
            self.verified_syscalls = self.count_verified_syscalls_total(previous)
            self.meta['verified_syscalls'] = self.verified_syscalls
            dmesg = (self.run / 'checkpoint/dmesg.txt').read_text(errors='replace')
            if re.search(r'BUG:|KASAN:|Oops:|possible circular locking|WARNING:',
                         self._filter_expected_fault_warnings(dmesg)):
                raise CampaignError('kernel_diagnostic')
            self.finish_disk()
            self.meta['status'] = 'complete'
            self.save()
        except BaseException as error:
            reason = str(error) or type(error).__name__
            fault = self.kernel_diagnostics()
            if fault:
                self.meta['kernel_fault'] = fault
                # Preserve the transport error as a detail while making the
                # report fail for the actual guest fault.
                if reason in ('trace_reader_eof', 'qemu_crash') or reason.startswith('command_exit_'):
                    reason = fault['kind'] + ':' + reason
            self.meta.update(status='host_interrupted' if self.cancelled else 'failed', stop_reason=reason)
            self.save()
            self.progress('CAMPAIGN STOP reason=' + reason)
            try:
                if hasattr(self, 'ssh_args') and self.qemu and self.qemu.poll() is None:
                    self.guest('stop', timeout=10, salvage=True, allowed=(0, 255))
            except BaseException:
                pass
            if self.collector:
                self.collector.stop()
            if self.current:
                m = read_json(self.current / 'manifest.json')
                if m.get('status') in ('running', 'complete'):
                    m['status'] = 'host_interrupted' if self.cancelled else (
                        'kernel_fault' if fault else
                        'trace_loss' if 'trace' in reason else 'qemu_crash' if 'qemu' in reason else 'incomplete')
                m.update(interrupt_reason=reason, end_wall_ns=time.time_ns())
                if fault:
                    m.setdefault('issues', []).append(fault['kind'])
                atomic_json(self.current / 'manifest.json', m)
                try:
                    if m.get('guest_started'):
                        self.fetch(BASE + '/' + self.current.name + '/.', self.current, salvage=True)
                        if not (self.current / 'analysis/lifecycle/report.json.gz').exists():
                            self.finalize(self.current, salvage=True)
                    else:
                        # ``rotate`` creates only the host-side manifest.  If
                        # failure happened before ``segment`` was invoked,
                        # there is no guest directory and an scp attempt would
                        # replace the primary error with a misleading ENOENT.
                        self.progress(f'SALVAGE SKIP segment={self.current.name} '
                                      'guest_not_started=1')
                except BaseException as salvage_error:
                    m = read_json(self.current / 'manifest.json')
                    m['issues'].append('salvage_error:' + str(salvage_error))
                    atomic_json(self.current / 'manifest.json', m)
            self.save()
        finally:
            if self.collector:
                self.collector.stop()
            if self.qemu and self.qemu.poll() is None:
                os.killpg(self.qemu.pid, signal.SIGTERM)
                try:
                    self.qemu.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.qemu.pid, signal.SIGKILL); self.qemu.wait()
        self.progress('REPORT START aggregating_campaign')
        aggregate_start = time.monotonic()
        report = aggregate(self.run)
        self.record_time('aggregate', time.monotonic()-aggregate_start)
        from performance_report import build as build_performance, render as render_performance
        performance = build_performance(self.run)
        (self.run / 'performance-report.json').write_text(json.dumps(performance, indent=2) + '\n')
        (self.run / 'performance-report.md').write_text(render_performance(performance))
        report['performance'] = performance
        # Campaign-wide streams are useful offline but need not remain as
        # duplicate uncompressed files during a long run.
        for path in (self.run / 'episodes.jsonl', self.run / 'segment-summary.jsonl'):
            if path.exists():
                compress(path, level=6)
        self.meta['elapsed_scope'] = 'prepare_collection_shutdown_disk_checks_and_aggregation'
        if hasattr(self, 'ledger'):
            self.ledger.reconcile()
        self.save()
        report.update(elapsed_seconds=self.meta.get('elapsed_seconds', round(time.monotonic()-self.started, 3)), elapsed_scope=self.meta['elapsed_scope'],
                      stage_timings=getattr(self, 'timings', {}), quota_ledger=self.meta.get('quota_ledger'))
        atomic_json(self.run / 'campaign-report.json', report)
        report_md = self.run / 'campaign-report.md'
        if report_md.exists():
            report_md.write_text(re.sub(r'Elapsed seconds: [^;]*;',
                                      f'Elapsed seconds: {report["elapsed_seconds"]} (including aggregation);',
                                      report_md.read_text()))
        self.progress(f'CAMPAIGN END passed={report["passed"]} report={self.run / "campaign-report.md"}')
        print(json.dumps({k: report[k] for k in ('passed', 'segments', 'coverage_complete', 'issues')}, indent=2), flush=True)
        return 0 if report['passed'] else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--segments', type=int, default=10000, help='hard maximum; six cover the device/mode matrix')
    p.add_argument('--mode', choices=('matrix', 'single', 'concurrent'), default='matrix',
                   help='matrix alternates modes; concurrent workers share and synchronize on one workload tree')
    p.add_argument('--duration-seconds', type=int, default=3600)
    p.add_argument('--target-syscalls', type=int, default=0,
                   help='stop after at least this many generated syscall calls; 0 disables')
    p.add_argument('--segment-seconds', type=int, default=10)
    p.add_argument('--max-batches', type=int, default=1)
    p.add_argument('--batch-timeout', type=int, default=60)
    p.add_argument('--boot-timeout', type=int, default=180)
    p.add_argument('--analysis-timeout', type=int, default=180)
    p.add_argument('--workers', type=int, default=2)
    p.add_argument('--memory-mb', type=int, default=32)
    p.add_argument('--guest-memory-mb', type=int, default=2048)
    p.add_argument('--cpus', type=int, default=2)
    p.add_argument('--qemu-cpu', choices=('qemu64', 'max'), default='qemu64',
                   help='TCG CPU model (qemu64 avoids the known KASAN/TCG boot diagnostic; default: qemu64)')
    p.add_argument('--buffer-kb', type=int, default=65536, help='per CPU')
    p.add_argument('--max-trace-mb', type=int, default=64)
    p.add_argument('--max-run-mb', type=int, default=40960, help='logical file bytes, including images')
    p.add_argument('--min-free-mb', type=int, default=1024)
    p.add_argument('--port', type=int, default=11883)
    p.add_argument('--seed', type=int, default=20261010)
    p.add_argument('--random-fault-injection', action='store_true',
                   help='inject seeded random errors at selected ext4 hooks; isolate private single-worker hits by default')
    p.add_argument('--random-fault-probability-ppm', type=int, default=1000,
                   help='per eligible hook-call probability in parts per million (default 1000 = 0.1 percent)')
    p.add_argument('--random-fault-operations', default=','.join(RANDOM_FAULT_ADMITTED_OPERATIONS),
                   help='comma-separated runtime hooks, or all for every runtime_random hook '
                        '(default reviewed set: ' + ','.join(RANDOM_FAULT_ADMITTED_OPERATIONS) + ')')
    p.add_argument('--random-fault-errnos', default='native',
                   help='comma-separated errnos shared by selected hooks, native for the hit hook whitelist, or hook=errnos;hook=errnos for per-hook choices')
    p.add_argument('--fault-isolation', action=argparse.BooleanOptionalAction, default=True,
                   help='stop a private single-worker workload after its injected syscall and run an immediate offline fsck (default: enabled)')
    p.add_argument('--syzkaller-calls', dest='syz_calls', type=int, default=32,
                   help='generated calls per worker program')
    p.add_argument('--resource-valid-every', type=int, default=2,
                   help='use typed valid namespace prefix every N program slots; 0 disables (default 50 percent)')
    p.add_argument('--fresh-generate-every', type=int, default=5,
                   help='force fresh ordinary Generate every N slots; 0 disables')
    p.add_argument('--scheduler-policy', choices=('uniform_round_robin', 'rarity_fair'),
                   default='uniform_round_robin')
    p.add_argument('--feedback-mode', choices=('pc', 'pc_edge', 'pc_semantic', 'pc_edge_semantic', 'none'),
                   default='pc_edge_semantic', help='signals allowed to admit corpus programs')
    p.add_argument('--target-scope', choices=('all', 'ext4_jbd2'), default='all',
                   help='PC admission scope; ext4_jbd2 makes target PCs the primary signal')
    p.add_argument('--strict-kcov-feedback', action='store_true',
                   help='stop at the next segment boundary after any KCOV feedback gap and fail the campaign')
    p.add_argument('--mutation-budget-policy', choices=('fixed', 'dynamic'), default='fixed')
    p.add_argument('--max-mutation-calls', type=int, default=256)
    p.add_argument('--scenario', choices=('baseline', 'stateful', 'large-dir', 'pressure', 'all'),
                   default='baseline',
                   help='persistent resource profile for long-lived and pressure experiments')
    p.add_argument('--predecessor', help='previous interrupted run; evidence remains in its own directory')
    p.add_argument('--rootfs-backing', help='raw root filesystem backing image (defaults to work/debian-clean-smalljournal.img, then work/debian.img)')
    a = p.parse_args()
    bounds = dict(segments=(1,100000), duration_seconds=(1,43200), target_syscalls=(0,10000000), segment_seconds=(1,300),
                  max_batches=(1,20), batch_timeout=(1,300), boot_timeout=(30,600), analysis_timeout=(10,600), workers=(1,8),
                  memory_mb=(0,256), guest_memory_mb=(1024,8192), cpus=(1,8), buffer_kb=(1024,65536),
                  max_trace_mb=(1,256), max_run_mb=(32,102400), min_free_mb=(0,102400),
                  port=(1024,65535), seed=(1,2**31-1), syz_calls=(1,256), resource_valid_every=(0,1000),
                  fresh_generate_every=(0,1000), max_mutation_calls=(1,256),
                  random_fault_probability_ppm=(1,1000000))
    for key, (lo, hi) in bounds.items():
        if not lo <= getattr(a, key) <= hi:
            p.error(f'{key} must be in [{lo}, {hi}]')
    shared_concurrency = a.workers > 1 and a.mode in ('concurrent', 'matrix')
    if (a.resource_valid_every or shared_concurrency) and a.syz_calls < 16:
        p.error("typed seed and shared-contention modes require --syzkaller-calls >= 16")
    if a.mutation_budget_policy == 'dynamic' and a.scheduler_policy != 'rarity_fair':
        p.error('--mutation-budget-policy dynamic requires --scheduler-policy rarity_fair')
    try:
        validate_random_fault_mode(a)
    except CampaignError as error:
        p.error(str(error))
    try:
        random_fault_policy(a, a.seed, 0)
    except CampaignError as error:
        p.error(str(error))
    runner = Runner(a)
    def interrupted(signum, _frame):
        reason = 'signal_' + signal.Signals(signum).name
        if runner.signal_stop_requested:
            runner.cancelled = reason
            return
        runner.signal_stop_requested = True
        runner.stop_requested = reason
        runner.progress('GRACEFUL STOP REQUEST reason=' + reason)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    return runner.execute()


if __name__ == '__main__':
    sys.exit(main())
