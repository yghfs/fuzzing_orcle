// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/ext4.h>
#include <linux/falloc.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define WORKERS 2
#define ROUNDS 12
#define EXTENTS 48
#define ES_BLOCK_SIZE 4096

static void fail(const char *what)
{
	perror(what);
	_exit(1);
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
	char data[ES_BLOCK_SIZE];
	uint32_t state = 0x243f6a88U ^ (id + 1);

	memset(data, 'a' + id, sizeof(data));
	for (unsigned int round = 0; round < ROUNDS; round++) {
		int fd;
		off_t size = (off_t)EXTENTS * 4 * ES_BLOCK_SIZE;

		snprintf(path, sizeof(path), "%s/w%u-%u", dir, id, round);
		fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
		if (fd < 0)
			fail("create workload file");
		if (ftruncate(fd, size))
			fail("sparse file size");

		for (unsigned int i = 0; i < EXTENTS; i++) {
			unsigned int slot = next_random(&state) % EXTENTS;
			off_t offset = (off_t)slot * 4 * ES_BLOCK_SIZE;

			if (pwrite(fd, data, sizeof(data), offset) != sizeof(data))
				fail("fragmented write");
		}
		if (fsync(fd))
			fail("sync fragmented writes");
		if (ioctl(fd, EXT4_IOC_PRECACHE_EXTENTS, 0))
			fail("precache extents");

		for (unsigned int slot = round % 2; slot < EXTENTS; slot += 2) {
			off_t offset = (off_t)slot * 4 * ES_BLOCK_SIZE;

			if (fallocate(fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE,
				      offset, BLOCK_SIZE))
				fail("punch written range");
		}
		if (fallocate(fd, FALLOC_FL_ZERO_RANGE, ES_BLOCK_SIZE,
			      (off_t)EXTENTS * ES_BLOCK_SIZE))
			fail("zero range");
		if (ioctl(fd, EXT4_IOC_PRECACHE_EXTENTS, 0))
			fail("re-precache extents");
		if (ioctl(fd, EXT4_IOC_CLEAR_ES_CACHE, 0))
			fail("clear extent cache");
		if (ftruncate(fd, size / 2))
			fail("truncate file");
		if (fsync(fd))
			fail("sync truncated file");
		if (close(fd))
			fail("close workload file");
		if (unlink(path))
			fail("unlink workload file");
	}
	_exit(0);
}

static void memory_pressure(void)
{
	size_t size = 512UL * 1024 * 1024;
	volatile unsigned char *memory;

	memory = mmap(NULL, size, PROT_READ | PROT_WRITE,
		      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (memory == MAP_FAILED)
		fail("mmap memory pressure");
	for (size_t offset = 0; offset < size; offset += 4096)
		memory[offset] = (unsigned char)(offset >> 12);
	if (munmap((void *)memory, size))
		fail("munmap memory pressure");
}

int main(int argc, char **argv)
{
	pid_t children[WORKERS];
	int failed = 0;

	if (argc != 2) {
		fprintf(stderr, "usage: %s DIR\n", argv[0]);
		return 2;
	}
	if (mkdir(argv[1], 0700))
		fail("mkdir workload directory");
	for (unsigned int i = 0; i < WORKERS; i++) {
		children[i] = fork();
		if (children[i] < 0)
			fail("fork worker");
		if (!children[i])
			worker(argv[1], i);
	}
	for (unsigned int i = 0; i < WORKERS; i++) {
		int status;

		if (waitpid(children[i], &status, 0) != children[i])
			fail("wait worker");
		if (!WIFEXITED(status) || WEXITSTATUS(status))
			failed = 1;
	}
	if (failed) {
		errno = EIO;
		fail("worker failed");
	}
	memory_pressure();
	if (rmdir(argv[1]))
		fail("rmdir workload directory");
	puts("es_tree_count_workload=complete workers=2 rounds=12 extents=48 pressure_mib=512");
	return 0;
}
