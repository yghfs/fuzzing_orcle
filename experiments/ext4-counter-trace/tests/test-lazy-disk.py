#!/usr/bin/env python3
"""Real sparse ext4 images: independent lazy bitmap count and negative mutation."""
import importlib.util
import pathlib
import struct
import subprocess
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('checker',pathlib.Path(__file__).resolve().parents[1]/'scripts/check-disk.py')
checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)


class LazyDiskTests(unittest.TestCase):
    def test_real_layouts_and_opposite_descriptor_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            for name,size,options in [
                ('1k',270,['-b','1024']),
                ('flex',1154,['-b','4096']),
                ('bigalloc',8200,['-b','4096','-O','bigalloc','-C','65536']),
                ('sparse2',1154,['-b','4096','-O','sparse_super2']),
                ('no_desc_csum',270,['-b','1024','-O','^metadata_csum'])]:
                with self.subTest(layout=name):
                    image=pathlib.Path(temp)/(name+'.raw')
                    with image.open('wb') as fp:fp.truncate(size*1024**2)
                    subprocess.run(['mke2fs','-q','-F','-t','ext4','-N','2048','-E',
                                    'lazy_itable_init=1',*options,str(image)],check=True)
                    view=checker.Image(image)
                    if name == 'bigalloc':
                        # e2fsprogs materializes every bigalloc block bitmap.
                        # Select an empty group with its flex metadata elsewhere;
                        # independently validate the equivalent logical flag.
                        group=next(g for g in range(1,view.groups) if view.uninit_free_clusters(g,view.group_info(g))==view.group_info(g)['free_blocks'])
                        info=view.group_info(group)
                        with image.open('r+b') as fp:
                            fp.seek(view.gdt_offset+group*view.desc_size+18)
                            fp.write(struct.pack('<H',info['flags']|2))
                    report=view.check()
                    self.assertTrue(report['valid'],report)
                    if name == 'no_desc_csum':
                        self.assertFalse(view.group_desc_csum)
                        self.assertFalse(report['r_itable_highwater']['checked'])
                        self.assertEqual(report['r_itable_highwater']['unsupported_groups_skipped'],
                                         view.groups)
                        view.fp.close()
                        image.unlink()
                        continue
                    uninit=[g for g in report['groups'] if g['flags']&2]
                    self.assertTrue(uninit,'must exercise BLOCK_UNINIT')
                    # Keep the global descriptor sum unchanged, corrupt only
                    # two local counts. The independent layout must reject it.
                    left,right=uninit[0]['group'],0
                    with image.open('r+b') as fp:
                        for group,delta in [(left,-1),(right,1)]:
                            offset=view.gdt_offset+group*view.desc_size+12
                            fp.seek(offset);value=struct.unpack('<H',fp.read(2))[0]
                            fp.seek(offset);fp.write(struct.pack('<H',value+delta))
                    view.fp.close()
                    changed=checker.Image(image)
                    self.assertFalse(changed.check()['valid'])
                    changed.fp.close()
                    image.unlink()

if __name__=='__main__':unittest.main()
