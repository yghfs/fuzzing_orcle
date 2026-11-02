#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
	git curl ca-certificates golang-go make gcc g++ \
	qemu-system-x86 qemu-utils debootstrap openssh-client sudo \
	build-essential bc bison flex libelf-dev libssl-dev libncurses-dev \
	dwarves cpio clang llvm lld pkg-config e2fsprogs busybox-static
