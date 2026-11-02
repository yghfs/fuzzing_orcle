"""Export and instrument upstream syzkaller programs, preserving replay evidence."""
from __future__ import annotations
import json
import pathlib
import re
from campaign import atomic_json, digest

PROJECT = pathlib.Path(__file__).resolve().parents[1]


def execution_issues(programs, calls, segment, batches, workers, fault_stop=None):
    """Require every expected generated call exactly once, including failed syscalls."""
    issues = []
    expected_programs = {f'b{batch}-w{worker}' for batch in range(1, batches+1) for worker in range(workers)}
    if any(str(c['id']).startswith(segment + '-b') and c.get('program') not in expected_programs for c in calls):
        issues.append('unexpected_syzkaller_program_executed')
    for batch in range(1, batches+1):
        for worker in range(workers):
            ident = f'b{batch}-w{worker}'
            meta = json.loads((programs / ident / 'program.json').read_text())
            selected = [c for c in calls if c.get('program') == ident and
                        str(c['id']).startswith(f'{segment}-b{batch}-')]
            observed = {}
            for call in selected:
                try:
                    index = int(call.get('call_index'))
                except (ValueError, TypeError):
                    index = None
                observed.setdefault(index, []).append(call)
            allowed_count = len(meta['calls'])
            if fault_stop and fault_stop.get('program') == ident:
                try:
                    stop_index = int(fault_stop['call_index'])
                except (KeyError, TypeError, ValueError):
                    stop_index = -1
                if stop_index < 0 or stop_index >= len(meta['calls']):
                    issues.append(ident + ':invalid_fault_stop_marker')
                else:
                    allowed_count = stop_index + 1
                    if any(index is None or index > stop_index for index in observed):
                        issues.append(ident + ':calls_after_fault_stop')
            expected_indices = set(range(allowed_count))
            if set(observed) != expected_indices:
                issues.append(ident + ':missing_or_extra_call_indices')
            for expected in meta['calls'][:allowed_count]:
                actual = observed.get(expected['index'], [])
                if len(actual) != 1 or not actual[0]['verified'] or actual[0]['nr'] != expected['nr'] or actual[0]['name'] != expected['name']:
                    issues.append(f'{ident}:call_{expected["index"]}_not_verified')
    if not batches:
        issues.append('no_syzkaller_batch_executed')
    return issues


def split_args(body):
    result, start, level = [], 0, 0
    for i, char in enumerate(body):
        if char == '(':
            level += 1
        elif char == ')':
            level -= 1
        elif char == ',' and level == 0:
            result.append(body[start:i].strip()); start = i+1
    result.append(body[start:].strip())
    return result


def instrument(source, metadata, runtime):
    calls = metadata['calls']
    native = {c['native_name'] for c in calls}
    pattern = re.compile(r'\bsyscall\(\s*__NR_(\w+)\s*(?=[,)])')
    cursor, pieces, index = 0, [], 0
    for match in pattern.finditer(source):
        if match[1] not in native:
            continue  # Exporter setup calls (mmap, signal setup) have no program index.
        end, depth = match.end(), 1
        while depth and end < len(source):
            char = source[end]
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            end += 1
        if depth:
            raise ValueError('unbalanced generated syscall')
        args = split_args(source[match.start()+len('syscall('):end-1])
        if index >= len(calls) or match[1] != calls[index]['native_name'] or len(args) > 7:
            raise ValueError('C export disagrees with syzkaller call metadata')
        call = calls[index]
        converted = [args[0]] + ['(long)(' + a + ')' for a in args[1:]] + ['0']*(7-len(args))
        replacement = f'ct_call({index}, "{call["name"]}", ' + ', '.join(converted) + ')'
        pieces += [source[cursor:match.start()], replacement]
        cursor, index = end, index+1
    pieces.append(source[cursor:])
    if index != len(calls) or not index:
        raise ValueError('missing generated syscall calls')
    source = ''.join(pieces)
    main = re.search(r'int main\(void\)\s*\{', source)
    if not main:
        raise ValueError('expected sequential C export main(void)')
    # Runtime appears after exporter headers/helpers and before generated calls.
    # Put the runtime before generated main/execute_one, never inside a function.
    functions = list(re.finditer(r'(?m)^(?:static )?(?:void|int) (?:execute_one|main)\([^\n]*\)\s*\{', source))
    insertion = min(m.start() for m in functions)
    source = source[:insertion] + runtime + '\n' + source[insertion:]
    source, n = re.subn(r'(int main\(void\)\s*\{)', r'\1\n  ct_init();', source, count=1)
    if n != 1:
        raise ValueError('runtime initialization missing')
    return source


def generate_program(command, generator, folder, seed, calls, mutate=None, runtime_path=None, input_mode="ordinary", decision=None, resource_template="auto"):
    argv = [generator, '-out', folder, '-seed', seed, '-calls', calls, '-input-mode', input_mode,
            '-resource-template', resource_template]
    if mutate:
        argv += ['-mutate', mutate]
    command(argv, timeout=60, log='generation.txt', stage='generation')
    meta = json.loads((folder / 'program.json').read_text())
    runtime = pathlib.Path(runtime_path or PROJECT / 'tests/syz-runtime.h').read_text()
    (folder / 'workload.c').write_text(instrument((folder / 'original.c').read_text(), meta, runtime))
    command(['gcc', '-O2', '-static', '-pthread', '-o', folder / 'workload', folder / 'workload.c'],
            timeout=60, log='generation.txt', stage='compilation')
    meta['schedule_decision'] = decision
    meta['mutation_input_sha256'] = digest(mutate) if mutate else None
    if mutate:
        try:
            mutation_input = str(mutate.relative_to(folder.parent))
        except ValueError:
            # Cross-segment corpus parents live under the campaign root.
            mutation_input = str(mutate)
    else:
        mutation_input = None
    meta['mutation_input'] = mutation_input
    meta['sha256'] = {name: digest(folder / name) for name in
                      ('program.syz', 'original.c', 'workload.c', 'workload')}
    atomic_json(folder / 'program.json', meta)
    return meta
