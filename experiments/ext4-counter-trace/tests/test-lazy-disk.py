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
    def check_bigalloc_feature_layout(self, bigalloc, flex_bg):
        with tempfile.TemporaryDirectory() as temp:
            image = pathlib.Path(temp) / 'features.raw'
            with image.open('wb') as fp:
                fp.truncate(128 * 1024**2)
            features = ['extent', 'bigalloc' if bigalloc else '^bigalloc',
                        'flex_bg' if flex_bg else '^flex_bg']
            options = ['-C', '65536'] if bigalloc else []
            subprocess.run(['mke2fs', '-q', '-F', '-t', 'ext4', '-b', '4096',
                            '-N', '256', '-O', ','.join(features),
                            *options, str(image)], check=True)
            view = checker.Image(image)
            try:
                self.assertEqual(view.bigalloc, bigalloc)
                self.assertEqual(view.cluster_ratio, 16 if bigalloc else 1)
                # A 128 MiB, 4 KiB-block image has 32768 blocks. Its bitmap
                # represents 2048 clusters only when bigalloc is enabled.
                self.assertEqual(view.valid_clusters(0), 2048 if bigalloc else 32768)
                report = view.check()
                self.assertTrue(report['valid'], report)
            finally:
                view.fp.close()

    def test_regular_with_flex_bg_is_not_bigalloc(self):
        self.check_bigalloc_feature_layout(bigalloc=False, flex_bg=True)

    def test_regular_without_flex_bg(self):
        self.check_bigalloc_feature_layout(bigalloc=False, flex_bg=False)

    def test_bigalloc_with_flex_bg(self):
        self.check_bigalloc_feature_layout(bigalloc=True, flex_bg=True)

    def test_bigalloc_without_flex_bg(self):
        self.check_bigalloc_feature_layout(bigalloc=True, flex_bg=False)

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
