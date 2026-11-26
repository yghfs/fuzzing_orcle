# ext4 filesystem fuzzing and consistency experiments

Linux x86-64 project for building instrumented Linux 6.12.1 kernels and running
repeatable ext4 accounting and consistency experiments under QEMU.
Source, patches, configurations and final reports are versioned; kernels, guest images and raw results are local.

Current implementation and evidence availability are indexed in
[implementation status](experiments/ext4-counter-trace/IMPLEMENTATION-STATUS.md).
Final findings are indexed in [BUG-INVENTORY.md](BUG-INVENTORY.md), including the
[recognized and fixed `__ext4_unlink()` bug](BUG-INVENTORY.md).

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
source, and builds the normal kernel with the unified ext4 consistency
checker (`CONFIG_EXT4_COUNTER_TRACE`). Bootstrap also
builds the syzkaller tools and prepares `work/debian-normal.img` and a local
SSH key. The normal guest is a verified copy of the base Debian image with
the ext4 `orphan_file` feature disabled. The base image stays intact. Set
`EXT4_CT_JOBS` to limit build parallelism. Images, build trees, and private
keys stay on the host and are excluded from Git.

Each step can also be run individually:

```sh
tools/install-deps.sh
tools/install-go.sh
tools/install-syzkaller.sh
bash experiments/ext4-counter-trace/scripts/prepare-dev-source.sh
bash experiments/ext4-counter-trace/scripts/build.sh --normal
tools/build-syzkaller.sh
tools/create-vm-image.sh
```

## Run a smoke test

The smoke run boots a disposable QEMU guest, checks counters across four
ext4 filesystem variants and verifies the model summary on the regular
filesystem. It validates each disk after guest shutdown:

```sh
bash experiments/ext4-counter-trace/scripts/run-smoke.sh
```

Run the longer formula-chain validation with:

```sh
bash experiments/ext4-counter-trace/scripts/run-chain.sh
```

Both use `experiments/ext4-counter-trace/build/linux-counter-trace-normal/`
and default to `work/debian-normal.img`. The manager config generator also
selects this build and guest image:

```sh
python3 tools/configure-syzkaller.py tools/ext4-manager.cfg
tools/syzkaller/bin/syz-manager -config=tools/ext4-manager.local.cfg -mode=smoke-test
```

See [tests/README.md](tests/README.md) for the ordinary invariant checker
regressions and [tools/README.md](tools/README.md) for local paths.

Run outputs are written under the ignored
`experiments/ext4-counter-trace/results/` directory. Override `EXT4_CT_JOBS`
to control compilation parallelism and `EXT4_CT_PORT` or
`EXT4_CT_CHAIN_PORT` if a local QEMU SSH-forwarding port is occupied.

## Research builds

The [checker documentation](experiments/ext4-counter-trace/README.md) describes
the unified checker and its research build.

The [counter-trace + fault injection experiment](experiments/ext4-counter-trace-fault-injection/README.md)
adds per-site execution tracing for all 265 reviewed ext4 error-site records,
formula-gated fault injection for 215 of them, and program-level fault-sequence
mutation on one continuously mounted filesystem. The isolated kernel build,
native syscall correlation, checker integration, and QEMU regression harness are included.

## Repository scope

Linux source is downloaded from kernel.org and verified against the checked-in
official SHA-256 list. The counter-trace patch is kept under `experiments/`.
The exact syzkaller revision used to
build the program generator is recorded in `tools/syzkaller-revision.txt` and
the Go module pins the same revision.

VM images and SSH keys must be created separately on each machine; they are
deliberately not distributed through GitHub. To add new experiment results,
commit a concise report or small evidence file rather than a raw VM disk,
build output tree, or unbounded campaign directory.
