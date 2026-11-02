// SPDX-License-Identifier: GPL-2.0-only
/* Exercise extent tree truncate with ENOMEM pressure. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define BLOCK 4096
#define SEED_EXTENTS 10
#define TARGET_EXTENTS 128

static void die(const char *what)
{
	perror(what);
	exit(1);
}

static void *lock_pressure(size_t bytes)
{
	struct rlimit lim = { .rlim_cur = bytes, .rlim_max = bytes };
	void *p;

	if (setrlimit(RLIMIT_MEMLOCK, &lim) && errno != EPERM)
		die("setrlimit");
	p = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
	if (p == MAP_FAILED)
		die("mmap pressure");
	if (mlock(p, bytes))
		die("mlock pressure");
	memset(p, 0xa5, bytes);
	return p;
}

static int seed_file(const char *path)
{
	char data[BLOCK];
	int fd;

	memset(data, 0x5a, sizeof(data));
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		return -1;
	for (unsigned int i = 0; i < SEED_EXTENTS; i++) {
		if (pwrite(fd, data, sizeof(data), (off_t)i * 2 * BLOCK) !=
		    (ssize_t)sizeof(data) || fsync(fd)) {
			int saved = errno ?: EIO;
			close(fd);
			errno = saved;
			return -1;
		}
	}
	return fd;
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/ext4-extent-truncate";
	unsigned long pressure_mb = argc > 2 ? strtoul(argv[2], NULL, 0) : 180;
	unsigned int attempts = argc > 3 ? strtoul(argv[3], NULL, 0) : 128;
	void *pressure;
	char path[512];
	unsigned int ok = 0, failed = 0;

	if (mkdir(dir, 0700) && errno != EEXIST)
		die("mkdir workload");
	pressure = lock_pressure((size_t)pressure_mb * 1024 * 1024);
	for (unsigned int i = 0; i < attempts; i++) {
		int fd, rc, fsync_rc, saved_errno;

		snprintf(path, sizeof(path), "%s/extent-%u", dir, i);
		fd = seed_file(path);
		if (fd < 0)
			die("seed extent file");
		/* Grow to force extent tree before truncate */
		if (ftruncate(fd, (off_t)TARGET_EXTENTS * BLOCK) < 0)
			die("ftruncate grow");
		if (fsync(fd))
			die("fsync grow");
		errno = 0;
		rc = ftruncate(fd, 0);
		saved_errno = errno;
		errno = 0;
		fsync_rc = fsync(fd);
		printf("attempt=%u ftruncate_zero_rc=%d ftruncate_zero_errno=%d fsync_rc=%d fsync_errno=%d\n",
		       i, rc, rc < 0 ? saved_errno : 0, fsync_rc,
		       fsync_rc < 0 ? errno : 0);
		if (rc < 0 || fsync_rc < 0)
			failed++;
		else
			ok++;
		close(fd);
		if (unlink(path))
			die("unlink extent file");
	}
	munlock(pressure, (size_t)pressure_mb * 1024 * 1024);
	madvise(pressure, (size_t)pressure_mb * 1024 * 1024, MADV_DONTNEED);
	munmap(pressure, (size_t)pressure_mb * 1024 * 1024);
	if (rmdir(dir))
		die("rmdir workload");
	printf("extent_truncate_enomem=complete ok=%u failed=%u pressure_mb=%lu\n",
	       ok, failed, pressure_mb);
	return 0;
}
