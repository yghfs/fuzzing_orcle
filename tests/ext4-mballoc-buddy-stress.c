// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/falloc.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#define WORKERS 4
#define ITERATIONS 80
#define EXTENTS 96
#define BLOCK_SIZE 4096

static void require(int ok, const char *operation)
{
	if (!ok) {
		perror(operation);
		_exit(1);
	}
}

static uint32_t next_random(uint32_t *state)
{
	*state ^= *state << 13;
	*state ^= *state >> 17;
	*state ^= *state << 5;
	return *state;
}

static void worker(const char *dir, unsigned int id)
{
	char path[512];
	char data[BLOCK_SIZE];
	uint32_t state = 0x9e3779b9U ^ (id + 1);

	memset(data, 'a' + id, sizeof(data));
	for (unsigned int iter = 0; iter < ITERATIONS; iter++) {
		int fd;
		off_t highest = 0;

		snprintf(path, sizeof(path), "%s/w%u-%u", dir, id, iter);
		fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
		require(fd >= 0, "open workload file");
		require(ftruncate(fd, (off_t)EXTENTS * 3 * BLOCK_SIZE) == 0,
			"sparse size");

		for (unsigned int n = 0; n < EXTENTS; n++) {
			unsigned int slot = next_random(&state) % EXTENTS;
			off_t offset = (off_t)(slot * 3) * BLOCK_SIZE;

			if (pwrite(fd, data, sizeof(data), offset) != sizeof(data))
				require(0, "fragmented write");
			if (offset > highest)
				highest = offset;
		}
		require(fsync(fd) == 0, "flush fragmented extents");
		require(fallocate(fd, 0, BLOCK_SIZE,
				 (off_t)EXTENTS * 2 * BLOCK_SIZE) == 0,
			"preallocate range");

		for (unsigned int n = 0; n < EXTENTS; n += 2) {
			unsigned int slot = next_random(&state) % EXTENTS;
			off_t offset = (off_t)(slot * 3) * BLOCK_SIZE;

			require(fallocate(fd, FALLOC_FL_PUNCH_HOLE |
					 FALLOC_FL_KEEP_SIZE, offset, BLOCK_SIZE) == 0,
				"punch fragmented extent");
		}
		require(ftruncate(fd, highest + BLOCK_SIZE) == 0,
			"truncate fragmented file");
		require(fsync(fd) == 0, "flush truncated file");
		require(unlink(path) == 0, "unlink workload file");
		require(close(fd) == 0, "close workload file");
	}
	_exit(0);
}

int main(int argc, char **argv)
{
	pid_t children[WORKERS];
	int status, failed = 0;

	if (argc != 2) {
		fprintf(stderr, "usage: %s DIR\n", argv[0]);
		return 2;
	}
	require(mkdir(argv[1], 0700) == 0, "mkdir workload directory");
	for (unsigned int i = 0; i < WORKERS; i++) {
		children[i] = fork();
		require(children[i] >= 0, "fork workload worker");
		if (!children[i])
			worker(argv[1], i);
	}
	for (unsigned int i = 0; i < WORKERS; i++) {
		require(waitpid(children[i], &status, 0) == children[i],
			"wait workload worker");
		if (!WIFEXITED(status) || WEXITSTATUS(status))
			failed = 1;
	}
	require(!failed, "buddy stress worker");
	require(rmdir(argv[1]) == 0, "remove workload directory");
	puts("mballoc_buddy_stress=complete workers=4 iterations=80 extents=96");
	return 0;
}
