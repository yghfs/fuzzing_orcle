#!/usr/bin/env python3
"""Formula order, program reset, mutation combinations and skipped-call semantics."""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'scripts'))
from efi_feedback import trajectory, mutate, identity, plan_text, reconcile_session, result_class
from efi_program import runtime
from efi_source_audit import apply_layer, audit_tree
spec = importlib.util.spec_from_file_location('runner', PROJECT/'scripts/run-campaign.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
spec = importlib.util.spec_from_file_location('injection', PROJECT/'scripts/configure-injection.py')
injection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(injection)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.policies = [dict(site_id=i,supported=True,callee='kmalloc_noprof') for i in (1,2,3)]
        self.calls = [dict(verified=True,call_index=i,pid=10,enter_line=i*100,exit_line=i*100+99) for i in range(3)]

    def line(self, number, body): return dict(line=number,body=body,pid=10,timestamp_ns=number)

    def error(self, line, order, call, site, eligible, injected=0):
        prefix=f'run=1 order={order} call={call} site_id={site} occurrence=1 context=42 eligible={eligible} allowed=1 injected={injected}'
        return [self.line(line,f'efi_site: {prefix} phase=enter result=0 pointer=0'),
                self.line(line+1,f'efi_site: {prefix} phase=return result={-12 if injected else 0} pointer=0')]

    def test_formula_order_cross_syscall_and_next_program_reset(self):
        lines=self.error(10,1,0,1,0)
        lines.append(self.line(20,'efi_formula: run=1 order=2 call=0 dev=254,16 point=INODE.ALLOC.BITMAP formula=R-IDELTA'))
        lines+=self.error(30,3,0,2,1)+self.error(110,4,1,3,1)
        events, candidates, issues=trajectory(lines,self.calls,self.policies,1)
        self.assertEqual(issues,[])
        self.assertEqual({p['site_id'] for p in candidates},{2,3})
        events, candidates, issues=trajectory(self.error(10,1,0,1,0),self.calls,self.policies,1)
        self.assertEqual(candidates,[])
        self.assertEqual(issues,[])

    def test_error_before_formula_does_not_become_eligible_retroactively(self):
        lines=self.error(10,1,0,1,0)+[self.line(20,'efi_formula: run=1 order=2 call=0 dev=254,16 point=F formula=R-IDELTA')]
        _,candidates,issues=trajectory(lines,self.calls,self.policies,1)
        self.assertEqual(candidates,[])
        self.assertEqual(issues,[])

    def test_illegal_injection_and_observer_events_are_rejected(self):
        _,_,issues=trajectory(self.error(10,1,0,1,0,1),self.calls,self.policies,1)
        self.assertTrue(any('injected_before_formula' in s for s in issues))
        lines=[self.line(350,'efi_formula: run=1 order=1 call=0 dev=254,16 point=F formula=R-IDELTA')]
        _,candidates,issues=trajectory(lines,self.calls,self.policies,1)
        self.assertEqual(candidates,[])
        self.assertTrue(issues)

    def test_three_candidates_explore_all_eight_combinations(self):
        candidates=[dict(call=0,site_id=i,occurrence=1,context=42) for i in (1,2,3)]
        seen={()}; queue=[()]
        while queue: queue+=mutate(queue.pop(0),candidates,seen)
        self.assertEqual(len(seen),8)
        self.assertIn(tuple(sorted(identity(p) for p in candidates)),seen)
        self.assertEqual(plan_text([identity(candidates[0])]),'plan 0 1 1 42\n')

    def test_dynamic_point_extends_a_fault_plan(self):
        old=(0,1,1,42)
        children=mutate([old],[dict(call=1,site_id=2,occurrence=1,context=99)],{(old,)})
        self.assertEqual(children,[(old,(1,2,1,99))])

    def test_other_device_and_missing_counter_cannot_open_gate(self):
        import os
        device = os.makedev(254, 16)
        self.calls[0]['object_dev'] = device
        formula = self.line(10, 'efi_formula: run=1 order=1 call=0 dev=254,17 point=F formula=R-IDELTA')
        _, candidates, issues = trajectory([formula], self.calls, self.policies, 1, device)
        self.assertEqual(candidates, [])
        self.assertTrue(any('formula_on_other_device' in s for s in issues))
        formula['body'] = formula['body'].replace('254,17', '254,16')
        _, _, issues = trajectory([formula], self.calls, self.policies, 1, device)
        self.assertTrue(any('formula_without_counter_event' in s for s in issues))

    def test_session_counts_and_plan_outcomes_must_match_trace(self):
        lines = self.error(10, 1, 0, 1, 0)
        joint, _, _ = trajectory(lines, self.calls, self.policies, 1)
        point = identity(joint[0])
        session = dict(run=1, calls=3, formulas=0, sites=1, injected=0)
        outcome = dict(zip(('call', 'site_id', 'occurrence', 'context'), point), status='blocked')
        self.assertEqual(reconcile_session(session, [outcome], joint, [point], 1, 3), [])
        outcome['status'] = 'not_reached'
        self.assertIn('session_plan_outcome_disagreement', reconcile_session(session, [outcome], joint, [point], 1, 3))
        session['sites'] = 2
        self.assertIn('session_trace_count_disagreement:sites', reconcile_session(session, [outcome], joint, [point], 1, 3))

    def test_successful_allocator_addresses_do_not_create_novelty(self):
        policy = dict(callee='get_zeroed_page_noprof')
        self.assertEqual(result_class(dict(result=0xffff888010000000), policy),
                         result_class(dict(result=-131940590387200), policy))
        self.assertNotEqual(result_class(dict(result=0), policy), result_class(dict(result=1), policy))

    def test_combination_is_scheduled_before_exhausting_singles(self):
        from collections import deque
        queue = deque([((0, i, 1, 42),) for i in range(1, 10)])
        pair = ((0, 1, 1, 42), (0, 2, 1, 42))
        queue.append(pair)
        self.assertEqual(runner.next_plan(queue, 1), ((0, 1, 1, 42),))
        self.assertEqual(runner.next_plan(queue, 2), pair)


class ExpressionTests(unittest.TestCase):
    def compile_and_run(self, text):
        header=(PROJECT/'instrumentation/ext4_efi.h').as_posix()
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory);(p/'linux').mkdir()
            (p/'linux/ext4_error_site.h').write_text('''#include <stdbool.h>
#include <stdint.h>
typedef uint64_t u64;typedef uint32_t u32;
extern bool ext4_error_site_enabled;
u64 ext4_error_site_enter(unsigned int);
void ext4_error_site_return(unsigned int,u64,long,bool);
#define READ_ONCE(x) (x)
''')
            (p/'test.c').write_text(text.replace('EFI_HEADER',header))
            subprocess.run(['gcc-14','-std=gnu11','-O2','-Wall','-Wextra','-Werror','-I',str(p),str(p/'test.c'),'-o',str(p/'test')],check=True)
            subprocess.run([str(p/'test')],check=True)

    def test_skip_preserves_argument_evaluation_and_cleanup(self):
        text=r'''
#include <assert.h>
#define CONFIG_EXT4_ERROR_SITE_TRACE 1
#define EXT4_ERROR_SITE_TU 1
#include "EFI_HEADER"
bool ext4_error_site_enabled;
static bool inject;
static int argument_count,call_count,cleanups;
u64 ext4_error_site_enter(unsigned int id) { (void)id; return 1; }
void ext4_error_site_return(unsigned int id,u64 inv,long r,bool p) { (void)id;(void)inv;(void)r;(void)p; }
struct ext4_efi_decision ext4_efi_before(u32 id,bool allowed) {
 (void)id; return (struct ext4_efi_decision){.tracked=true,.inject=inject&&allowed};
}
void ext4_efi_after(u32 id,struct ext4_efi_decision *d,long r,bool p) { (void)id;(void)d;(void)r;(void)p; }
static int call(int value) { call_count++;return value; }
static int argument(void) { argument_count++;return 7; }
#define WRAPPED(allowed) ({ __auto_type a=argument(); EXT4_EFI_CALL(1,allowed,-12,cleanups++,call(a)); })
int main(void) {
 assert(WRAPPED(true)==7 && argument_count==1 && call_count==1 && !cleanups);
 inject=true;
 assert(WRAPPED(true)==-12 && argument_count==2 && call_count==1 && cleanups==1);
 assert(WRAPPED(false)==7 && argument_count==3 && call_count==2 && cleanups==1);
 int skipped=0&&WRAPPED(true);
 assert(!skipped && argument_count==3);
 int value=0;int *p=&value;
 int *q=EXT4_EFI_CALL(2,true,0,(void)0,p);
 assert(q==0);
 return 0;
}
'''
        self.compile_and_run(text)

    def test_usercopy_full_failure_preserves_buffer_contract(self):
        mapping=json.loads((PROJECT/'sites.json').read_text())
        wrappers={}
        for name in ('copy_from_user','copy_to_user'):
            site=next(s.copy() for s in mapping['sites'] if s['callee']==name)
            site['source_expression']=f'{name}(dst, src, size)'
            wrappers[name]=injection.wrapper([site],injection.policy(site))
        text=r'''
#include <assert.h>
#include <string.h>
#define CONFIG_EXT4_ERROR_SITE_TRACE 1
#define EXT4_ERROR_SITE_TU 1
#include "EFI_HEADER"
bool ext4_error_site_enabled;
static bool inject, size_ok=true;
static int checks, calls;
u64 ext4_error_site_enter(unsigned int id) { (void)id; return 1; }
void ext4_error_site_return(unsigned int id,u64 inv,long r,bool p) { (void)id;(void)inv;(void)r;(void)p; }
struct ext4_efi_decision ext4_efi_before(u32 id,bool allowed) {
 (void)id; return (struct ext4_efi_decision){.tracked=true,.inject=inject&&allowed};
}
void ext4_efi_after(u32 id,struct ext4_efi_decision *d,long r,bool p) { (void)id;(void)d;(void)r;(void)p; }
static bool check_copy_size(const void *dst,unsigned long n,bool source) {
 (void)dst;(void)n;(void)source;checks++;return size_ok;
}
static unsigned long copy_from_user(void *dst,const void *src,unsigned long n) {
 calls++;if (!check_copy_size(dst,n,false)) return n;memcpy(dst,src,n);return 0;
}
static unsigned long copy_to_user(void *dst,const void *src,unsigned long n) {
 calls++;if (!check_copy_size(src,n,true)) return n;memcpy(dst,src,n);return 0;
}
int main(void) {
 unsigned char dst[8],src[8];unsigned long size=sizeof(dst);
 memset(src,0x5a,sizeof(src));memset(dst,0xa5,sizeof(dst));
 assert(FROM==0 && calls==1 && checks==1 && memcmp(dst,src,size)==0);
 inject=true;
 assert(FROM==size && calls==1 && checks==2);
 for (unsigned int i=0;i<size;i++) assert(dst[i]==0);
 memset(dst,0xa5,sizeof(dst));size_ok=false;
 assert(FROM==size && calls==1 && checks==3);
 for (unsigned int i=0;i<size;i++) assert(dst[i]==0xa5);
 size_ok=true;
 assert(TO==size && calls==1 && checks==3);
 for (unsigned int i=0;i<size;i++) assert(dst[i]==0xa5);
 inject=false;
 assert(TO==0 && calls==2 && checks==4 && memcmp(dst,src,size)==0);
 inject=true;size=0;
 assert(FROM==0 && calls==3 && checks==5);
 return 0;
}
'''.replace('FROM',wrappers['copy_from_user']).replace('TO',wrappers['copy_to_user'])
        self.compile_and_run(text)

    def test_runtime_arms_immediately_before_native_call(self):
        text=runtime()
        self.assertIn('efi_call_begin(index, nr, object_dev);\n  errno = 0;\n  long ret = syscall(',text)
        self.assertIn('fd == efi_fd',text)
        self.assertIn('int seed_fd = efi_open_stable_file();',text)
        self.assertIn('stable data descriptors lost',text)
        self.assertNotIn('open("./file0/file0"',text)

    def test_seed_holder_reference_survives_rename_and_unlink(self):
        source=PROJECT/'tests/seed-holder.c'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);seed=root/'seed';ready=root/'ready.json'
            seed.write_bytes(b'held inode')
            binary=root/'seed-holder'
            subprocess.run(['gcc-14','-O2','-Wall','-Wextra','-Werror','-o',str(binary),str(source)],check=True)
            process=subprocess.Popen([str(binary),str(seed),str(ready)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline and not ready.exists() and process.poll() is None:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(),'holder did not start' if process.poll() is None else 'holder exited before publishing its inode')
                metadata=json.loads(ready.read_text())
                original=os.stat(seed)
                self.assertEqual((metadata['dev'],metadata['ino']),(original.st_dev,original.st_ino))
                renamed=root/'renamed'
                seed.rename(renamed)
                renamed.unlink()
                reference=metadata['reference']
                self.assertEqual((os.stat(reference).st_dev,os.stat(reference).st_ino),
                                 (original.st_dev,original.st_ino))
                with open(reference,'r+b') as held:
                    self.assertEqual(held.read(),b'held inode')
                    held.seek(0);held.write(b'changed!!!')
                    held.flush()
                self.assertEqual(os.stat(reference).st_nlink,0)
                self.assertIsNone(process.poll())
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=5)
            self.assertFalse(ready.exists())

    def test_every_realistic_record_has_an_explicit_policy(self):
        mapping=json.loads((PROJECT/'sites.json').read_text())
        policies=json.loads((PROJECT/'injection.json').read_text())
        self.assertEqual({s['site_id'] for s in mapping['sites']},{s['site_id'] for s in policies['policies']})
        for p in policies['policies']:
            # buffer_uptodate is a predicate over object state, so it is injected
            # by clearing the very flag the check reads -- exactly what upstream's
            # ext4_simulate_fail_bh() does just before the same check. The other
            # stateful predicates have no equivalent single-flag mutation (a
            # cleared BH_Uptodate does not make buffer_write_io_error() true).
            if p['callee']=='buffer_uptodate':
                self.assertTrue(p['supported'])
                self.assertEqual(p['failure'],'0')
                self.assertEqual(p['cleanup'],'clear_buffer_uptodate(__efi_a0)')
            elif p['callee'] in ('buffer_write_io_error','bh2jh','buffer_verified'):
                self.assertFalse(p['supported'])
            if p['callee']=='copy_from_user':
                self.assertEqual(p['failure'],'__efi_a2')
                self.assertIn('check_copy_size(__efi_a0, __efi_a2, false)',p['cleanup'])
                self.assertIn('memset(__efi_a0, 0, __efi_a2)',p['cleanup'])
            if p['callee']=='d_make_root':self.assertEqual(p['cleanup'],'iput(__efi_a0)')


def function_tokens(text, name):
    tokens = [t[0] for t in injection.sites.tokens(text)]
    for start, token in enumerate(tokens):
        if token != name or tokens[start+1] != '(':
            continue
        depth = 1
        end = start+2
        while depth:
            depth += (tokens[end] == '(') - (tokens[end] == ')')
            end += 1
        if tokens[end] != '{':
            continue
        depth = 1
        end += 1
        while depth:
            depth += (tokens[end] == '{') - (tokens[end] == '}')
            end += 1
        return tokens[start:end]
    raise AssertionError(f'missing function definition: {name}')


class SourceSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mapping = json.loads((PROJECT/'sites.json').read_text())
        patch, manifest = injection.generate()
        cls.sources = {}
        cls.upstream = {}
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            injection.sites.load_baseline(tree, mapping['sites'])
            injection.sites.patch_run(tree, injection.sites.PATCH)
            layer = tree/'injection.patch'
            layer.write_bytes(patch)
            injection.sites.patch_run(tree, layer)
            for name in ('ext4.h', 'inline.c', 'orphan.c', 'namei.c', 'xattr.c',
                         'ext4_jbd2.c', 'extents.c'):
                path = 'fs/ext4/'+name
                cls.sources[name] = (tree/path).read_text()
                cls.upstream[name] = (injection.sites.PRISTINE/path).read_text()
        # Project the actual generated wrappers back to their original calls.
        # This checks the final three-layer source, not textual patch fragments.
        seen = set()
        for site in mapping['sites']:
            key = (site['source'], site['runtime_selector'])
            name = Path(site['source']).name
            if name not in cls.sources or key in seen:
                continue
            seen.add(key)
            wrapped = injection.wrapper([site], injection.policy(site))
            if cls.sources[name].count(wrapped) != 1:
                raise AssertionError(f'generated wrapper missing/duplicated: {site["site_id"]}')
            cls.sources[name] = cls.sources[name].replace(wrapped, site['source_expression'])

    def test_original_xattr_validation_and_dirty_metadata_function(self):
        for name, function in (('xattr.c', 'check_xattrs'),
                               ('ext4_jbd2.c', '__ext4_handle_dirty_metadata'),
                               ('namei.c', 'ext4_rename2')):
            with self.subTest(function=function):
                self.assertEqual(function_tokens(self.sources[name], function),
                                 function_tokens(self.upstream[name], function))
        self.assertNotIn('ext4_handle_dirty_metadata_error(', self.sources['ext4_jbd2.c'])
        self.assertNotIn('!error && !ext4_handle_valid(handle)', self.sources['xattr.c'])

    def test_upstream_failure_and_mount_contracts_are_preserved(self):
        self.assertIn('#define AGGRESSIVE_CHECK__\n', self.sources['ext4.h'])
        self.assertIn('unsigned int len)', self.sources['inline.c'].split(
            'static int ext4_prepare_inline_data(', 1)[1].split('{', 1)[0])
        orphan = function_tokens(self.sources['orphan.c'], 'ext4_init_orphan_info')
        self.assertNotIn('orphan_file_size', orphan)
        self.assertNotIn('orphan_file_blocks', orphan)
        self.assertNotIn('EXT4_MAX_ORPHAN_FILE_BLOCKS', self.sources['orphan.c'])
        self.assertIn('oi->of_blocks = inode->i_size >> sb->s_blocksize_bits;',
                      self.sources['orphan.c'])
        grow = function_tokens(self.sources['extents.c'], 'ext4_ext_grow_indepth')
        fail = grow[grow.index('sb_getblk_gfp'):grow.index('lock_buffer')]
        self.assertNotIn('ext4_free_blocks', fail)
        self.assertIn('ext4_checker_extent_blocks_error', fail)
        self.assertIn('ENOMEM', fail)

    def test_unlink_keeps_original_operation_order(self):
        tokens = function_tokens(self.sources['namei.c'], '__ext4_unlink')
        original = function_tokens(self.upstream['namei.c'], '__ext4_unlink')
        self.assertNotIn('ext4_reserve_inode_write', tokens)
        self.assertNotIn('dir_iloc', tokens)
        self.assertNotIn('inode_iloc', tokens)
        actions = {'ext4_delete_entry', 'inode_set_mtime_to_ts', 'ext4_mark_inode_dirty',
                   'drop_nlink', 'ext4_orphan_add', 'inode_set_ctime_current',
                   'ext4_journal_stop', 'brelse'}
        self.assertEqual([t for t in tokens if t in actions],
                         [t for t in original if t in actions])

    def test_namespace_instrumentation_performs_each_original_write_once(self):
        tokens = function_tokens(self.sources['namei.c'], 'ext4_setent')
        assignment = [t[0] for t in injection.sites.tokens('ent->de->inode = cpu_to_le32')]
        self.assertEqual(sum(tokens[i:i+len(assignment)] == assignment
                             for i in range(len(tokens))), 1)
        mkdir = function_tokens(self.sources['namei.c'], 'ext4_mkdir')
        self.assertEqual(mkdir.count('clear_nlink'), 1)
        self.assertNotIn('ext4_ct_nlink', mkdir)
        delete = self.sources['namei.c'].split('deleted_ino, 2, 0);', 1)[1]
        self.assertTrue(delete.lstrip().startswith('return 0;\nout:'))


class SourceAuditTests(unittest.TestCase):
    def fixture(self, root):
        official = {}
        for name in ('a.c', 'b.c'):
            data = f'original {name}\n'.encode()
            (root/name).write_bytes(data)
            official[name] = dict(path=name, type='file', mode='0o664',
                                  sha256=injection.sites.sha(data))
        (root/'link').symlink_to('a.c')
        official['link'] = dict(path='link', type='symlink', mode='0o777', target='a.c')
        expected = {path: e.copy() for path, e in official.items()}
        files = []
        for name, data in (('a.c', b'instrumented a.c\n'), ('trace.c', b'observer\n')):
            (root/name).write_bytes(data)
            files.append(dict(path=name, before=official.get(name, {}).get('sha256'),
                              after=injection.sites.sha(data)))
        apply_layer(expected, files, 'before', 'after')
        return official, expected

    def test_accepts_only_exact_upstream_plus_bound_patch_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            official, expected = self.fixture(root)
            report = audit_tree(root, official, expected)
            self.assertEqual(report['status'], 'pass')
            self.assertEqual((report['unchanged_regular_files'], report['changed_upstream_files'],
                              report['added_files'], report['symlinks']), (1, 1, 1, 1))

    def test_rejects_unlisted_edits_missing_files_extras_links_and_executable_changes(self):
        for action in ('outside_patch', 'inside_patch', 'missing', 'extra', 'link', 'executable'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                official, expected = self.fixture(root)
                if action == 'outside_patch': (root/'b.c').write_text('unreviewed edit')
                elif action == 'inside_patch': (root/'a.c').write_text('unreviewed edit')
                elif action == 'missing': (root/'b.c').unlink()
                elif action == 'extra': (root/'other.c').write_text('unlisted file')
                elif action == 'link':
                    (root/'link').unlink()
                    (root/'link').symlink_to('b.c')
                elif action == 'executable': (root/'b.c').chmod(0o755)
                with self.assertRaisesRegex(ValueError, 'unreviewed development source'):
                    audit_tree(root, official, expected)

    def test_patch_layers_must_match_the_previous_file_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            _, expected = self.fixture(Path(directory))
            with self.assertRaisesRegex(ValueError, 'patch source chain mismatch'):
                apply_layer(expected, [dict(path='a.c', before='incorrect', after='replacement')],
                            'before', 'after')


if __name__=='__main__': unittest.main()
