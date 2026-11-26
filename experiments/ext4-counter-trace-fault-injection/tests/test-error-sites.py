#!/usr/bin/env python3
"""Exercise probe expression semantics, CSV identity and trace reconstruction."""
import csv
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parents[1]
spec = importlib.util.spec_from_file_location('site_report', PROJECT/'scripts/report-sites.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class ProbeTests(unittest.TestCase):
    def test_realistic_records_and_header_scopes(self):
        with (ROOT/'realistic_review.csv').open(newline='') as stream:
            reviewed = {int(r['site_id']): r for r in csv.DictReader(stream) if r['realistic'] == '1'}
        manifest = json.loads((PROJECT/'sites.json').read_text())
        mapped = {s['site_id']: s for s in manifest['sites']}
        self.assertEqual(set(reviewed), set(mapped))
        for site_id, site in mapped.items():
            row = reviewed[site_id]
            for key in ('source', 'translation_unit', 'caller', 'callee'):
                self.assertEqual(site[key], row[key])
            if site['source'] != site['translation_unit']:
                self.assertIn(f"EXT4_ERROR_SITE_TU == {site['translation_unit_id']} ? {site_id}", site['runtime_selector'])

    def test_expression_type_side_effects_nesting_and_short_circuit(self):
        header = (PROJECT/'instrumentation/ext4_error_site.h').as_posix()
        program = r'''
#include <assert.h>
#define CONFIG_EXT4_ERROR_SITE_TRACE 1
#define EXT4_ERROR_SITE_TU 1
#include "PROBE_HEADER"
bool ext4_error_site_enabled = true;
static int begun, ended, evaluated;
static unsigned int order[8];
u64 ext4_error_site_enter(unsigned int id) { order[begun] = id; return ++begun; }
void ext4_error_site_return(unsigned int id, u64 seq, long result, bool ptr)
{ (void)id; (void)seq; (void)result; (void)ptr; ended++; }
static int effect(void) { return ++evaluated; }
int main(void) {
    int value = EXT4_ES_CALL(11, effect());
    assert(value == 1 && evaluated == 1 && begun == 1 && ended == 1);
    int short_circuit = 0 && EXT4_ES_CALL(12, effect());
    assert(!short_circuit && evaluated == 1 && begun == 1);
    value = EXT4_ES_CALL(13, EXT4_ES_CALL(14, effect()));
    assert(value == 2 && evaluated == 2 && begun == 3 && ended == 3);
    assert(order[1] == 13 && order[2] == 14);
    int *pointer = &value;
    assert(EXT4_ES_CALL(15, pointer) == pointer);
    assert(__builtin_types_compatible_p(__typeof__(EXT4_ES_CALL(15, pointer)), int *));
    ext4_error_site_enabled = false;
    assert(EXT4_ES_CALL(16, effect()) == 3);
    assert(begun == 4 && ended == 4);
    ext4_error_site_enabled = true;
    assert(EXT4_ES_CALL(0, effect()) == 4);
    assert(begun == 4 && ended == 4);
    return 0;
}
'''.replace('PROBE_HEADER', header)
        self.compile_run(program)

    def test_header_in_unrelated_translation_unit_has_no_runtime_dependency(self):
        header = (PROJECT/'instrumentation/ext4_error_site.h').as_posix()
        program = '#define CONFIG_EXT4_ERROR_SITE_TRACE 1\n#include "' + header + '"\n'
        program += 'int main(void) { int n = 0; return EXT4_ES_CALL(123, ++n) != 1; }\n'
        self.compile_run(program)

    def compile_run(self, program):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path/'linux').mkdir()
            (path/'linux/types.h').write_text('#include <stdbool.h>\n#include <stdint.h>\ntypedef uint64_t u64;\n')
            (path/'linux/compiler.h').write_text('#define READ_ONCE(x) (*(volatile __typeof__(x) *)&(x))\n')
            (path/'probe.c').write_text(program)
            subprocess.run(['gcc-14', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror',
                            '-I', str(path), str(path/'probe.c'), '-o', str(path/'probe')], check=True)
            subprocess.run([str(path/'probe')], check=True)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {'sites': [dict(site_id=i, source='fs/ext4/test.c', line=1,
                                      translation_unit='fs/ext4/test.c', caller='test', callee='call')
                                   for i in (1, 2)]}

    def event(self, site, invocation, phase, result=0, kind='integer'):
        if phase == 'enter':
            kind = 'pending'
        return f'test-10 [000] ... 1.0: error_site: schema=1 site_id={site} invocation={invocation} phase={phase} result={result} kind={kind}\n'

    def test_interleaved_invocations_and_return_values(self):
        trace = (self.event(1, 10, 'enter') + self.event(2, 11, 'enter') +
                 self.event(1, 10, 'return', -12) + self.event(2, 11, 'return', 0, 'pointer'))
        summary, events = report.analyze(trace, self.manifest)
        self.assertTrue(summary['trace_complete_pairs'])
        self.assertEqual(summary['reached_sites'], 2)
        self.assertEqual(events[2]['result'], -12)
        self.assertNotIn('failure', events[3])

    def test_missing_events_are_incomplete(self):
        summary, _ = report.analyze(self.event(1, 10, 'enter') + self.event(2, 11, 'return'), self.manifest)
        self.assertFalse(summary['trace_complete_pairs'])
        self.assertEqual(len(summary['issues']), 2)

    def test_unknown_ids_and_malformed_events_are_rejected(self):
        summary, _ = report.analyze(self.event(3, 12, 'enter') + 'error_site: bad\n', self.manifest)
        self.assertFalse(summary['trace_complete_pairs'])
        self.assertEqual(len(summary['issues']), 2)

    def test_repeated_invocation_is_rejected(self):
        summary, _ = report.analyze(self.event(1, 10, 'enter') * 2, self.manifest)
        self.assertTrue(any(s.startswith('duplicate_invocation:') for s in summary['issues']))


if __name__ == '__main__':
    unittest.main()
