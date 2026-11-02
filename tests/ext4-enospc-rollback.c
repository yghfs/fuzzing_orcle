// SPDX-License-Identifier: GPL-2.0-only
/* Exercise real ENOSPC through buffered allocation and range fallbacks. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/falloc.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <unistd.h>

#define BLOCK 4096U
#define CHUNK (1024U * 1024U)
#define MAX_FILES 32
#define NS_FILES 4096

static unsigned char write_buf[CHUNK];

static int create_namespace_pressure(const char *dir)
{
	char path[512];
	int fd;

	snprintf(path, sizeof(path), "%s/rename-source", dir);
	if (mkdir(path, 0700) < 0)
		return -1;
	snprintf(path, sizeof(path), "%s/rename-target", dir);
	if (mkdir(path, 0700) < 0)
		return -1;
	for (unsigned int i = 0; i < NS_FILES * 2; i++) {
		char name[64];
		const char *parent = i < NS_FILES ? "rename-source" : "rename-target";

		snprintf(name, sizeof(name), "%s/%s/%08u", dir, parent,
			 i < NS_FILES ? i : i - NS_FILES);
		fd = open(name, O_CREAT | O_EXCL | O_WRONLY, 0600);
		if (fd < 0)
			return -1;
		if (close(fd) < 0)
			return -1;
	}
	return 0;
}

static int rename_until_enospc(const char *dir)
{
	char source[512], target[512];
	unsigned int renamed = 0;
	int failed = 0, source_exists, target_exists;

	for (; renamed < NS_FILES; renamed++) {
		snprintf(source, sizeof(source), "%s/rename-source/%08u", dir, renamed);
		snprintf(target, sizeof(target), "%s/rename-target/moved-%08u", dir, renamed);
		if (rename(source, target) == 0)
			continue;
		failed = errno;
		break;
	}
	fprintf(stderr, "rename_pressure_completed=%u stop_errno=%d (%s)\n",
		renamed, failed, strerror(failed));
	if (failed != ENOSPC)
		return -1;
	source_exists = access(source, F_OK) == 0;
	target_exists = access(target, F_OK) == 0;
	if (!source_exists || target_exists) {
		fprintf(stderr, "rename_failure_namespace_bad source=%d target=%d\n",
			source_exists, target_exists);
		return -1;
	}
	return 0;
}

static int remove_namespace_pressure(const char *dir)
{
	char path[512];

	for (unsigned int i = 0; i < NS_FILES; i++) {
		snprintf(path, sizeof(path), "%s/rename-source/%08u", dir, i);
		if (unlink(path) < 0 && errno != ENOENT)
			return -1;
		snprintf(path, sizeof(path), "%s/rename-target/%08u", dir, i);
		if (unlink(path) < 0)
			return -1;
		snprintf(path, sizeof(path), "%s/rename-target/moved-%08u", dir, i);
		if (unlink(path) < 0 && errno != ENOENT)
			return -1;
	}
	snprintf(path, sizeof(path), "%s/rename-source", dir);
	if (rmdir(path) < 0)
		return -1;
	snprintf(path, sizeof(path), "%s/rename-target", dir);
	return rmdir(path);
}

static int fill_until_enospc(const char *dir, int *out_fd, uint64_t *written)
{
	char path[256];
	int fd, i;
	ssize_t n;

	snprintf(path, sizeof(path), "%s/fill", dir);
	fd = open(path, O_CREAT | O_TRUNC | O_RDWR, 0600);
	if (fd < 0)
		return -1;
	memset(write_buf, 0x5a, sizeof(write_buf));
	*written = 0;
	for (i = 0; i < 16384; i++) {
		n = write(fd, write_buf, sizeof(write_buf));
		if (n > 0) {
			*written += (uint64_t)n;
			if ((n == (ssize_t)sizeof(write_buf) && !(i & 31)) && fsync(fd) < 0) {
				perror("fill fsync");
				close(fd);
				return -1;
			}
			continue;
		}
		if (n < 0 && (errno == ENOSPC || errno == EDQUOT)) {
			printf("fill_stop errno=%d (%s) written=%llu\n", errno,
			       strerror(errno), (unsigned long long)*written);
			*out_fd = fd;
			return 0;
		}
		fprintf(stderr, "fill unexpected n=%lld errno=%d (%s)\n",
			(long long)n, errno, strerror(errno));
		close(fd);
		return -1;
	}
	fprintf(stderr, "fill did not reach ENOSPC written=%llu\n",
		(unsigned long long)*written);
	*out_fd = fd;
	return 1;
}

static void report_fallocate(const char *label, int fd, int mode,
			     off_t off, off_t len)
{
	int saved, rc;

	errno = 0;
	rc = fallocate(fd, mode, off, len);
	saved = errno;
	printf("%s rc=%d errno=%d (%s) off=%lld len=%lld\n", label, rc,
	       saved, strerror(saved), (long long)off, (long long)len);
}

static int verify_byte(int fd, off_t off, unsigned char want)
{
	unsigned char b;
	if (pread(fd, &b, 1, off) != 1) {
		perror("verify pread");
		return -1;
	}
	printf("verify off=%lld byte=0x%02x expected=0x%02x\n",
	       (long long)off, b, want);
	return b == want ? 0 : -1;
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/tmp/ext4-enospc";
	char path[256];
	struct statvfs sv;
	int fill_fd = -1, probe_fd = -1, i, rc = 1;
	uint64_t written = 0;
	off_t tail;

	if (mkdir(dir, 0700) < 0 && errno != EEXIST) {
		perror("mkdir");
		return 2;
	}
	if (create_namespace_pressure(dir) < 0) {
		perror("prepare rename namespace pressure");
		return 1;
	}
	if (statvfs(dir, &sv) == 0)
		printf("before free_blocks=%llu blocks=%llu bsize=%lu\n",
		       (unsigned long long)sv.f_bfree,
		       (unsigned long long)sv.f_blocks, (unsigned long)sv.f_bsize);
	memset(write_buf, 0x5a, sizeof(write_buf));
	i = fill_until_enospc(dir, &fill_fd, &written);
	if (i != 0)
		goto out;
	if (fill_fd < 0)
		goto out;
	if (fsync(fill_fd) < 0)
		perror("fill final fsync");
	if (rename_until_enospc(dir) < 0) {
		perror("rename ENOSPC postcondition");
		goto out;
	}
	tail = written >= BLOCK ? (off_t)(written - BLOCK) : 0;
	if (verify_byte(fill_fd, 0, 0x5a) || verify_byte(fill_fd, tail, 0x5a))
		goto out;

	/* A failed preallocation must not corrupt the already written extent. */
	snprintf(path, sizeof(path), "%s/probe", dir);
	probe_fd = open(path, O_CREAT | O_TRUNC | O_RDWR, 0600);
	if (probe_fd < 0) {
		perror("open probe");
		goto out;
	}
	report_fallocate("prealloc_full", probe_fd, 0, 0, (off_t)CHUNK * 256);
	report_fallocate("zero_full", probe_fd,
			 FALLOC_FL_ZERO_RANGE | FALLOC_FL_KEEP_SIZE,
			 0, (off_t)CHUNK * 128);
	if (verify_byte(fill_fd, 0, 0x5a) || verify_byte(fill_fd, tail, 0x5a))
		goto out;

	/* Free a real range, then retry allocation and exercise rollback again. */
	errno = 0;
	rc = fallocate(fill_fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE,
		       (off_t)(written / 2), (off_t)(written / 4));
	printf("punch rc=%d errno=%d (%s)\n", rc, errno, strerror(errno));
	if (rc < 0)
		goto out;
	report_fallocate("retry_prealloc", probe_fd, 0, 0, (off_t)CHUNK * 64);
	if (fsync(fill_fd) < 0 || fsync(probe_fd) < 0)
		perror("post recovery fsync");
	if (ftruncate(probe_fd, 0) < 0)
		perror("probe truncate");
	if (fsync(probe_fd) < 0)
		perror("probe truncate fsync");
	printf("enospc_rollback_complete written=%llu\n",
	       (unsigned long long)written);
	rc = 0;
out:
	if (probe_fd >= 0)
		close(probe_fd);
	if (fill_fd >= 0)
		close(fill_fd);
	if (!rc) {
		unlink(path);
		snprintf(path, sizeof(path), "%s/fill", dir);
		if (unlink(path) < 0)
			perror("unlink fill");
		if (remove_namespace_pressure(dir) < 0)
			perror("remove rename namespace pressure");
	}
	if (rmdir(dir) < 0 && errno != ENOTEMPTY)
		perror("rmdir");
	return rc;
}
