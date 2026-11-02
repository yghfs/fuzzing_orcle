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
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/ioctl.h>
#include <sys/types.h>
#include <unistd.h>

static int write_control(const char *path, const char *value)
{
	int fd = open(path, O_WRONLY | O_CLOEXEC);
	size_t len = strlen(value);
	ssize_t written;

	if (fd < 0)
		return -1;
	written = write(fd, value, len);
	close(fd);
	return written == (ssize_t)len ? 0 : -1;
}

int main(int argc, char **argv)
{
	const char *mountpoint = argc > 1 ? argv[1] : "/mnt/ext4-resize";
	uint32_t blocks = argc > 2 ? (uint32_t)strtoul(argv[2], NULL, 0) : 61312;
	unsigned long pressure_mb = argc > 3 ? strtoul(argv[3], NULL, 0) : 192;
	unsigned long memcg_limit_mb = argc > 4 ? strtoul(argv[4], NULL, 0) : 0;
	size_t pressure_len = (size_t)pressure_mb * 1024 * 1024;
	void *pressure = MAP_FAILED;
	struct statfs before, after;
	int fd, rc, saved_errno;
	char value[64];

	if (memcg_limit_mb) {
		if (mkdir("/sys/fs/cgroup/ext4-resize-pressure", 0755) &&
		    errno != EEXIST) {
			perror("mkdir memory cgroup");
			return 2;
		}
		snprintf(value, sizeof(value), "%lu\n",
			 memcg_limit_mb * 1024 * 1024);
		if (write_control("/sys/fs/cgroup/ext4-resize-pressure/memory.max",
				  value) ||
		    write_control("/sys/fs/cgroup/ext4-resize-pressure/memory.swap.max",
				  "0\n")) {
			perror("set memory cgroup limit");
			return 2;
		}
		snprintf(value, sizeof(value), "%d\n", getpid());
		if (write_control("/sys/fs/cgroup/ext4-resize-pressure/cgroup.procs",
				  value)) {
			perror("move into memory cgroup");
			return 2;
		}
	}

	pressure = mmap(NULL, pressure_len, PROT_READ | PROT_WRITE,
			MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (pressure == MAP_FAILED) {
		perror("mmap pressure");
		return 2;
	}
	for (size_t off = 0; off < pressure_len; off += 4096)
		*((volatile unsigned char *)pressure + off) = 0xa5;
	if (mlock(pressure, pressure_len)) {
		perror("mlock pressure");
		return 2;
	}
	printf("pressure_populated_mb=%lu memcg_limit_mb=%lu\n",
	       pressure_mb, memcg_limit_mb);
	fflush(stdout);

	fd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (fd < 0) {
		perror("open mountpoint");
		return 2;
	}
	if (fstatfs(fd, &before)) {
		perror("statfs before");
		return 2;
	}
	rc = ioctl(fd, EXT4_IOC_GROUP_EXTEND, &blocks);
	saved_errno = errno;
	if (fstatfs(fd, &after)) {
		perror("statfs after");
		return 2;
	}
	printf("group_extend_blocks=%" PRIu32 " rc=%d errno=%d (%s)\n",
	       blocks, rc, saved_errno, strerror(saved_errno));
	printf("statfs_blocks_before=%" PRIu64 " after=%" PRIu64 "\n",
	       (uint64_t)before.f_blocks, (uint64_t)after.f_blocks);
	fflush(stdout);
	close(fd);
	munlock(pressure, pressure_len);
	munmap(pressure, pressure_len);
	return 0;
}
