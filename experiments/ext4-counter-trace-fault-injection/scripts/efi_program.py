"""Bind syzkaller C exports to native-syscall-scoped program EFI sessions."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
BASE = PROJECT.parent/'ext4-counter-trace'
sys.path.insert(0, str(BASE/'scripts'))
import syz_workload


def runtime():
    text = (BASE/'tests/syz-runtime.h').read_text()
    new = (PROJECT/'tests/efi-runtime.h').read_text()
    start = text.index('static int ct_kcov_fd')
    text = text[:start] + new + '\n' + text[start:]
    replacements = [
        ('  /* Keep fallback descriptors valid after generated rename/unlink operations.\n'
         '   * The directory entry may disappear, but an open regular file remains a\n'
         '   * valid target for later data syscalls. */\n'
         '  int seed_fd = open("./file0/file0", O_RDWR | O_CLOEXEC);\n'
         '  if (seed_fd < 0) seed_fd = open("./file0/data", O_RDWR | O_CLOEXEC);\n'
         '  if (seed_fd < 0) { perror("stable data file"); exit(91); }',
         '  /* The input holder keeps this same inode alive across all replays. */\n'
         '  int seed_fd = efi_open_stable_file();'),
        ('  if (ct_stable_fd >= 0) close(ct_stable_fd);\n'
         '  ct_stable_fd = open("./file0/file0", O_RDWR | O_CLOEXEC);\n'
         '  if (ct_stable_fd < 0) ct_stable_fd = open("./file0/data", O_RDWR | O_CLOEXEC);\n'
         '  if (ct_stable_fd < 0) _exit(91);\n'
         '  return ct_stable_fd;',
         '  dprintf(2, "stable data descriptors lost\\n");\n'
         '  _exit(91);'),
        ('  return fd >= 0 && (fd == ct_stable_fd || fd == ct_stable_fd_backup);',
         '  return fd >= 0 && (fd == ct_stable_fd || fd == ct_stable_fd_backup || fd == efi_fd);'),
        ('  ct_phase("END", "runtime_init");\n}', '  ct_phase("END", "runtime_init");\n  efi_init();\n}'),
        ('  long ret = syscall(nr,a,b,c,d,e,f);',
         '  efi_call_begin(index, nr, object_dev);\n  errno = 0;\n  long ret = syscall(nr,a,b,c,d,e,f);'),
        ('  int error = ret == -1 ? errno : 0;\n  ct_kcov_stop();',
         '  int error = ret == -1 ? errno : 0;\n  efi_call_end();\n  ct_kcov_stop();'),
    ]
    for before, after in replacements:
        if text.count(before) != 1: raise ValueError(f'runtime interface changed: {before}')
        text = text.replace(before, after)
    return text


def export(folder):
    folder = Path(folder)
    meta = json.loads((folder/'program.json').read_text())
    text = syz_workload.instrument((folder/'original.c').read_text(), meta, runtime())
    (folder/'workload.c').write_text(text)
    subprocess.run(['gcc', '-O2', '-static', '-pthread', '-o', str(folder/'workload'), str(folder/'workload.c')], check=True, timeout=90)
    meta['efi_session'] = 'native_syscall_windows_program_formula_gate'
    meta['sha256'] = {name: hashlib.sha256((folder/name).read_bytes()).hexdigest()
                      for name in ('program.syz', 'original.c', 'workload.c', 'workload')}
    (folder/'program.json').write_text(json.dumps(meta, indent=2)+'\n')
    return meta


def generate(folder, seed, calls, parent=None, template='namespace'):
    folder = Path(folder)
    generator = BASE/'build/ct-syzgen'
    if not generator.is_file(): raise ValueError('missing ct-syzgen; run tools/build-syzkaller.sh')
    argv = [str(generator), '-out', str(folder), '-seed', str(seed), '-calls', str(calls),
            '-input-mode', 'resource-valid', '-resource-template', template]
    if parent: argv += ['-mutate', str(parent)]
    subprocess.run(argv, check=True, timeout=90)
    return export(folder)
