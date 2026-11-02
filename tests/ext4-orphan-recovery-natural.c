// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int prepare(const char *dir)
{
	char path[512];
	struct stat st;
	int dirfd, fd;

	if (mkdir(dir, 0700) < 0 && errno != EEXIST)
		return -1;
	snprintf(path, sizeof(path), "%s/unlinked-open", dir);
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		return -1;
	if (fallocate(fd, 0, 0, 1024 * 1024) < 0 ||
	    pwrite(fd, "orphan-data", 11, 0) != 11 || fsync(fd) < 0 ||
	    fstat(fd, &st) < 0)
		goto fail;
	if (unlink(path) < 0 || fstat(fd, &st) < 0 || st.st_nlink != 0)
		goto fail;
	dirfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0)
		goto fail;
	if (fsync(dirfd) < 0 || syncfs(dirfd) < 0) {
		close(dirfd);
		goto fail;
	}
	close(dirfd);
	printf("orphan_prepared ino=%lu nlink=%lu\n",
	       (unsigned long)st.st_ino, (unsigned long)st.st_nlink);
	fflush(stdout);
	for (;;)
		pause();
fail:
	{
		int saved = errno;
		close(fd);
		errno = saved;
	}
	return -1;
}

static int verify(const char *dir)
{
	char path[512];
	snprintf(path, sizeof(path), "%s/unlinked-open", dir);
	if (access(path, F_OK) == 0 || errno != ENOENT) {
		errno = EIO;
		return -1;
	}
	puts("orphan_recovery_verify=ok");
	return 0;
}

int main(int argc, char **argv)
{
	if (argc != 3 || (strcmp(argv[1], "prepare") &&
			  strcmp(argv[1], "verify"))) {
		fprintf(stderr, "usage: %s prepare|verify DIR\n", argv[0]);
		return 2;
	}
	if (!strcmp(argv[1], "prepare")) {
		if (prepare(argv[2]) < 0) {
			perror("orphan prepare");
			return 1;
		}
	} else if (verify(argv[2]) < 0) {
		perror("orphan verify");
		return 1;
	}
	return 0;
}
