// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define BLOCK 4096

static int check_file(int fd, const char *phase, unsigned long blocks,
		      off_t size)
{
	struct stat st;
	char data[BLOCK];
	int errors = 0;

	if (fsync(fd) || fstat(fd, &st)) {
		perror("sync/stat");
		return 1;
	}
	printf("phase=%s ino=%lu blocks=%lu expected_blocks=%lu size=%lld expected_size=%lld\n",
	       phase, (unsigned long)st.st_ino, (unsigned long)st.st_blocks,
	       blocks, (long long)st.st_size, (long long)size);
	if ((unsigned long)st.st_blocks != blocks || st.st_size != size)
		errors++;
	if (!size)
		return errors;
	for (int i = 0; i < 4; i++) {
		if (pread(fd, data, sizeof(data), (off_t)i * 2 * BLOCK) != BLOCK) {
			perror("read seed");
			return errors + 1;
		}
		for (int j = 0; j < BLOCK; j++) {
			if (data[j] != 'E') {
				fprintf(stderr, "seed data changed at extent %d byte %d\n", i, j);
				return errors + 1;
			}
		}
	}
	return errors;
}

int main(int argc, char **argv)
{
	const char *mode;
	unsigned int count;
	int expected_errno = ENOMEM;
	int errors = 0;

	if (argc != 4 && argc != 5) {
		fprintf(stderr, "usage: %s DIRECTORY fault|success|retry COUNT [ERRNO]\n", argv[0]);
		return 2;
	}
	if (argc == 5) {
		expected_errno = atoi(argv[4]);
		if (expected_errno != ENOMEM && expected_errno != EIO)
			return 2;
	}
	mode = argv[2];
	if (strcmp(mode, "fault") && strcmp(mode, "success") && strcmp(mode, "retry"))
		return 2;
	count = strtoul(argv[3], NULL, 10);
	if (!count || count > 100)
		return 2;
	for (unsigned int i = 0; i < count; i++) {
		char path[512], data[BLOCK];
		int fd, rc, saved;

		if ((size_t)snprintf(path, sizeof(path), "%s/%s-%u", argv[1], mode, i) >= sizeof(path))
			return 2;
		fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
		if (fd < 0) {
			perror("open");
			return 2;
		}
		memset(data, 'E', sizeof(data));
		for (int j = 0; j < 4; j++) {
			if (pwrite(fd, data, sizeof(data), (off_t)j * 2 * BLOCK) != BLOCK || fsync(fd)) {
				perror("seed");
				close(fd);
				return 2;
			}
		}
		errors += check_file(fd, "seed", 32, 7 * BLOCK);
		errno = 0;
		rc = fallocate(fd, 0, 8 * BLOCK, BLOCK);
		saved = errno;
		printf("mode=%s file=%u fallocate_rc=%d errno=%d\n", mode, i, rc, saved);
		if (!strcmp(mode, "success")) {
			if (rc)
				errors++;
			errors += check_file(fd, "success", 48, 9 * BLOCK);
		} else {
			if (rc != -1 || saved != expected_errno)
				errors++;
			errors += check_file(fd, "failed-grow", 32, 7 * BLOCK);
			if (!strcmp(mode, "retry")) {
				if (fallocate(fd, 0, 8 * BLOCK, BLOCK)) {
					perror("retry fallocate");
					errors++;
				}
				errors += check_file(fd, "retry-success", 48, 9 * BLOCK);
				if (ftruncate(fd, 0)) {
					perror("truncate");
					errors++;
				}
				errors += check_file(fd, "truncate", 0, 0);
			}
		}
		if (close(fd)) {
			perror("close");
			errors++;
		}
	}
	printf("mode=%s files=%u errors=%d\n", mode, count, errors);
	return errors ? 1 : 0;
}
