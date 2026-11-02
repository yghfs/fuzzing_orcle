// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/ext4.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/xattr.h>
#include <unistd.h>

#define NS_LOOPS 256
#define DIR_ENTRIES 4096
#define INODE_LIMIT 20000

static void fail(const char *what)
{
	perror(what);
	exit(1);
}

static int mkdirat_ok(int dfd, const char *name)
{
	if (mkdirat(dfd, name, 0700) < 0)
		return -1;
	return 0;
}

static void namespace_churn(int rootfd)
{
	unsigned int i;
	char name[64], linkname[64], renamed[64];
	int fd;

	fd = openat(rootfd, "link-source", O_CREAT | O_EXCL | O_RDWR, 0600);
	if (fd < 0)
		fail("create link source");
	if (fsync(fd) < 0)
		fail("fsync link source");
	close(fd);
	for (i = 0; i < NS_LOOPS; i++) {
		snprintf(name, sizeof(name), "dir-%04u", i);
		if (mkdirat_ok(rootfd, name) < 0)
			fail("mkdir churn");
		snprintf(linkname, sizeof(linkname), "link-%04u", i);
		if (linkat(rootfd, "link-source", rootfd, linkname, 0) < 0)
			fail("link churn");
		snprintf(renamed, sizeof(renamed), "renamed-%04u", i);
		if (renameat(rootfd, linkname, rootfd, renamed) < 0)
			fail("rename churn");
		if (unlinkat(rootfd, renamed, 0) < 0 ||
		    unlinkat(rootfd, name, AT_REMOVEDIR) < 0)
			fail("namespace cleanup");
	}
	if (unlinkat(rootfd, "link-source", 0) < 0)
		fail("unlink link source");
	printf("namespace_churn=%u mkdir/link/rename/unlink cycles\n", i);
}

static void attempt_resize(const char *mountpoint)
{
	struct statfs before, after;
	uint32_t target = 48000;
	void *pressure = NULL;
	const char *pressure_mb = getenv("EXT4_RESIZE_MEMORY_PRESSURE_MB");
	int fd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	int rc, saved;

	if (fd < 0)
		fail("open mountpoint for resize");
	if (fstatfs(fd, &before) < 0)
		fail("statfs before resize");
	if (pressure_mb && strtoul(pressure_mb, NULL, 10)) {
		size_t bytes = (size_t)strtoul(pressure_mb, NULL, 10) * 1024 * 1024;
		long page_size = sysconf(_SC_PAGESIZE);
		volatile unsigned char *p;
		pressure = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
				MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (pressure == MAP_FAILED)
			fail("mmap resize memory pressure");
		p = pressure;
		for (size_t offset = 0; offset < bytes; offset += (size_t)page_size)
			p[offset] = (unsigned char)offset;
		printf("resize_memory_pressure_bytes=%zu page_size=%ld\n",
		       bytes, page_size);
	}
	errno = 0;
	rc = ioctl(fd, EXT4_IOC_GROUP_EXTEND, &target);
	saved = errno;
	if (fstatfs(fd, &after) < 0)
		fail("statfs after resize");
	printf("resize_ioctl_rc=%d errno=%d(%s) blocks_before=%" PRIu64
	       " blocks_after=%" PRIu64 " target=%u\n", rc, saved,
	       strerror(saved), (uint64_t)before.f_blocks,
	       (uint64_t)after.f_blocks, target);
	target = 70000;
	errno = 0;
	rc = ioctl(fd, EXT4_IOC_GROUP_EXTEND, &target);
	saved = errno;
	if (fstatfs(fd, &after) < 0)
		fail("statfs after rejected resize");
	printf("resize_beyond_device_rc=%d errno=%d(%s) blocks_after=%" PRIu64
	       " target=%u\n", rc, saved, strerror(saved),
	       (uint64_t)after.f_blocks, target);
	if (pressure && munmap(pressure,
			      (size_t)strtoul(pressure_mb, NULL, 10) * 1024 * 1024) < 0)
		fail("munmap resize memory pressure");
	close(fd);
}

static void fill_directory(int rootfd)
{
	int dfd, fd;
	unsigned int i;
	unsigned int linked = 0;
	char name[48];
	struct stat st_before, st_after;

	if (mkdirat_ok(rootfd, "full-dir") < 0)
		fail("mkdir full-dir");
	dfd = openat(rootfd, "full-dir", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dfd < 0)
		fail("open full-dir");
	fd = openat(rootfd, "hardlink-target", O_CREAT | O_EXCL | O_RDWR, 0600);
	if (fd < 0)
		fail("create hardlink target");
	close(fd);
	for (i = 0; i < DIR_ENTRIES; i++) {
		snprintf(name, sizeof(name), "entry-%05u-abcdefghijkl", i);
		fd = openat(dfd, name, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
		if (fd < 0) {
			printf("directory_fill_stop=%u errno=%d(%s)\n", i, errno,
			       strerror(errno));
			break;
		}
		close(fd);
	}
	if (fsync(dfd) < 0 || syncfs(dfd) < 0)
		fail("sync directory fill");
	if (fstatat(rootfd, "hardlink-target", &st_before, 0) < 0)
		fail("stat hardlink target");

	fd = openat(rootfd, "space-filler", O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC,
		    0600);
	if (fd < 0)
		fail("create filler");
	errno = 0;
	if (fallocate(fd, 0, 0, 512LL * 1024 * 1024) < 0)
		printf("fallocate_full_errno=%d(%s)\n", errno, strerror(errno));
	else
		puts("fallocate_full=completed");
	if (fsync(fd) < 0)
		printf("filler_fsync_errno=%d(%s)\n", errno, strerror(errno));
	close(fd);
	if (syncfs(dfd) < 0)
		printf("syncfs_full_errno=%d(%s)\n", errno, strerror(errno));

	for (i = 0; i < 4096; i++) {
		snprintf(name, sizeof(name), "hardlink-%03u", i);
		errno = 0;
		if (linkat(rootfd, "hardlink-target", dfd, name, 0) < 0) {
			printf("hardlink_failure_attempt=%u errno=%d(%s)\n", i,
			       errno, strerror(errno));
			break;
		}
		linked++;
	}
	if (fstatat(rootfd, "hardlink-target", &st_after, 0) < 0)
		fail("stat hardlink target after");
	printf("hardlink_successes=%u hardlink_target_nlink_before=%lu "
	       "after=%lu expected_after=%lu\n", linked,
	       (unsigned long)st_before.st_nlink,
	       (unsigned long)st_after.st_nlink,
	       (unsigned long)st_before.st_nlink + linked);
	errno = 0;
	if (mkdirat(dfd, "mkdir-at-full", 0700) < 0)
		printf("mkdir_full_errno=%d(%s)\n", errno, strerror(errno));
	else
		puts("mkdir_at_full=succeeded");

	if (unlinkat(rootfd, "space-filler", 0) < 0)
		fail("remove filler");
	close(dfd);
}

static void inode_pressure(int rootfd)
{
	int dfd, fd;
	unsigned int i;
	char name[48];
	struct statfs st;

	if (mkdirat_ok(rootfd, "inode-pressure") < 0)
		fail("mkdir inode-pressure");
	dfd = openat(rootfd, "inode-pressure", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dfd < 0)
		fail("open inode-pressure");
	for (i = 0; i < INODE_LIMIT; i++) {
		snprintf(name, sizeof(name), "inode-%05u", i);
		fd = openat(dfd, name, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
		if (fd < 0) {
			printf("inode_alloc_stop=%u errno=%d(%s)\n", i, errno,
			       strerror(errno));
			break;
		}
		close(fd);
	}
	if (fstatfs(dfd, &st) < 0)
		fail("statfs inode pressure");
	printf("inode_pressure_created=%u ffree=%" PRIu64 "\n", i,
	       (uint64_t)st.f_ffree);
	for (unsigned int n = 0; n < i; n++) {
		snprintf(name, sizeof(name), "inode-%05u", n);
		if (unlinkat(dfd, name, 0) < 0)
			fail("unlink inode pressure");
	}
	if (fsync(dfd) < 0 || syncfs(dfd) < 0)
		fail("sync inode cleanup");
	close(dfd);
	if (unlinkat(rootfd, "inode-pressure", AT_REMOVEDIR) < 0)
		fail("rmdir inode-pressure");
}

static void ea_inode_churn(const char *mountpoint)
{
	char path[512], *value;
	size_t size = 60000;
	int fd;

	snprintf(path, sizeof(path), "%s/ea-target", mountpoint);
	fd = open(path, O_CREAT | O_RDWR | O_TRUNC | O_CLOEXEC, 0600);
	if (fd < 0)
		fail("create ea target");
	value = malloc(size);
	if (!value)
		fail("allocate ea value");
	memset(value, 'E', size);
	if (setxattr(path, "user.large", value, size, XATTR_CREATE) < 0)
		printf("setxattr_create_errno=%d(%s)\n", errno, strerror(errno));
	else
		puts("setxattr_create=completed");
	value[0] = 'R';
	if (setxattr(path, "user.large", value, size, XATTR_REPLACE) < 0)
		printf("setxattr_replace_errno=%d(%s)\n", errno, strerror(errno));
	else
		puts("setxattr_replace=completed");
	if (removexattr(path, "user.large") < 0)
		printf("removexattr_errno=%d(%s)\n", errno, strerror(errno));
	else
		puts("removexattr=completed");
	free(value);
	close(fd);
	if (unlink(path) < 0)
		fail("remove ea target");
}

int main(int argc, char **argv)
{
	const char *mountpoint = argc > 1 ? argv[1] : "/mnt/formula";
	int rootfd;
	while (access("/tmp/formula-probe/go", F_OK) < 0)
		usleep(1000);

	rootfd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (rootfd < 0)
		fail("open ext4 mount");
	if (getenv("EXT4_EA_ONLY")) {
		ea_inode_churn(mountpoint);
		if (syncfs(rootfd) < 0)
			fail("ea-only syncfs");
		close(rootfd);
		return 0;
	}
	attempt_resize(mountpoint);
	if (getenv("EXT4_RESIZE_ONLY")) {
		if (syncfs(rootfd) < 0)
			fail("resize-only syncfs");
		close(rootfd);
		return 0;
	}
	if (getenv("EXT4_EA_INODE"))
		ea_inode_churn(mountpoint);
	namespace_churn(rootfd);
	fill_directory(rootfd);
	inode_pressure(rootfd);
	if (syncfs(rootfd) < 0)
		fail("final syncfs");
	close(rootfd);
	return 0;
}
