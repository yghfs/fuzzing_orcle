// SPDX-License-Identifier: GPL-2.0-only
/* Exercise indirect truncation after naturally pressuring reclaimable memory. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>

#define BLOCK_SIZE 4096
#define DIRECT_BLOCKS 12
#define PTRS_PER_BLOCK (BLOCK_SIZE / sizeof(uint32_t))
#define DOUBLE_START (DIRECT_BLOCKS + PTRS_PER_BLOCK)

static void die(const char *what)
{
	perror(what);
	exit(1);
}

static void *pin_memory(size_t bytes)
{
	struct rlimit limit = { .rlim_cur = bytes, .rlim_max = bytes };
	volatile unsigned char *memory;

	if (setrlimit(RLIMIT_MEMLOCK, &limit) && errno != EPERM)
		die("setrlimit");
	memory = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		      MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
	if (memory == MAP_FAILED)
		die("mmap pressure");
	if (mlock((void *)memory, bytes))
		die("mlock pressure");
	for (size_t offset = 0; offset < bytes; offset += 4096)
		memory[offset] = (unsigned char)(offset >> 12);
	return (void *)memory;
}

int main(int argc, char **argv)
{
	const char *path = argc > 1 ? argv[1] : "/mnt/indirect-pressure/file";
	unsigned int branches = argc > 2 ? strtoul(argv[2], NULL, 0) : 192;
	unsigned long pressure_mib = argc > 3 ? strtoul(argv[3], NULL, 0) : 180;
	unsigned long dense_mib = argc > 4 ? strtoul(argv[4], NULL, 0) : 0;
	unsigned char data[BLOCK_SIZE];
	struct stat before, after;
	int fd;
	void *pressure;
	int rc, saved_errno;

	if (!dense_mib && (!branches || branches >= PTRS_PER_BLOCK)) {
		fprintf(stderr, "branches must be in 1..%lu\n",
			(unsigned long)PTRS_PER_BLOCK - 1);
		return 2;
	}
	memset(data, 0x6d, sizeof(data));
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		die("open");
	if (dense_mib) {
		size_t chunk_size = 1024 * 1024;
		unsigned char *chunk = malloc(chunk_size);

		if (!chunk)
			die("allocate dense data buffer");
		memset(chunk, 0x6d, chunk_size);
		if (ftruncate(fd, (off_t)dense_mib * 1024 * 1024))
			die("dense grow");
		for (off_t offset = 0; offset < (off_t)dense_mib * 1024 * 1024;
		     offset += (off_t)chunk_size) {
			if (pwrite(fd, chunk, chunk_size, offset) !=
			    (ssize_t)chunk_size)
				die("populate dense indirect file");
		}
		free(chunk);
	} else {
		if (ftruncate(fd, (off_t)(DOUBLE_START +
				  branches * PTRS_PER_BLOCK) * BLOCK_SIZE))
			die("sparse grow");
		for (unsigned int i = 0; i < branches; i++) {
			off_t block = DOUBLE_START +
				(off_t)i * PTRS_PER_BLOCK + 7;
			if (pwrite(fd, data, sizeof(data), block * BLOCK_SIZE) !=
			    (ssize_t)sizeof(data))
				die("populate double-indirect branch");
		}
	}
	if (fsync(fd) || fstat(fd, &before))
		die("sync baseline");
	printf("prepared branches=%u dense_mib=%lu size=%lld st_blocks=%lld\n",
	       branches, dense_mib,
	       (long long)before.st_size, (long long)before.st_blocks);
	fflush(stdout);

	pressure = pressure_mib ?
		pin_memory((size_t)pressure_mib * 1024 * 1024) : NULL;
	errno = 0;
	rc = ftruncate(fd, 0);
	saved_errno = errno;
	if (fstat(fd, &after))
		die("stat after truncate");
	printf("truncate_rc=%d errno=%d (%s) size_after=%lld st_blocks_after=%lld\n",
	       rc, rc < 0 ? saved_errno : 0,
	       strerror(rc < 0 ? saved_errno : 0),
	       (long long)after.st_size, (long long)after.st_blocks);
	fflush(stdout);
	if (rc == 0 && fsync(fd))
		die("fsync truncated file");
	if (close(fd))
		die("close");
	if (pressure) {
		munlock(pressure, (size_t)pressure_mib * 1024 * 1024);
		munmap(pressure, (size_t)pressure_mib * 1024 * 1024);
	}
	return rc < 0 ? 1 : 0;
}
