#!/usr/bin/env python3
"""Generate explicit failure policies and a layer over the reviewed site probes."""
from __future__ import annotations
import argparse
import difflib
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from efi_source_audit import verify_source

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sites', PROJECT/'scripts/instrument-sites.py')
sites = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sites)
PATCH = PROJECT/'patches/0002-formula-gated-injection.patch'
MANIFEST = PROJECT/'injection.json'
ASSETS = {'ext4_efi.h': 'include/linux/ext4_efi.h',
          'ext4_efi_event.h': 'include/trace/events/ext4_efi.h', 'efi.c': 'fs/ext4/efi.c'}

# A predicate over buffer state can be injected faithfully by mutating the
# flag instead of faking the answer. The cleanup slot performs the same
# clear_buffer_uptodate() that upstream's ext4_simulate_fail_bh() applies
# immediately before the very same check, so the object and the return value
# stay consistent and every later observer sees the same failure.
STATE_MUTATORS = {'buffer_uptodate': 'clear_buffer_uptodate'}

# These return codes have a matching failure contract. Stateful predicates and
# calls which partially initialize/consume complex resources remain observable
# and are explicitly rejected, rather than fabricating an impossible errno.
STATEFUL = {'buffer_uptodate', 'buffer_write_io_error', 'buffer_verified', 'bh2jh',
            'is_handle_aborted', 'insert_inode_locked', 'kobject_init_and_add', 'sb_getblk',
            'jbd2_journal_start_reserved', 'jbd2_journal_get_write_access',
            'jbd2_journal_get_create_access', 'jbd2_journal_dirty_metadata',
            'jbd2_journal_revoke', 'jbd2_journal_wipe', 'jbd2_journal_load',
            'jbd2_journal_errno', 'jbd2_journal_flush'}
NULLS = {'kmalloc_noprof', 'kzalloc_noprof', 'kmem_cache_alloc_noprof', 'kmalloc_array_noprof',
         'posix_acl_alloc', 'sb_getblk_gfp', 'shrinker_alloc', 'krealloc_noprof', 'd_alloc',
         'new_inode', 'iget_locked', '__kvmalloc_node_noprof', 'pcpu_alloc_noprof',
         '__kmem_cache_create', 'mempool_create_node_noprof', 'get_zeroed_page_noprof',
         'kmemdup_nul', 'd_make_root', 'kstrndup', 'kvmalloc_array_node_noprof',
         'kmem_cache_alloc_lru_noprof', '__bread', 'kmem_cache_create_usercopy',
         'kobject_create_and_add', 'kmemdup_noprof'}
ENOMEM = {'posix_acl_create', '__percpu_counter_init_many', '__percpu_init_rwsem'}
EIO = {'file_modified', 'file_write_and_wait_range', 'generic_buffers_fsync_noflush',
       'sync_mapping_buffers', 'sync_inode_metadata', 'sb_issue_zeroout', 'filemap_write_and_wait',
       'bdev_freeze', 'jbd2_fc_begin_commit', 'jbd2_fc_wait_bufs', 'jbd2_submit_inode_data',
       'jbd2_wait_inode_data', 'dquot_commit', 'dquot_acquire', 'dquot_commit_info'}
EINVAL = {'fiemap_prep', 'setattr_prepare', 'fs_parse', 'fs_lookup_param',
          'generic_check_addressable', 'kstrtoul', 'kstrtouint', 'dquot_quota_on'}
QUOTA = {'dquot_initialize', 'dquot_alloc_inode', 'dquot_transfer', 'dquot_reserve_block',
         'dquot_alloc_space', 'dquot_alloc_block', 'dquot_alloc_space_nodirty'}
CRYPT = {'fscrypt_prepare_readdir', 'fscrypt_file_open', 'fscrypt_prepare_new_inode',
         'fscrypt_prepare_setattr', 'fscrypt_prepare_link', 'fscrypt_prepare_symlink',
         'fscrypt_prepare_rename'}


def arguments(expression):
    ts = sites.tokens(expression)
    begin, end, depth = 2, len(ts)-1, 0
    args, start = [], ts[1][2]
    for token, lo, hi in ts[2:end]:
        if token in ('(', '[', '{'): depth += 1
        elif token in (')', ']', '}'): depth -= 1
        elif token == ',' and not depth:
            args.append(expression[start:lo].strip()); start = hi
    args.append(expression[start:ts[end][1]].strip())
    return args


def policy(site):
    name, spelling = site['callee'], site['source_spelling']
    args = arguments(site['source_expression'])
    a = [f'__efi_a{i}' for i in range(len(args))]
    value, guard, cleanup = None, 'true', '(void)0'
    reason = 'skip_call_with_contract_failure'
    if name in STATE_MUTATORS:
        # A buffer-state predicate is not an error code, so substituting only
        # the return value would contradict the object every other observer
        # sees: one call would report "invalid" while the same bh still carries
        # BH_Uptodate elsewhere in the syscall. Mutate the flag in the cleanup
        # slot, which runs before the substituted result is assigned, so all
        # later buffer_uptodate() calls on that bh agree. This is exactly what
        # upstream's own ext4_simulate_fail_bh() does at the read site.
        value = '0'
        cleanup = f'{STATE_MUTATORS[name]}({a[0]})'
        reason = 'object_state_mutation_matches_upstream_ext4_simulate_fail_bh'
    elif name in STATEFUL:
        value, guard, reason = '0', 'false', 'requires_stateful_failure_model'
    elif name in NULLS:
        value = '0'
    elif name == 'get_inode_acl': value = '-ENOMEM'
    elif name in ENOMEM: value = '-ENOMEM'
    elif name in EIO: value = '-EIO'
    elif name in EINVAL: value = '-EINVAL'
    elif name in QUOTA:
        value = '-EDQUOT' if name != 'dquot_initialize' else '-ENOMEM'
        guard = f'sb_any_quota_loaded({a[1] if name == "dquot_transfer" else a[0]}->i_sb)'
        reason = 'quota_must_be_loaded'
    elif name in CRYPT:
        value = '-EOPNOTSUPP'
        guard = f'IS_ENCRYPTED({a[0]})'
        if name == 'fscrypt_prepare_rename': guard = f'(IS_ENCRYPTED({a[0]}) || IS_ENCRYPTED({a[2]}))'
        reason = 'encrypted_inode_required'
    elif name in {'fsverity_file_open', 'fsverity_prepare_setattr'}:
        value, guard, reason = '-EOPNOTSUPP', f'IS_VERITY({a[0]})', 'verity_inode_required'
        if name == 'fsverity_prepare_setattr': guard = f'IS_VERITY(d_inode({a[0]}))'
    elif name in {'inode_trylock_shared', 'down_read_trylock', 'inode_trylock'}: value = '0'
    elif name in {'copy_from_user', 'copy_to_user'}:
        value, guard = a[2], f'({a[2]} > 0)'
        if name == 'copy_from_user':
            # Match full failure after the public API's destination-size check.
            # A rejected size returns n without writing; a valid buffer is zeroed.
            cleanup = f'if (check_copy_size({a[0]}, {a[2]}, false)) memset({a[0]}, 0, {a[2]})'
    elif name == 'inode_newsize_ok': value = '-EFBIG'
    elif name == 'generic_write_checks': value = '-EINVAL'
    elif name in {'iomap_seek_hole', 'iomap_seek_data'}: value = '-EIO'
    elif name == 'mnt_want_write_file': value = '-EROFS'
    elif name == 'fiemap_fill_next_extent': value = '-EFAULT'
    elif name == 'jbd2_fc_get_buf': value = '-ENOSPC'
    else: raise ValueError(f'no reviewed policy for {name}')
    if name == 'fscrypt_prepare_setattr': guard = f'IS_ENCRYPTED(d_inode({a[0]}))'
    if name == 'fscrypt_prepare_link': guard = f'IS_ENCRYPTED({a[1]})'
    if name == 'd_make_root': cleanup = f'iput({a[0]})'
    # A GFP_NOFAIL allocation must execute normally. The arguments are captured
    # first, so even complex GFP expressions and side effects execute once.
    gfp = {'kmalloc': 1, 'kmalloc_noprof': 1, 'kzalloc': 1, 'kcalloc': 2,
           'kmalloc_array': 2, 'kmem_cache_alloc': 1, 'kmem_cache_zalloc': 1,
           'jbd2_alloc_inode': 0, 'posix_acl_alloc': 1, 'sb_getblk_gfp': 2,
           'krealloc': 2, 'kvmalloc': 1, 'kvzalloc': 1, 'kvmalloc_node_noprof': 1,
           'get_zeroed_page': 0, 'kmemdup_nul': 2, 'kstrndup': 2,
           'kvmalloc_array': 2, 'kmemdup': 2}.get(spelling)
    if gfp is not None and guard != 'false':
        guard = f'({guard} && !({a[gfp]} & __GFP_NOFAIL))'
    if spelling == 'kmem_cache_create': guard = f'!({a[3]} & SLAB_PANIC)'
    if spelling == 'kmem_cache_create_usercopy': guard = f'!({a[3]} & SLAB_PANIC)'
    return dict(site_id=site['site_id'], callee=name,
                supported=name not in STATEFUL or name in STATE_MUTATORS,
                failure=value, guard=guard, cleanup=cleanup, reason=reason)


def wrapper(group, p):
    site = group[0]
    expr, selector = site['source_expression'], site['runtime_selector']
    args = arguments(expr)
    captured = [f'__efi_a{i}' for i in range(len(args))]
    declarations = []
    for index, arg in enumerate(args):
        if site['source_spelling'] == 'alloc_percpu' and index == 0:
            captured[index] = arg  # alloc_percpu takes a C type, not a value.
        else:
            declarations.append(f'__auto_type {captured[index]} = ({arg});')
    call = site['source_spelling'] + '(' + ', '.join(captured) + ')'
    return '({ ' + ' '.join(declarations) + f" EXT4_EFI_CALL({selector}, {p['guard']}, {p['failure']}, {p['cleanup']}, {call}); }})"


def restore_upstream_semantics(changed, baseline, rows):
    """Undo non-instrumentation edits carried by the checker base patch.

    The experiment starts from the official Linux 6.12.1 source. Counter
    checker hooks may add observations, but must not silently inherit fixes or
    unrelated behavior changes from the checker development tree. Keep each
    restoration narrow and fail closed if its reviewed source shape drifts.
    """
    def replace_once(path, old, new, label):
        text = changed.get(path, (baseline/path).read_text())
        if text.count(old) != 1:
            raise ValueError(f'{label} source shape changed: {path}')
        changed[path] = text.replace(old, new, 1)

    replace_once('fs/ext4/ext4.h', '#define AGGRESSIVE_CHECK\n',
                 '#define AGGRESSIVE_CHECK__\n', 'upstream AGGRESSIVE_CHECK')
    replace_once('fs/ext4/inline.c',
                 'static int ext4_prepare_inline_data(handle_t *handle, struct inode *inode,\n'
                 '\t\t\t\t    loff_t len)',
                 'static int ext4_prepare_inline_data(handle_t *handle, struct inode *inode,\n'
                 '\t\t\t\t    unsigned int len)',
                 'upstream inline-data parameter')
    replace_once('fs/ext4/orphan.c', '\tu64 orphan_file_size, orphan_file_blocks;\n', '',
                 'upstream orphan-file size handling')
    replace_once('fs/ext4/orphan.c', '#define EXT4_MAX_ORPHAN_FILE_BLOCKS 512\n\n', '',
                 'remove non-upstream orphan-file limit')
    replace_once('fs/ext4/orphan.c',
                 '\torphan_file_size = i_size_read(inode);\n'
                 '\tif (!orphan_file_size ||\n'
                 '\t    orphan_file_size & (sb->s_blocksize - 1)) {\n'
                 '\t\text4_error(sb, "orphan file has invalid size %llu",\n'
                 '\t\t\t   orphan_file_size);\n'
                 '\t\tret = -EFSCORRUPTED;\n'
                 '\t\tgoto out_put;\n'
                 '\t}\n'
                 '\torphan_file_blocks = orphan_file_size >> sb->s_blocksize_bits;\n'
                 '\tif (orphan_file_blocks > EXT4_MAX_ORPHAN_FILE_BLOCKS ||\n'
                 '\t    orphan_file_blocks > INT_MAX ||\n'
                 '\t    orphan_file_blocks > ext4_blocks_count(EXT4_SB(sb)->s_es)) {\n'
                 '\t\text4_error(sb, "orphan file has invalid block count %llu",\n'
                 '\t\t\t   orphan_file_blocks);\n'
                 '\t\tret = -EFSCORRUPTED;\n'
                 '\t\tgoto out_put;\n'
                 '\t}\n'
                 '\toi->of_blocks = orphan_file_blocks;\n',
                 '\toi->of_blocks = inode->i_size >> sb->s_blocksize_bits;\n',
                 'upstream orphan-file initialization')
    replace_once('fs/ext4/xattr.c',
                 'if (!error && !ext4_handle_valid(handle))',
                 'if (!ext4_handle_valid(handle))',
                 'upstream xattr error path')
    replace_once('fs/ext4/xattr.c',
                 '\t\tif (le32_to_cpu(BHDR(bh)->h_refcount) == 0 ||\n'
                 '\t\t    le32_to_cpu(BHDR(bh)->h_refcount) > EXT4_XATTR_REFCOUNT_MAX) {\n'
                 '\t\t\terr_str = "invalid reference count";\n'
                 '\t\t\tgoto errout;\n'
                 '\t\t}\n', '', 'upstream xattr acceptance checks')

    replace_once('fs/ext4/namei.c', '\tstruct ext4_iloc dir_iloc, inode_iloc;\n', '',
                 'upstream unlink inode-buffer declarations')
    replace_once('fs/ext4/namei.c',
                 '\t/*\n'
                 '\t * Reserve the inode buffers before removing the directory entry.  If\n'
                 '\t * reading either inode table block or getting journal write access fails,\n'
                 '\t * unlink must fail before it changes the directory.\n'
                 '\t */\n'
                 '\tdir_iloc.bh = NULL;\n'
                 '\tinode_iloc.bh = NULL;\n'
                 '\tif (!skip_remove_dentry) {\n'
                 '\t\tretval = ext4_reserve_inode_write(handle, dir, &dir_iloc);\n'
                 '\t\tif (retval)\n'
                 '\t\t\tgoto out_handle;\n'
                 '\t}\n'
                 '\tretval = ext4_reserve_inode_write(handle, inode, &inode_iloc);\n'
                 '\tif (retval)\n'
                 '\t\tgoto out_handle;\n\n', '', 'upstream unlink operation order')
    replace_once('fs/ext4/namei.c',
                 '\tif (dir_iloc.bh)\n\t\tbrelse(dir_iloc.bh);\n'
                 '\tif (inode_iloc.bh)\n\t\tbrelse(inode_iloc.bh);\n', '',
                 'upstream unlink buffer releases')
    replace_once('fs/ext4/namei.c',
                 '\t\text4_ct_nlink(inode, 0, "NAMESPACE.NLINK.ROLLBACK");\n'
                 '\t\tclear_nlink(inode);',
                 '\t\t{\n'
                 '\t\t\tunsigned int before = inode->i_nlink;\n\n'
                 '\t\t\tclear_nlink(inode);\n'
                 '\t\t\text4_ct_ns_record_nlink(inode->i_sb,\n'
                 '\t\t\t\t\t"NAMESPACE.NLINK.ROLLBACK", inode,\n'
                 '\t\t\t\t\tbefore, inode->i_nlink, 4, 0);\n'
                 '\t\t}', 'single mkdir rollback update')
    replace_once('fs/ext4/namei.c',
                 '\t}\n\tent->de->inode = cpu_to_le32(ino);\n'
                 '\tif (ext4_has_feature_filetype(ent->dir->i_sb))',
                 '\t}\n\tif (ext4_has_feature_filetype(ent->dir->i_sb))',
                 'single rename dirent update')
    replace_once('fs/ext4/namei.c',
                 '\t\t\t\t deleted_ino, 2, 0);\n\n\terr = 0;\nout:',
                 '\t\t\t\t deleted_ino, 2, 0);\n\n\treturn 0;\nout:',
                 'upstream successful delete return')

    # Restore the original function, including its diagnostic __func__ and
    # journal/no-journal branches. Reinsert only its reviewed error probes.
    name = 'fs/ext4/ext4_jbd2.c'
    text = changed.get(name, (baseline/name).read_text())
    helper = 'static int ext4_handle_dirty_metadata_error('
    signature = 'int __ext4_handle_dirty_metadata('
    original = (sites.PRISTINE/name).read_text()
    if text.count(helper) != 1 or original.count(signature) != 1:
        raise ValueError('upstream dirty-metadata source shape changed')
    function = original[original.index(signature):]
    for site in rows:
        if site['source'] == name and site['caller'] == '__ext4_handle_dirty_metadata':
            expression = site['source_expression']
            pattern = r'(?<!\w)' + re.escape(expression)
            if len(re.findall(pattern, function)) != 1:
                raise ValueError(f'dirty-metadata probe shape changed: {site["site_id"]}')
            function = re.sub(pattern, lambda match:
                f'EXT4_ES_CALL({site["runtime_selector"]}, {expression})', function, count=1)
    changed[name] = text[:text.index(helper)] + function

    replace_once('fs/ext4/extents.c',
                 '\t\text4_free_blocks(handle, inode, NULL, newblock, 1,\n'
                 '\t\t\t\t EXT4_FREE_BLOCKS_METADATA |\n'
                 '\t\t\t\t EXT4_FREE_BLOCKS_FORGET);\n', '',
                 'upstream extent-grow allocation failure')

    # These base-layer edits neither record a sample nor feed a checker hook.
    # Restore the upstream expressions, returns, and adjacent comments too.
    replace_once('fs/ext4/ext4_jbd2.c',
                 '\tif (!ext4_handle_valid(handle)) {\n'
                 '\t\treturn ext4_get_nojournal();\n\t}',
                 '\tif (!ext4_handle_valid(handle))\n'
                 '\t\treturn ext4_get_nojournal();', 'upstream reserved-start branch')
    pending = next(s for s in rows if s['source'] == 'fs/ext4/extents_status.c'
                   and s['caller'] == '__alloc_pending')
    probe = f'EXT4_ES_CALL({pending["runtime_selector"]}, {pending["source_expression"]})'
    replace_once('fs/ext4/extents_status.c',
                 '\tif (!nofail) {\n' + f'\t\treturn {probe};\n\t}}',
                 '\tif (!nofail)\n' + f'\t\treturn {probe};',
                 'upstream pending-allocation branch')
    replace_once('fs/ext4/extents_status.c',
                 '\telse {\n\t\tes = __es_alloc_extent(false);\n\t}',
                 '\telse\n\t\tes = __es_alloc_extent(false);',
                 'upstream extent-allocation branch')
    replace_once('fs/ext4/inline.c',
                 '\terr = ext4_mark_inode_dirty(handle, inode);\n\treturn err;\n}\n\n'
                 'static int ext4_convert_inline_data_nolock',
                 '\treturn ext4_mark_inode_dirty(handle, inode);\n}\n\n'
                 'static int ext4_convert_inline_data_nolock', 'upstream inline return')
    replace_once('fs/ext4/inline.c',
                 '\tif (likely(err == 0)) {\n'
                 '\t\terr = ext4_mark_inode_dirty(handle, dir);\n\t}',
                 '\tif (likely(err == 0))\n'
                 '\t\terr = ext4_mark_inode_dirty(handle, dir);',
                 'upstream inline-delete branch')
    replace_once('fs/ext4/namei.c',
                 '\terr = ext4_journal_get_write_access(handle, dir->i_sb, bh,\n'
                 '\t\t\t\t\t    EXT4_JTR_NONE);\n'
                 '\tif (err)\n\t\tgoto journal_error;',
                 '\terr = ext4_journal_get_write_access(handle, sb, bh, EXT4_JTR_NONE);\n'
                 '\tif (err)\n\t\tgoto journal_error;',
                 'upstream directory write-access argument')
    replace_once('fs/ext4/namei.c',
                 '\n\t\terr = ext4_journal_get_write_access(handle, dir->i_sb, frame->bh,',
                 '\n\t\terr = ext4_journal_get_write_access(handle, sb, frame->bh,',
                 'upstream directory-frame argument')
    replace_once('fs/ext4/namei.c',
                 '\t\terr = ext4_journal_get_write_access(handle, dir->i_sb, (frame - 1)->bh,',
                 '\t\terr = ext4_journal_get_write_access(handle, sb,\n'
                 '\t\t\t\t\t\t    (frame - 1)->bh,',
                 'upstream directory-index argument')
    replace_once('fs/ext4/namei.c',
                 '\tif (flags & RENAME_EXCHANGE) {\n'
                 '\t\terr = ext4_cross_rename(old_dir, old_dentry,\n'
                 '\t\t\t\t\tnew_dir, new_dentry);\n'
                 '\t} else {\n'
                 '\t\terr = ext4_rename(idmap, old_dir, old_dentry, new_dir,\n'
                 '\t\t\t\t  new_dentry, flags);\n'
                 '\t}\n\treturn err;',
                 '\tif (flags & RENAME_EXCHANGE) {\n'
                 '\t\treturn ext4_cross_rename(old_dir, old_dentry,\n'
                 '\t\t\t\t\t new_dir, new_dentry);\n'
                 '\t}\n\n'
                 '\treturn ext4_rename(idmap, old_dir, old_dentry, new_dir, new_dentry, flags);',
                 'upstream rename return flow')
    replace_once('fs/ext4/orphan.c',
                 '\t\tif (err != -ENOSPC) {\n\t\t\treturn err;\n\t\t}',
                 '\t\tif (err != -ENOSPC)\n\t\t\treturn err;',
                 'upstream orphan fallback branch')
    replace_once('fs/ext4/orphan.c',
                 '\tif (ext4_test_inode_state(inode, EXT4_STATE_ORPHAN_FILE)) {\n'
                 '\t\terr = ext4_orphan_file_del(handle, inode);\n'
                 '\t\treturn err;\n\t}',
                 '\tif (ext4_test_inode_state(inode, EXT4_STATE_ORPHAN_FILE))\n'
                 '\t\treturn ext4_orphan_file_del(handle, inode);',
                 'upstream orphan-delete return')
    name = 'fs/ext4/orphan.c'
    text = changed[name]
    if text.count('goto ct_out;\n') != 3:
        raise ValueError('orphan-file probe exits changed')
    changed[name] = re.sub(r'(goto ct_out;\n)\s*goto out_active;\n', r'\1', text)
    replace_once(name, '\text4_ct_exit(inode->i_sb, &ct_scope, ret);\nout_active:\n',
                 '\text4_ct_exit(inode->i_sb, &ct_scope, ret);\n',
                 'remove unused orphan-add label')
    for name, old in (
        ('fs/ext4/extents.c', '/* Route the formula hook through the same dirty-metadata/inode-dirty\n'
         ' * contracts used by an ordinary extent publication. */\n\n\n'),
        ('fs/ext4/resize.c', '\t/* The group-add superblock hook covers this buffer. Keep the descriptor\n'
         '\t * access hook scoped to the GDT and resize-inode descriptor pointers. */\n')):
        replace_once(name, old, '', 'remove unrelated base-layer comment')
    replace_once('fs/ext4/migrate.c',
                 '\t * Worst case inode allocation can touch the inode bitmap, an\n'
                 '\t * uninitialized block bitmap, the group descriptor, and the inode\n'
                 '\t * table block.  Quota updates need additional credits.\n',
                 '\t * Worst case we can touch the allocation bitmaps and a block\n'
                 '\t * group descriptor block.  We do need to worry about\n'
                 '\t * credits for modifying the quota inode.\n',
                 'upstream migrate comment')


def restore_upstream_formatting(changed, baseline):
    """Drop whitespace-only edits unrelated to the added instrumentation."""
    manifest = json.loads((sites.BASE/'patches/manifest.json').read_text())
    for name in set(changed) | {f['path'] for f in manifest['files']}:
        if not (sites.PRISTINE/name).is_file():
            continue
        text = changed.get(name, (baseline/name).read_text())
        original = (sites.PRISTINE/name).read_text().splitlines(True)
        current = text.splitlines(True)
        result = []
        for tag, a, b, c, d in difflib.SequenceMatcher(
                None, original, current, autojunk=False).get_opcodes():
            before, after = ''.join(original[a:b]), ''.join(current[c:d])
            same_tokens = (before and after and
                           [t[0] for t in sites.tokens(before)] ==
                           [t[0] for t in sites.tokens(after)])
            if tag != 'equal' and (same_tokens or (not before.strip() and not after.strip())):
                result.append(before)
            else:
                result.append(after)
        restored = ''.join(result)
        if restored != text:
            changed[name] = restored


# --- namespace exit models --------------------------------------------------
#
# The rmdir / unlink / rename exit models are part of the checker itself, not of
# the injection layer: they live in the shared base patch
# (../ext4-counter-trace/patches/0001-counter-trace-core-and-probes.patch), which
# this experiment applies as its baseline.  The models judge an error exit by
# what it actually committed -- a failed ext4_mark_inode_dirty() is not a durable
# update -- and inspect only in-memory state, so both experiments share one
# implementation of that logic.
#
# Call sites are NOT shared: the base patch reserves the unlink inode buffers
# before ext4_delete_entry(), which closes the very window these models look for,
# and restore_upstream_semantics() below reverts that so the defects exist here.
# The hooks are inherited from the base patch; this layer only restores upstream
# operation order around them.


def generate():
    mapping = json.loads((PROJECT/'sites.json').read_text())
    policies = [policy(s) for s in mapping['sites']]
    by_id = {p['site_id']: p for p in policies}
    rows = mapping['sites']
    with tempfile.TemporaryDirectory() as temp:
        baseline = Path(temp)
        sites.load_baseline(baseline, rows)
        sites.patch_run(baseline, sites.PATCH)
        changed = {}
        restore_upstream_semantics(changed, baseline, rows)
        groups = {}
        for site in rows:
            groups.setdefault((site['source'], site['runtime_selector']), []).append(site)
        for (name, selector), group in groups.items():
            text = changed.get(name, (baseline/name).read_text())
            marker = f"EXT4_ES_CALL({selector}, {group[0]['source_expression']})"
            if text.count(marker) != 1: raise ValueError(f'probe not unique: {group[0]["site_id"]}')
            text = text.replace(marker, wrapper(group, by_id[group[0]['site_id']]), 1)
            changed[name] = text
        for name in list(changed):
            changed[name] = changed[name].replace('#include <linux/ext4_error_site.h>', '#include <linux/ext4_efi.h>')
        name = 'fs/ext4/counter_trace.c'
        text = (baseline/name).read_text()
        # Both generic formula records and namespace transitions pass these two
        # callsites. BEGIN/END and snapshot scans are rejected by the hook.
        needle = '\ttrace_ext4_counter_event(sb, ct->epoch, seq, op, parent, point, formula,'
        assert text.count(needle) == 2
        text = text.replace(needle, '\text4_efi_formula(sb, point, formula);\n'+needle)
        changed[name] = '#include <linux/ext4_efi.h>\n' + text
        changed['fs/ext4/Makefile'] = (baseline/'fs/ext4/Makefile').read_text() + '\next4-$(CONFIG_EXT4_ERROR_SITE_TRACE) += efi.o\n'
        text = (baseline/'fs/ext4/Kconfig').read_text()
        text = text.replace('bool "Trace reviewed ext4 error sites"',
                            'bool "Trace and inject faults at reviewed ext4 error sites"')
        text = text.replace('depends on EXT4_COUNTER_TRACE && EXT4_FS=y',
                            'depends on EXT4_COUNTER_TRACE && EXT4_FS=y && STACKTRACE && HAVE_SYSCALL_TRACEPOINTS')
        text = text.replace('Tracing is disabled at runtime by default. This does not inject faults.',
                            'Global tracing is disabled by default. A root-owned debugfs session\n'
                            '\t  can inject contract failures after a formula in a native syscall.')
        changed['fs/ext4/Kconfig'] = text
        for asset, name in ASSETS.items(): changed[name] = (PROJECT/'instrumentation'/asset).read_text()
        restore_upstream_formatting(changed, baseline)
        chunks, files = [], []
        for name, after in sorted(changed.items()):
            before = (baseline/name).read_text() if (baseline/name).exists() else ''
            chunks.append(''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                         fromfile=f'a/{name}' if before else '/dev/null', tofile=f'b/{name}')))
            files.append(dict(path=name, before=sites.sha(before.encode()) if before else None, after=sites.sha(after.encode())))
        patch = ''.join(chunks).encode()
        with tempfile.NamedTemporaryFile() as stream:
            stream.write(patch); stream.flush(); sites.patch_run(baseline, Path(stream.name))
        assert all(sites.sha((baseline/f['path']).read_bytes()) == f['after'] for f in files)
    inputs = [Path(__file__).resolve(), PROJECT/'scripts/efi_source_audit.py'] + [PROJECT/'instrumentation'/a for a in ASSETS]
    manifest = dict(schema=1, mode='formula_gated_program_injection',
                    base_sites_sha256=sites.sha((PROJECT/'sites.json').read_bytes()),
                    patch_sha256=sites.sha(patch), replay_verified=True,
                    inputs={p.relative_to(PROJECT).as_posix(): sites.sha(p.read_bytes()) for p in inputs},
                    summary=dict(observed=len(rows), supported=sum(p['supported'] for p in policies),
                                 stateful_observe_only=sum(not p['supported'] for p in policies)),
                    files=files, policies=policies)
    return patch, manifest


def prepare(manifest):
    source = PROJECT/'dev/linux-6.12.1'
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['cp', '-al', str(sites.PRISTINE), str(source)], check=True)
    # The tree is a build product of (pristine, base patch, review patch), so the
    # layers are reapplied whenever it is not already at the layer-1 state.  A
    # tree that was recreated from the pristine source is indistinguishable from
    # a missing one here, and skipping the layers would leave it unauditable.
    def at_before():
        return all((source/f['path']).is_file() and
                   sites.sha((source/f['path']).read_bytes()) == f['before']
                   if f['before'] else not (source/f['path']).exists()
                   for f in manifest['files'])

    def at_after():
        return all((source/f['path']).is_file() and
                   sites.sha((source/f['path']).read_bytes()) == f['after']
                   if f['after'] else not (source/f['path']).exists()
                   for f in manifest['files'])

    if not at_before() and not at_after():
        sites.patch_run(source, sites.BASE_PATCH)
        sites.patch_run(source, sites.PATCH)
    if at_before():
        sites.patch_run(source, PATCH)
    report = verify_source(source, PROJECT, manifest)
    output = PROJECT/'build/linux-error-site-trace/source-audit.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2)+'\n')
    print(f'full_source_audit=pass files={report["regular_files"]} '
          f'changed={report["changed_upstream_files"]} added={report["added_files"]}')
    print(f'injection_source_verified={source}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--prepare-source', action='store_true')
    args = parser.parse_args()
    patch, manifest = generate()
    for path, content in [(PATCH, patch), (MANIFEST, (json.dumps(manifest, indent=2)+'\n').encode())]:
        if args.check or args.prepare_source:
            if not path.is_file() or path.read_bytes() != content: raise ValueError(f'stale output: {path}')
        else: path.write_bytes(content)
    if args.prepare_source: prepare(manifest)
    print(json.dumps(manifest['summary']))


if __name__ == '__main__': main()
