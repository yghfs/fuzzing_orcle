// SPDX-License-Identifier: GPL-2.0-only
/* Stress failed mkdir cleanup with ordinary allocation and memory pressure. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define MAX_PARENTS 4096
#define MAX_LOOPS 1000000
#define FILL_CHUNK (1024 * 1024)

static void fail(const char *what)
{
	perror(what);
	exit(1);
}

static pid_t start_memory_pressure(size_t mib)
{
	int ready[2];
	pid_t pid;

	if (pipe(ready))
		fail("pipe");
	pid = fork();
	if (pid < 0)
		fail("fork");
	if (!pid) {
		struct rlimit limit = { .rlim_cur = RLIM_INFINITY,
					.rlim_max = RLIM_INFINITY };
		long page = sysconf(_SC_PAGESIZE);
		size_t bytes = mib * 1024 * 1024;
		void *memory;
		char ready_byte = 'R';

		close(ready[0]);
		setrlimit(RLIMIT_MEMLOCK, &limit);
		memory = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
			      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (memory == MAP_FAILED)
			_exit(20);
		for (size_t off = 0; off < bytes; off += page)
			((volatile char *)memory)[off] = (char)(off >> 12);
		if (mlock(memory, bytes))
			_exit(21);
		if (write(ready[1], &ready_byte, 1) != 1)
			_exit(22);
		for (;;)
			pause();
	}
	close(ready[1]);
	{
		char ready_byte;
		if (read(ready[0], &ready_byte, 1) != 1) {
			int status;
			close(ready[0]);
			waitpid(pid, &status, 0);
			errno = ENOMEM;
			fail("lock memory pressure (reduce locked MiB)");
		}
	}
	close(ready[0]);
	return pid;
}

static void drop_caches(void)
{
	int fd = open("/proc/sys/vm/drop_caches", O_WRONLY | O_CLOEXEC);
	if (fd >= 0) {
		ssize_t written = write(fd, "3\n", 2);
		(void)written;
		close(fd);
	}
}

static unsigned long fill_data_blocks(const char *root)
{
	char path[512];
	unsigned long chunks = 0;
	int fd;

	if (snprintf(path, sizeof(path), "%s/.filler", root) >=
	    (int)sizeof(path)) {
		errno = ENAMETOOLONG;
		fail("filler path");
	}
	fd = open(path, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		fail("open filler");
	for (;;) {
		int err = posix_fallocate(fd, (off_t)chunks * FILL_CHUNK,
					  FILL_CHUNK);

		if (err == ENOSPC)
			break;
		if (err) {
			errno = err;
			fail("posix_fallocate filler");
		}
		chunks++;
	}
	if (!chunks) {
		errno = ENOSPC;
		fail("filler allocated no chunks");
	}
	if (fsync(fd))
		fail("fsync filler");
	close(fd);
	return chunks;
}

int main(int argc, char **argv)
{
	const char *root = argc > 1 ? argv[1] : "/mnt/ext4-mkdir-pressure";
	unsigned int loops = argc > 2 ? strtoul(argv[2], NULL, 10) : 20000;
	unsigned int parents = argc > 3 ? strtoul(argv[3], NULL, 10) : 256;
	size_t lock_mib = argc > 4 ? strtoul(argv[4], NULL, 10) : 360;
	char parent[512], child[512];
	pid_t pressure;
	unsigned int mkdir_errors = 0, published_after_error = 0;
	unsigned long filler_chunks;
	int rootfd;

	if (!loops || loops > MAX_LOOPS || !parents || parents > MAX_PARENTS ||
	    !lock_mib)
		return 2;
	if (mkdir(root, 0700) && errno != EEXIST)
		fail("mkdir root");
	for (unsigned int i = 0; i < parents; i++) {
		if (snprintf(parent, sizeof(parent), "%s/p%u", root, i) >=
		    (int)sizeof(parent)) {
			errno = ENAMETOOLONG;
			fail("parent path");
		}
		if (mkdir(parent, 0700) && errno != EEXIST)
			fail("mkdir parent");
	}
	rootfd = open(root, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (rootfd < 0)
		fail("open root");
	if (syncfs(rootfd))
		fail("syncfs setup");
	filler_chunks = fill_data_blocks(root);
	if (syncfs(rootfd))
		fail("syncfs full filesystem");
	pressure = start_memory_pressure(lock_mib);
	drop_caches();
	printf("mkdir_pressure_start loops=%u parents=%u locked_mib=%zu filler_chunks=%lu\n",
	       loops, parents, lock_mib, filler_chunks);
	fflush(stdout);
	for (unsigned int i = 0; i < loops; i++) {
		unsigned int parent_no = i % parents;
		int rc;

		if (snprintf(parent, sizeof(parent), "%s/p%u", root,
			     parent_no) >= (int)sizeof(parent) ||
		    snprintf(child, sizeof(child), "%s/child", parent) >=
			    (int)sizeof(child)) {
			errno = ENAMETOOLONG;
			fail("child path");
		}
		if (!(i % 256))
			drop_caches();
		rc = mkdir(child, 0700);
		if (rc) {
			int saved = errno;
			struct stat st;
			mkdir_errors++;
			if (fstatat(AT_FDCWD, child, &st, AT_SYMLINK_NOFOLLOW) == 0) {
				published_after_error++;
				printf("mkdir_error_published iteration=%u errno=%d mode=%#o nlink=%lu\n",
				       i, saved, st.st_mode, (unsigned long)st.st_nlink);
				(void)rmdir(child);
			}
		} else if (rmdir(child)) {
			if (errno != ENOENT && errno != EIO)
				perror("rmdir child");
		}
		if (!(i % 1000)) {
			printf("mkdir_pressure_progress=%u errors=%u published_after_error=%u\n",
			       i, mkdir_errors, published_after_error);
			fflush(stdout);
		}
	}
	kill(pressure, SIGTERM);
	waitpid(pressure, NULL, 0);
	for (unsigned int i = 0; i < parents; i++) {
		snprintf(parent, sizeof(parent), "%s/p%u", root, i);
		(void)rmdir(parent);
	}
	(void)rmdir(root);
	if (syncfs(rootfd))
		perror("syncfs result");
	close(rootfd);
	printf("mkdir_pressure_complete loops=%u errors=%u published_after_error=%u\n",
	       loops, mkdir_errors, published_after_error);
	return 0;
}
