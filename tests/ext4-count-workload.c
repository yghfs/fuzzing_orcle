// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/falloc.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#define WORKERS 4
#define ITERATIONS 12

static void require(int ok, const char *operation)
{
	if (!ok) {
		perror(operation);
		exit(1);
	}
}

static void worker(const char *dir, unsigned int id)
{
	char path[512], shared[512];
	char data[4096];
	int fd, shared_fd;
	unsigned int i;

	memset(data, 'A' + id, sizeof(data));
	snprintf(shared, sizeof(shared), "%s/shared", dir);
	shared_fd = open(shared, O_RDWR | O_CLOEXEC);
	require(shared_fd >= 0, "open shared");
	for (i = 0; i < ITERATIONS; i++) {
		snprintf(path, sizeof(path), "%s/worker-%u-%u", dir, id, i);
		fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
		require(fd >= 0, "open worker");
		require(ftruncate(fd, 1024 * 1024) == 0, "sparse size");
		require(pwrite(fd, data, sizeof(data), 0) == sizeof(data), "delayed start");
		require(pwrite(fd, data, sizeof(data), 31 * 4096) == sizeof(data),
			"delayed end");
		require(fallocate(fd, FALLOC_FL_KEEP_SIZE, 4096, 30 * 4096) == 0,
			"unwritten middle");
		require(fsync(fd) == 0, "fsync unwritten");
		require(fallocate(fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE,
				 8192, 20 * 4096) == 0, "punch hole");

		require(ftruncate(fd, 0) == 0, "truncate");
		require(fsync(fd) == 0, "fsync truncated");
		require(unlink(path) == 0, "unlink open inode");
		require(close(fd) == 0, "close unlinked inode");
		require(pwrite(shared_fd, data, sizeof(data),
				(off_t)(id * ITERATIONS + i) * 8192) == sizeof(data),
			"concurrent shared write");
		require(fsync(shared_fd) == 0, "concurrent shared fsync");
	}
	require(close(shared_fd) == 0, "close shared");
}

int main(int argc, char **argv)
{
	char path[512];
	pid_t children[WORKERS];
	int fd, status, failed = 0;
	unsigned int i;

	if (argc != 2) {
		fprintf(stderr, "usage: %s DIR\n", argv[0]);
		return 2;
	}
	require(mkdir(argv[1], 0700) == 0, "mkdir");
	snprintf(path, sizeof(path), "%s/shared", argv[1]);
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	require(fd >= 0, "create shared");
	require(close(fd) == 0, "close shared seed");
	for (i = 0; i < WORKERS; i++) {
		children[i] = fork();
		require(children[i] >= 0, "fork");
		if (!children[i]) {
			worker(argv[1], i);
			_exit(0);
		}
	}
	for (i = 0; i < WORKERS; i++) {
		require(waitpid(children[i], &status, 0) == children[i], "waitpid");
		if (!WIFEXITED(status) || WEXITSTATUS(status))
			failed = 1;
	}
	if (failed)
		return 1;
	require(unlink(path) == 0, "unlink shared");
	require(rmdir(argv[1]) == 0, "rmdir");
	printf("count_workload=complete workers=%u iterations=%u\n",
	       WORKERS, ITERATIONS);
	return 0;
}
