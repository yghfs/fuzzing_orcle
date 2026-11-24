# Host setup

Run `bash tools/bootstrap.sh` on a Debian or Ubuntu x86-64 host. It prepares
the pinned Linux and syzkaller sources, builds the unified checker, and prepares
a local guest image and SSH key. See the
[root README](../README.md) for host requirements and the smoke test.

Load the checkout paths with `. tools/env.sh`. For scripts that only need the
kernel paths, source `tools/kernel-paths.sh` instead. `FUZZING_ORCLE_KERNEL`
points to the prepared source, `FUZZING_ORCLE_KERNEL_OBJ` to the normal build
directory, and `FUZZING_ORCLE_BZIMAGE` to its bootable image.
`FUZZING_ORCLE_ROOTFS` defaults to `work/debian-normal.img`; override it with
the path to a prepared guest image. Go 1.26.0 is installed locally; the
syzkaller revision is listed in
[syzkaller-revision.txt](syzkaller-revision.txt).
`install-syzkaller.sh` checks that the generator's Go module agrees with the
pin. An existing source copy without Git metadata is reused with its revision
reported as unverified.

Generate a config for the local checkout from a tracked template:

```sh
python3 tools/configure-syzkaller.py tools/ext4-manager.cfg
```

The ignored `tools/ext4-manager.local.cfg` points to the normal build and
guest image. Run `bash tools/create-vm-image.sh` if the default image is
missing. This prepares and checks `work/debian-normal.img` with the ext4
`orphan_file` feature disabled on the copy, preserving the existing base
image. Regenerate existing `.local.cfg` files after switching builds or images.
The generator fills missing paths and respects the individual template's HTTP
address. Use the environment variables above to select a custom build or image.

The supported regression commands are listed in [tests/README.md](../tests/README.md).
