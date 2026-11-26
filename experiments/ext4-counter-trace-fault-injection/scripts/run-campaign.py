#!/usr/bin/env python3
"""Run program-level, formula-gated ext4 fault mutation on one persistent mount."""
from __future__ import annotations
import argparse
import collections
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from efi_program import PROJECT, BASE, export, generate
from efi_feedback import analyze_round, identity, mutate, plan_text

ROOT = PROJECT.parents[1]


def dump(path, value): Path(path).write_text(json.dumps(value, indent=2)+'\n')
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def next_plan(queue, variant):
    # Alternate ordinary breadth with a combination already learned from a
    # previous replay, so a large first frontier cannot starve multi-fault runs.
    index = next((i for i, plan in enumerate(queue) if len(plan) > 1), 0) if variant % 2 == 0 else 0
    queue.rotate(-index)
    plan = queue.popleft()
    queue.rotate(index)
    return plan


def command(argv, timeout=90, **kwargs):
    return subprocess.run([str(v) for v in argv], check=True, timeout=timeout, **kwargs)


def write_progress(path, status, reports, corpus, signatures, started,
                   current_program=None, current_variant=None):
    plans = [plan for report in reports for plan in report['plans']]
    dump(path, dict(status=status, completed_programs=len({r['program'] for r in reports}),
                    current_program=current_program, current_variant=current_variant,
                    rounds=len(reports),
                    injection_rounds=sum(r.get('session', {}).get('injected', 0) > 0 for r in reports),
                    injections=sum(r.get('session', {}).get('injected', 0) for r in reports),
                    blocked=sum(p['status'] == 'blocked' for p in plans),
                    not_reached=sum(p['status'] == 'not_reached' for p in plans),
                    distinct_joint_sequences=len(signatures),
                    corpus=[str(p) for p in corpus],
                    elapsed_seconds=round(time.monotonic()-started, 2)))


def verify_build():
    obj = PROJECT/'build/linux-error-site-trace'
    binding = json.loads((obj/'build-binding.json').read_text())
    paths = {'sites': PROJECT/'sites.json', 'patch': PROJECT/'patches/0001-reviewed-error-site-trace.patch',
             'injection': PROJECT/'injection.json', 'injection_patch': PROJECT/'patches/0002-formula-gated-injection.patch',
             'base_patch': BASE/'patches/0001-counter-trace-core-and-probes.patch',
             'source_audit': obj/'source-audit.json',
             'config': obj/'.config', 'kernel': obj/'arch/x86/boot/bzImage'}
    for name, path in paths.items():
        if binding.get(name) != sha(path): raise ValueError(f'kernel build binding changed: {name}; run build.sh')
    return obj, paths


class Guest:
    def __init__(self, run, args, kernel):
        self.run, self.args, self.process = run, args, None
        self.seed_holders = {}
        self.ssh_options = ['-i', args.key, '-p', str(args.port), '-o', 'BatchMode=yes',
                            '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=no',
                            '-o', 'UserKnownHostsFile=/dev/null', '-o', 'LogLevel=ERROR']
        command(['qemu-img','create','-q','-f','qcow2','-F','raw','-b',args.rootfs,run/'root.qcow2'])
        with (run/'ext4.raw').open('wb') as stream: stream.truncate(args.disk_mb*1024*1024)
        options = ['-O','^orphan_file','-b','4096','-N','4096','-E','lazy_itable_init=0']
        if args.inline: options += ['-O','inline_data,^orphan_file','-I','512']
        command(['mke2fs','-q','-F','-t','ext4',*options,run/'ext4.raw'])
        self.log = (run/'serial.log').open('w')
        self.process = subprocess.Popen(['qemu-system-x86_64','-machine','q35,accel=tcg','-cpu','qemu64',
            '-m','2048','-smp','2','-kernel',str(kernel),'-drive',f'file={run}/root.qcow2,format=qcow2,if=virtio',
            '-drive',f'file={run}/ext4.raw,format=raw,if=virtio','-netdev',f'user,id=n0,hostfwd=tcp:127.0.0.1:{args.port}-:22',
            '-device','virtio-net-pci,netdev=n0','-append','root=/dev/vda rw console=ttyS0 net.ifnames=0',
            '-display','none','-monitor','none','-serial','stdio'], stdout=self.log, stderr=subprocess.STDOUT)

    def ssh(self, text, timeout=90, check=True):
        result = subprocess.run(['ssh',*self.ssh_options,'root@127.0.0.1',text],
                                capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f'guest command failed: {text}\n{result.stdout}\n{result.stderr}')
        return result

    def copy(self, source, dest, remote=False):
        opts = [str(v) for v in self.ssh_options]
        opts[opts.index('-p')] = '-P'
        source, dest = str(source), str(dest)
        if remote: source = 'root@127.0.0.1:' + source
        else: dest = 'root@127.0.0.1:' + dest
        command(['scp','-q','-r',*opts,source,dest], timeout=180)

    def ready(self):
        deadline = time.monotonic()+240
        while time.monotonic()<deadline:
            if self.process.poll() is not None: raise RuntimeError('QEMU exited during boot')
            result = self.ssh('mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null || true; test -e /sys/kernel/debug/ext4_efi/session', check=False)
            if not result.returncode: return
            time.sleep(1)
        raise TimeoutError('VM did not expose ext4 EFI session')

    def setup(self):
        self.ssh('''set -eu
mkdir -p /mnt/efi /tmp/efi /sys/kernel/tracing
mount /dev/vdb /mnt/efi
mount -t tracefs tracefs /sys/kernel/tracing 2>/dev/null || true
echo 0 > /sys/kernel/tracing/tracing_on
echo global > /sys/kernel/tracing/trace_clock
echo 65536 > /sys/kernel/tracing/buffer_size_kb
echo 1 > /sys/kernel/tracing/events/ext4_counter/ext4_counter_event/enable
echo 1 > /sys/kernel/tracing/events/ext4_efi/efi_site/enable
echo 1 > /sys/kernel/tracing/events/ext4_efi/efi_formula/enable
echo 1 > /sys/kernel/tracing/events/raw_syscalls/sys_enter/enable
echo 1 > /sys/kernel/tracing/events/raw_syscalls/sys_exit/enable
cat /proc/sys/kernel/random/boot_id > /tmp/efi/boot-id.txt
''')
        self.copy(PROJECT/'tests/guest-round.sh','/tmp/efi/round.sh')
        self.copy(self.run/'seed-holder','/tmp/efi/seed-holder')

    def start_seed_holder(self, ident, root):
        """Keep a stable-file inode reachable when this input renames/unlinks it."""
        remote_dir = f'/tmp/efi/seeds/{ident}'
        ready_path = f'{remote_dir}/ready.json'
        self.ssh(f'mkdir -p {remote_dir}; rm -f {ready_path} {ready_path}.tmp {remote_dir}/holder.log; '
                 f'nohup /tmp/efi/seed-holder {root}/file0/file0 {ready_path} '
                 f'>/tmp/efi/seeds/{ident}/holder.log 2>&1 </dev/null &')
        deadline = time.monotonic() + 20
        metadata = None
        while time.monotonic() < deadline:
            result = self.ssh(f'test -s {ready_path} && cat {ready_path}', check=False)
            if result.returncode == 0:
                try: metadata = json.loads(result.stdout)
                except json.JSONDecodeError: time.sleep(0.1); continue
                break
            time.sleep(0.2)
        if not metadata:
            raise RuntimeError(f'seed holder failed to publish its inode for {ident}')
        holder = dict(ident=ident, pid=int(metadata['pid']),
                      reference=metadata['reference'], dev=int(metadata['dev']),
                      ino=int(metadata['ino']), ready=ready_path)
        self.seed_holders[ident] = holder
        print(f"seed_holder program={ident} pid={holder['pid']} dev={holder['dev']} ino={holder['ino']}",flush=True)
        return holder

    def stop_seed_holder(self, ident):
        holder = self.seed_holders.pop(ident, None)
        if not holder: return
        self.ssh(f'kill -TERM {holder["pid"]}; '
                 f'for attempt in $(seq 1 100); do '
                 f'if ! kill -0 {holder["pid"]} 2>/dev/null; then break; fi; '
                 f'sleep 0.05; done; test ! -e {holder["ready"]}', timeout=15)

    def discard_remote_round(self, remote):
        result = self.ssh(f'rm -rf -- {remote}', timeout=30, check=False)
        if result.returncode:
            raise RuntimeError(f'could not remove guest round directory {remote}: '
                               f'exit={result.returncode} {result.stderr.strip()}')

    def stop_all_seed_holders(self):
        for ident in list(self.seed_holders):
            self.stop_seed_holder(ident)

    def prepare_root(self, ident):
        # Preparation happens once per NEW input, never between its fault
        # variants. A modified tree remains modified for the next replay.
        root = f'/mnt/efi/{ident}'
        self.ssh(f'''set -eu
mkdir -p {root}/file0 {root}/file1 {root}/file2 {root}/file3 {root}/dir0 {root}/empty0 {root}/srcdir {root}/dstdir
for name in file0/file0 file1/data dir0/file0 source replacement exchange0 exchange1; do
  printf 'counter-trace-seed\n' > {root}/$name
done
''')
        return root

    def poweroff(self):
        self.stop_all_seed_holders()
        self.ssh('umount /mnt/efi && systemctl poweroff')
        self.process.wait(timeout=60)
        self.log.close()

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=10)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait(timeout=10)
        self.log.close()


    def recover(self):
        """Collect partial trace before stopping a timed-out/failed campaign."""
        if self.process.poll() is not None: return
        result = self.ssh('''echo 0 > /sys/kernel/tracing/tracing_on
cat /sys/kernel/tracing/trace > /tmp/efi/emergency-trace.txt
dmesg > /tmp/efi/emergency-dmesg.txt
''', timeout=15, check=False)
        (self.run/'recovery.txt').write_text(result.stdout+result.stderr)
        if not result.returncode:
            self.copy('/tmp/efi/.', self.run/'guest-recovery', remote=True)
        for holder in self.seed_holders.values():
            self.ssh(f'kill -TERM {holder["pid"]} 2>/dev/null || true', timeout=5, check=False)
        self.seed_holders.clear()
        self.ssh('umount /mnt/efi; systemctl poweroff', timeout=15, check=False)
        try: self.process.wait(timeout=30)
        except subprocess.TimeoutExpired: pass


def fixture(folder, kind):
    """Deterministic regression inputs; not counted as syzkaller generation."""
    folder.mkdir(parents=True)
    names = [('openat$dir','openat',257)]
    code = ['long dir = syscall(__NR_openat, AT_FDCWD, ".", O_RDONLY|O_DIRECTORY, 0);']
    if kind == 'cycle':
        for name, nr, text in [('mkdirat',258,'syscall(__NR_mkdirat, dir, "cycle", 0700);'),
                               ('unlinkat',263,'syscall(__NR_unlinkat, dir, "cycle", AT_REMOVEDIR);'),
                               ('mkdirat',258,'syscall(__NR_mkdirat, dir, "cycle", 0700);'),
                               ('unlinkat',263,'syscall(__NR_unlinkat, dir, "cycle", AT_REMOVEDIR);')]:
            names.append((name,name,nr)); code.append(text)
    names.append(('close','close',3)); code.append('syscall(__NR_close, dir);')
    text = '#define _GNU_SOURCE\n#include <unistd.h>\n#include <fcntl.h>\n#include <sys/syscall.h>\nint main(void) {\n'+'\n'.join(code)+'\nreturn 0;\n}\n'
    (folder/'original.c').write_text(text)
    (folder/'program.syz').write_text('# regression fixture '+kind+'\n')
    dump(folder/'program.json', dict(source='regression_fixture',calls=[dict(index=i,name=n,native_name=nn,nr=nr) for i,(n,nn,nr) in enumerate(names)]))
    return export(folder)


def saved_program(folder, source):
    metadata = json.loads((source/'program.json').read_text())
    if not 1 <= len(metadata['calls']) <= 256:
        raise ValueError('saved program must contain 1..256 sequential calls')
    for name in ('original.c', 'program.syz'):
        expected = metadata.get('sha256', {}).get(name)
        if expected and sha(source/name) != expected:
            raise ValueError(f'saved input hash changed: {source/name}')
    folder.mkdir()
    for name in ('original.c', 'program.syz', 'program.json'):
        shutil.copy2(source/name, folder/name)
    # Re-export with the current runtime, and re-observe contexts on the new
    # mount. Old raw stack fingerprints are never assumed valid after reboot.
    return export(folder)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--programs',type=int,default=4)
    parser.add_argument('--replays-per-program',type=int,default=16)
    parser.add_argument('--calls',type=int,default=16)
    parser.add_argument('--seed',type=int,default=1)
    parser.add_argument('--template',choices=['namespace','file-state'],default='namespace')
    parser.add_argument('--program-timeout',type=int,default=45)
    parser.add_argument('--duration-seconds',type=int,default=3600)
    parser.add_argument('--port',type=int,default=11882)
    parser.add_argument('--disk-mb',type=int,default=260)
    parser.add_argument('--inline',action='store_true')
    parser.add_argument('--regression',action='store_true')
    parser.add_argument('--program-dir',type=Path,help='re-explore a saved program with a fresh baseline trace and fault plans')
    parser.add_argument('--rootfs',type=Path,default=ROOT/'work/debian-normal.img')
    parser.add_argument('--key',type=Path,default=ROOT/'work/debian.id_rsa')
    args = parser.parse_args()
    if args.program_dir and args.regression: parser.error('--program-dir and --regression are mutually exclusive')
    if args.program_dir:
        args.program_dir = args.program_dir.resolve()
        for name in ('original.c', 'program.syz', 'program.json'):
            if not (args.program_dir/name).is_file(): parser.error(f'missing saved input: {args.program_dir/name}')
        args.programs = 1
    args.rootfs = args.rootfs.resolve()
    args.key = args.key.resolve()
    if not (1<=args.programs<=10000 and 0<=args.replays_per_program<=10000 and 16<=args.calls<=256 and
            1<=args.program_timeout<=300 and 60<=args.duration_seconds<=43200 and 132<=args.disk_mb<=4096):
        parser.error('invalid bounded program, replay, call, timeout, duration or disk setting')
    obj, paths = verify_build()
    command([sys.executable, PROJECT/'scripts/configure-injection.py','--check','--prepare-source'])
    (PROJECT/'results').mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix='campaign-',dir=PROJECT/'results')).resolve()
    print(f'run_dir={run}',flush=True)
    for name in ('sites','injection','source_audit','config','kernel'): shutil.copy2(paths[name],run/paths[name].name)
    shutil.copy2(obj/'build-binding.json',run/'build-binding.json')
    for name in ('efi-runtime.h','guest-round.sh'): shutil.copy2(PROJECT/'tests'/name,run/name)
    shutil.copy2(PROJECT/'tests/seed-holder.c',run/'seed-holder.c')
    command(['gcc-14','-O2','-static','-Wall','-Wextra','-Werror','-o',
             run/'seed-holder',PROJECT/'tests/seed-holder.c'], timeout=60)
    sources = [PROJECT/'scripts'/name for name in ('run-campaign.py', 'efi_feedback.py', 'efi_program.py', 'configure-injection.py', 'efi_source_audit.py')]
    sources += [BASE/'scripts'/name for name in ('trace_lifecycle.py', 'point_report.py', 'syz_workload.py', 'campaign.py')]
    sources += [PROJECT/'tests/seed-holder.c', PROJECT/'tests/efi-runtime.h',
                PROJECT/'tests/guest-round.sh', BASE/'tests/syz-runtime.h',
                ROOT/'tools/syzkaller-revision.txt']
    for source in sources:
        destination = run/'host-source'/source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source,destination)
    dump(run/'host-binding.json',{str(path.relative_to(ROOT)):sha(path) for path in sources})
    dump(run/'options.json',{k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()})
    policies = json.loads(paths['injection'].read_text())['policies']
    guest, reports, corpus, signatures = None, [], [], set()
    started = time.monotonic()
    status, failure = 'running', None
    counter = 0
    try:
        guest = Guest(run,args,run/paths['kernel'].name)
        guest.ready(); guest.setup()
        write_progress(run/'progress.json','running',reports,corpus,signatures,started)
        kinds = ['cycle','noformula'] if args.regression else [None]*args.programs
        for number, kind in enumerate(kinds):
            if time.monotonic()-started >= args.duration_seconds: break
            folder = run/f'program-{number:04d}'
            parent = corpus[number % len(corpus)] if corpus and number%2 else None
            meta = (saved_program(folder,args.program_dir) if args.program_dir else
                    fixture(folder,kind) if kind else generate(folder,args.seed+number,args.calls,parent,args.template))
            root = guest.prepare_root(folder.name)
            holder = guest.start_seed_holder(folder.name,root)
            dump(folder/'seed-holder.json',holder)
            guest.copy(folder/'workload',f'/tmp/efi/{folder.name}')
            queue, seen, variants = collections.deque([()]), {()}, 0
            while queue and variants <= args.replays_per_program and time.monotonic()-started < args.duration_seconds:
                plan = next_plan(queue, variants)
                counter+=1
                round_dir = folder/f'round-{variants:04d}'
                round_dir.mkdir()
                (round_dir/'plan.txt').write_text(plan_text(plan))
                dump(round_dir/'plan.json',[dict(zip(('call','site_id','occurrence','context'),p)) for p in plan])
                remote = f'/tmp/efi/r{counter}'
                guest.ssh(f'mkdir -p {remote}')
                guest.copy(round_dir/'plan.txt',remote+'/plan.txt')
                elapsed = time.monotonic()-started
                remaining = max(0, args.duration_seconds-elapsed)
                print(f'program={number} variant={variants} run={counter} '
                      f'elapsed={elapsed:.0f}s remaining={remaining:.0f}s '
                      f'planned_failures={len(plan)}',flush=True)
                dump(round_dir/'round-state.json',dict(run=counter,stage='executing',remote=remote))
                try:
                    result = guest.ssh(f'sh /tmp/efi/round.sh {remote} /tmp/efi/{folder.name} {root} '
                                       f'{counter} {args.program_timeout} {holder["reference"]} '
                                       f'{holder["dev"]} {holder["ino"]}',
                                       timeout=args.program_timeout+180,check=False)
                    (round_dir/'guest.txt').write_text(result.stdout+result.stderr)
                    guest.copy(remote+'/.',round_dir,remote=True)
                except BaseException:
                    # Preserve the original failure while making a best-effort
                    # cleanup if result collection itself failed.
                    try: guest.discard_remote_round(remote)
                    except Exception as cleanup_error:
                        print(f'remote_round_cleanup_failed path={remote} '
                              f'error={cleanup_error!r}', file=sys.stderr, flush=True)
                    raise
                # Per-round traces are copied to the host; leaving them under
                # the guest's small root filesystem eventually exhausts it.
                guest.discard_remote_round(remote)
                dump(round_dir/'round-state.json',dict(run=counter,stage='collected',remote=remote,guest_exit=result.returncode))
                if result.returncode:
                    raise RuntimeError(f'guest round collection failed: run={counter} '
                                       f'exit={result.returncode}; see {round_dir}/guest.txt')
                feedback = analyze_round(round_dir,meta,policies,plan,counter)
                seed_stats=[]
                for name in ('seed-before.json','seed-after.json'):
                    path=round_dir/name
                    if not path.is_file():
                        feedback['issues'].append('missing_'+name)
                        continue
                    observed=json.loads(path.read_text())
                    if (int(observed['dev']),int(observed['ino'])) != (holder['dev'],holder['ino']):
                        feedback['issues'].append('stable_seed_inode_changed:'+name)
                    seed_stats.append(dict(stage=name.removesuffix('.json'),nlink=int(observed['nlink']),
                                           size=int(observed['size'])))
                if len(seed_stats)==2:
                    feedback['stable_seed_inode'] = dict(dev=holder['dev'],ino=holder['ino'],
                                                         samples=seed_stats)
                if result.returncode:
                    feedback['issues'].append(f'guest_round_exit:{result.returncode}')
                    feedback['complete'] = False
                novelty = feedback['complete'] and feedback['signature'] not in signatures
                if novelty:
                    signatures.add(feedback['signature'])
                    if folder/'program.syz' not in corpus and not kind: corpus.append(folder/'program.syz')
                    actual_plan = tuple(sorted(identity(p) for p in feedback['actual_faults']))
                    for seed_plan in sorted({tuple(plan), actual_plan}):
                        queue.extend(mutate(seed_plan,feedback['candidates'],seen))
                if kind == 'noformula' and variants == 0:
                    # A real site reached BEFORE a formula must remain blocked,
                    # even though the preceding program opened its own gate.
                    events=[json.loads(line) for line in (round_dir/'joint.jsonl').read_text().splitlines()]
                    early=next((e for e in events if e['kind']=='error' and e['phase']=='enter'),None)
                    if early:
                        missing = list(identity(early)); missing[2] += 1000000
                        queue.append(tuple(sorted((identity(early), tuple(missing)))))
                feedback.update(program=folder.name,variant=variants,novel=novelty,planned=len(plan),
                                report=str((round_dir/'feedback.json').relative_to(run)))
                dump(round_dir/'feedback.json',feedback)
                dump(round_dir/'round-state.json',dict(run=counter,stage='analyzed',remote=remote))
                reports.append(feedback)
                write_progress(run/'progress.json','running',reports,corpus,signatures,started,
                               current_program=number,current_variant=variants)
                print(f"  eligible={len(feedback['candidates'])} injected={feedback.get('session',{}).get('injected',0)} "
                      f"complete={feedback['complete']} checker={feedback['checker_passed']}",flush=True)
                variants+=1
                if feedback['stable_mismatch'] or feedback['checker_finding'] or feedback['kernel_diagnostics']:
                    status='finding'; break
                if not feedback['complete']:
                    status='incomplete'; break
            guest.stop_seed_holder(folder.name)
            if status in ('finding','incomplete'): break
        if status=='running': status='complete' if reports else 'incomplete'
        guest.poweroff()
        fsck = subprocess.run(['e2fsck','-fn',str(run/'ext4.raw')],capture_output=True,text=True,timeout=120)
        (run/'e2fsck.txt').write_text(fsck.stdout+fsck.stderr)
        dump(run/'offline.json',dict(e2fsck_exit=fsck.returncode))
        if fsck.returncode: status='finding'
    except BaseException as exc:
        status='interrupted' if isinstance(exc,KeyboardInterrupt) else 'error'
        failure=repr(exc)
        print(failure,file=sys.stderr)
        (run/'exception.txt').write_text(traceback.format_exc())
        if guest:
            try: guest.recover()
            except BaseException as recovery:
                (run/'recovery-error.txt').write_text(repr(recovery)+'\n')
    finally:
        if guest: guest.close()
        summary=dict(schema=1,status=status,error=failure,state_policy='continuous_mounted_filesystem',
                     programs=len({r['program'] for r in reports}),rounds=len(reports),
                     injection_rounds=sum(r.get('session',{}).get('injected',0)>0 for r in reports),
                     injections=sum(r.get('session',{}).get('injected',0) for r in reports),
                     distinct_joint_sequences=len(signatures),
                     not_reached=sum(p['status']=='not_reached' for r in reports for p in r['plans']),
                     blocked=sum(p['status']=='blocked' for r in reports for p in r['plans']),
                     corpus=[str(p.relative_to(run)) for p in corpus],
                     elapsed_seconds=round(time.monotonic()-started,2),reports=reports)
        if args.regression:
            noformula=[r for r in reports if r['program']=='program-0001']
            summary['regression_passed']=bool(status=='complete' and noformula and
                all(not r['formula_events'] and not r['candidates'] and not r.get('session',{}).get('injected') for r in noformula) and
                any(p['status']=='blocked' for r in noformula for p in r['plans']) and
                any(p['status']=='not_reached' for r in noformula for p in r['plans']) and
                any(r.get('session',{}).get('injected',0)>1 for r in reports))
        dump(run/'report.json',summary)
        write_progress(run/'progress.json',status,reports,corpus,signatures,started)
        print(json.dumps({k:v for k,v in summary.items() if k!='reports'},indent=2),flush=True)
    return 0 if status=='complete' and (not args.regression or summary['regression_passed']) else 1


if __name__=='__main__': raise SystemExit(main())
