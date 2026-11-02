#!/usr/bin/env python3
"""Shared errno policy for runtime configuration and generated kernel checks."""
from __future__ import annotations

import json
import pathlib
import re

PROJECT = pathlib.Path(__file__).resolve().parents[1]
CATALOG_PATH = PROJECT / 'fault-errno-catalog.json'


def load_catalog(path: pathlib.Path = CATALOG_PATH) -> dict:
    catalog = json.loads(path.read_text())
    if catalog.get('schema') != 1 or not isinstance(catalog.get('hooks'), dict):
        raise ValueError('invalid_fault_errno_catalog')
    default_errno = catalog.get('default_errno')
    if default_errno != 'native' and (
            not isinstance(default_errno, str) or
            not re.fullmatch(r'E[A-Z0-9_]+', default_errno)):
        raise ValueError('invalid_fault_errno_default')
    admitted = catalog.get('random_admission', [])
    if (not isinstance(admitted, list) or
            any(not isinstance(item, str) or not item for item in admitted) or
            len(set(admitted)) != len(admitted)):
        raise ValueError('invalid_fault_errno_random_admission')
    global_codes = {}
    for hook, definition in catalog['hooks'].items():
        if not isinstance(definition, dict):
            raise ValueError('invalid_fault_errno_hook:' + hook)
        entries = definition.get('native_errnos')
        sources = definition.get('source')
        if (not hook or not definition.get('kernel_operation') or
                not isinstance(definition.get('return_path'), str) or
                not definition['return_path'].strip() or
                not isinstance(sources, list) or not sources or
                any(not isinstance(source, str) or not source.strip() for source in sources) or
                not isinstance(entries, list) or not entries or len(entries) > 8):
            raise ValueError('invalid_fault_errno_hook:' + hook)
        if any(not isinstance(item, dict) or
               not isinstance(item.get('name'), str) or
               not re.fullmatch(r'E[A-Z0-9_]+', item['name']) or
               not isinstance(item.get('code'), int) or isinstance(item['code'], bool) or
               item['code'] <= 0 or
               (('aliases' in item) and
                (not isinstance(item['aliases'], list) or
                 any(not isinstance(alias, str) or
                     not re.fullmatch(r'E[A-Z0-9_]+', alias) or
                     alias == item['name'] for alias in item['aliases']) or
                 len(set(item['aliases'])) != len(item['aliases']))) or
               not isinstance(item.get('source'), str) or not item['source'].strip()
               for item in entries):
            raise ValueError('invalid_fault_errno_entries:' + hook)
        names = [item['name'] for item in entries]
        aliases = [alias for item in entries for alias in item.get('aliases', [])]
        codes = [item['code'] for item in entries]
        if (len(set(names)) != len(names) or len(set(aliases)) != len(aliases) or
                set(names) & set(aliases) or len(set(codes)) != len(codes) or
                not names):
            raise ValueError('invalid_fault_errno_entries:' + hook)
        for item in entries:
            previous = global_codes.setdefault(item['name'], item['code'])
            if previous != item['code']:
                raise ValueError('conflicting_fault_errno_code:' + item['name'])
            for alias in item.get('aliases', []):
                previous = global_codes.setdefault(alias, item['code'])
                if previous != item['code']:
                    raise ValueError('conflicting_fault_errno_code:' + alias)
    unknown_admitted = sorted(set(admitted) - set(catalog['hooks']))
    if unknown_admitted:
        raise ValueError('unknown_fault_errno_random_admission:' + ','.join(unknown_admitted))
    return catalog


def random_admission_hooks(catalog: dict | None = None) -> list[str]:
    """Return hooks with a reviewed runtime fixture/return-chain contract.

    ``runtime_random`` describes a kernel-capable hook.  Admission is a
    separate, deliberately small registry so adding a new source hook cannot
    silently expand a long-running campaign's random fault surface.
    """
    catalog = catalog or load_catalog()
    return list(catalog.get('random_admission', []))


def errno_choices(hook: str) -> list[str]:
    catalog = load_catalog()
    if hook not in catalog['hooks']:
        return []
    return [item['name'] for item in catalog['hooks'][hook]['native_errnos']]


def errno_codes(hook: str) -> list[int]:
    catalog = load_catalog()
    if hook not in catalog['hooks']:
        return []
    return [item['code'] for item in catalog['hooks'][hook]['native_errnos']]


def normalize_errno_choices(value: str) -> list[str]:
    if value == 'native':
        return ['native']
    raw_choices = [item.strip().upper() for item in value.split(',')]
    catalog = load_catalog()
    names_by_code = {
        item['code']: item['name']
        for definition in catalog['hooks'].values()
        for item in definition['native_errnos']
    }
    names_by_alias = {
        alias: item['name']
        for definition in catalog['hooks'].values()
        for item in definition['native_errnos']
        for alias in item.get('aliases', [])
    }
    choices = []
    for item in raw_choices:
        if item.startswith('-') and item[1:].isdigit():
            item = names_by_code.get(int(item[1:]), item)
        else:
            item = item.removeprefix('-')
            item = names_by_alias.get(item, item)
        choices.append(item)
    if not choices or any(not item for item in choices) or len(set(choices)) != len(choices):
        raise ValueError('invalid_random_fault_errnos')
    known = {item['name'] for definition in catalog['hooks'].values()
             for item in definition['native_errnos']}
    if any(item not in known for item in choices):
        raise ValueError('unsupported_random_fault_errno:' + ','.join(choices))
    return choices


def normalize_errno_selection(value: str, operations: list[str]) -> dict[str, list[str]]:
    """Normalize a global or per-hook errno policy.

    The original comma-separated form applies one list to every selected hook.
    A semicolon-separated ``hook=list`` form lets hooks with different native
    error sets run in the same segment, for example
    ``mballoc_mark=EROFS;inode_evict_final_dirty=EFSCORRUPTED,ENOSPC``.
    """
    if not operations or len(set(operations)) != len(operations):
        raise ValueError('invalid_random_fault_operations')
    if '=' not in value:
        selected = normalize_errno_choices(value)
        return {operation: selected[:] for operation in operations}
    if not value or any(not item.strip() for item in value.split(';')):
        raise ValueError('invalid_random_fault_errno_policy')
    by_operation: dict[str, list[str]] = {}
    for item in value.split(';'):
        if '=' not in item:
            raise ValueError('invalid_random_fault_errno_policy')
        operation, choices = item.split('=', 1)
        operation = operation.strip()
        if operation not in operations or operation in by_operation:
            raise ValueError('invalid_random_fault_errno_policy:' + operation)
        by_operation[operation] = normalize_errno_choices(choices.strip())
    missing = sorted(set(operations) - set(by_operation))
    if missing:
        raise ValueError('missing_random_fault_errno_policy:' + ','.join(missing))
    return by_operation


def render_errno_selection(selection: dict[str, list[str]]) -> str:
    """Render a normalized per-hook policy for the kernel arm grammar."""
    return ';'.join(f'{operation}={",".join(choices)}'
                    for operation, choices in selection.items())


def validate_selection(operations: list[str], choices: list[str]) -> None:
    if choices == ['native']:
        return
    unsupported = sorted({f'{operation}:{choice}' for operation in operations
                          for choice in choices if choice not in errno_choices(operation)})
    if unsupported:
        raise ValueError('unsupported_random_fault_errno_choices:' + ','.join(unsupported))


def generate_kernel_header(catalog: dict | None = None) -> str:
    catalog = catalog or load_catalog()
    hooks = catalog['hooks']
    lines = [
        '/* SPDX-License-Identifier: GPL-2.0-only */',
        '/* Generated by scripts/generate-fault-errno-header.py. */',
        '#ifndef EXT4_FAULT_ERRNO_CATALOG_H',
        '#define EXT4_FAULT_ERRNO_CATALOG_H',
        '',
        '#include <linux/errno.h>',
        '#include <linux/string.h>',
        '',
        'static inline int ext4_fault_errno_from_name(const char *name)',
        '{',
    ]
    errno_names = {}
    for definition in hooks.values():
        for item in definition['native_errnos']:
            errno_names.setdefault(item['name'], item['name'])
            for alias in item.get('aliases', []):
                errno_names.setdefault(alias, item['name'])
    for name in errno_names:
        canonical = errno_names[name]
        lines.append(f'\tif (!strcmp(name, "{name}") || !strcmp(name, "-{name}"))')
        lines.append(f'\t\treturn -{canonical};')
    lines += ['\treturn 0;', '}', '',
        'static inline unsigned int',
        'ext4_fault_errno_count(enum ext4_fault_operation operation)',
        '{',
        '\tswitch (operation) {',
    ]
    for hook, definition in hooks.items():
        lines.append(f"\tcase {definition['kernel_operation']}:")
        lines.append(f"\t\treturn {len(definition['native_errnos'])};")
    lines += ['\tdefault:', '\t\treturn 1;', '\t}', '}', '',
              'static inline int ext4_fault_errno_at(',
              '\tenum ext4_fault_operation operation, unsigned int index)', '{',
              '\tswitch (operation) {']
    for hook, definition in hooks.items():
        lines.append(f"\tcase {definition['kernel_operation']}:")
        lines.append('\t\tswitch (index) {')
        for index, item in enumerate(definition['native_errnos']):
            lines.append(f"\t\tcase {index}: return -{item['name']};")
        lines += ['\t\tdefault: return 0;', '\t\t}']
    default_name = catalog['default_errno']
    if default_name == 'native':
        default_errno_lines = ['\tdefault:', '\t\treturn 0;', '\t}', '}']
        default_supported_lines = []
    else:
        default_errno_lines = ['\tdefault:',
                               f'\t\treturn index == 0 ? -{default_name} : 0;',
                               '\t}', '}']
        default_supported_lines = [f'\tif (error == -{default_name})',
                                   '\t\treturn true;']
    lines += default_errno_lines + ['',
              'static inline bool ext4_fault_errno_supported(',
              '\tenum ext4_fault_operation operation, int error)', '{',
              '\tunsigned int i;',
              *default_supported_lines,
              '\tfor (i = 0; i < ext4_fault_errno_count(operation); i++)',
              '\t\tif (ext4_fault_errno_at(operation, i) == error)',
              '\t\t\treturn true;',
              '\treturn false;',
              '}', '',
              '#endif /* EXT4_FAULT_ERRNO_CATALOG_H */', '']
    return '\n'.join(lines)


def generated_header_matches() -> bool:
    path = PROJECT / 'dev/linux-6.12.1/fs/ext4/fault_errno_catalog.h'
    return path.is_file() and path.read_text() == generate_kernel_header()
