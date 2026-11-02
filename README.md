# ext4 filesystem fuzzing and consistency experiments

Linux x86-64 project for building instrumented Linux 6.12.1 kernels and
running repeatable ext4 accounting and fault-injection experiments under QEMU.
The test code, kernel patches, kernel configurations, and analysis reports are
versioned. Official Linux source, compiler output, guest
images, generated programs, SSH credentials, and raw campaign results are
created locally and excluded from Git.

## Build a Debian or Ubuntu host

Use an x86-64 Debian or Ubuntu machine with at least 16 GB of RAM and 40 GB of
free disk space. A hardware KVM device speeds up QEMU but is optional; the
tracked runner configurations use software emulation. The build downloads the
official Linux 6.12.1 source archive and pinned syzkaller source, then builds
the kernel and host tools. Expect the first build and guest image creation to
take some time.

```sh
git clone https://github.com/yghfs/fuzzing_orcle.git
cd fuzzing_orcle
EXT4_CT_JOBS=4 tools/bootstrap.sh
```

`tools/bootstrap.sh` installs host packages with `sudo`, installs Go 1.26.0,
fetches the pinned syzkaller revision, verifies the official Linux 6.12.1
source, and builds the invariant-checker and counter-trace kernels. It then
builds the syzkaller tools and creates a local Debian guest image and SSH key
under `work/`. Set `EXT4_CT_JOBS` to limit build parallelism. The separate
source-fault kernel is built when requested below. Images, build trees, and
private keys stay on the host and are excluded from Git.

Each step can also be run individually:

```sh
tools/install-deps.sh
tools/install-go.sh
tools/install-syzkaller.sh
bash experiments/ext4-counter-trace/scripts/prepare-dev-source.sh
bash experiments/ext4-counter-trace/scripts/build.sh
bash experiments/ext4-source-fault-study/scripts/build-local-kernel.sh
tools/create-vm-image.sh
```

## Run a smoke test

The counter-trace smoke run boots a disposable QEMU guest and four ext4
filesystem variants, checks the recorded events and stable snapshots, and
validates each disk after guest shutdown:

```sh
bash experiments/ext4-counter-trace/scripts/run-smoke.sh
```

Run the longer formula-chain validation with:

```sh
bash experiments/ext4-counter-trace/scripts/run-chain.sh
```

The original invariant-checker kernel and its reproducible patch are kept in
`experiments/ext4-invariant-checker/`. Generate a manager config for this
checkout with `python3 tools/configure-syzkaller.py tools/ext4-manager.cfg`,
then run `tools/syzkaller/bin/syz-manager -config=tools/ext4-manager.local.cfg
-mode=smoke-test`.

Run outputs are written under the ignored
`experiments/ext4-counter-trace/results/` directory. Override `EXT4_CT_JOBS`
to control compilation parallelism and `EXT4_CT_PORT` or
`EXT4_CT_CHAIN_PORT` if a local QEMU SSH-forwarding port is occupied.

## Run the source fault-injection study

After the bootstrap has prepared Linux 6.12.1 and installed the build
dependencies, build and run the separate fault-injection kernel:

```sh
bash experiments/ext4-source-fault-study/scripts/build-local-kernel.sh
KERNEL=experiments/ext4-source-fault-study/build/linux-source-fault/arch/x86/boot/bzImage \
  bash experiments/ext4-source-fault-study/scripts/run-qemu-cases.sh
```

Choose one test case by setting `CASE_FILTER` (for example,
`CASE_FILTER=unlink-parent`). The test builds a temporary guest initramfs,
creates test filesystems, injects the selected fault in QEMU, and runs offline
filesystem checks. It does not use a host disk device. Read
[`experiments/ext4-source-fault-study/README.md`](experiments/ext4-source-fault-study/README.md)
for the interpretation and boundaries of each finding.

## Repository scope

Linux source is downloaded from kernel.org and verified against the checked-in
official SHA-256 list. The counter-trace and source-fault study patches are
kept separately under `experiments/`. The exact syzkaller revision used to
build the program generator is recorded in `tools/syzkaller-revision.txt` and
the Go module pins the same revision.

VM images and SSH keys must be created separately on each machine; they are
deliberately not distributed through GitHub. To add new experiment results,
commit a concise report or small evidence file rather than a raw VM disk,
build output tree, or unbounded campaign directory.
