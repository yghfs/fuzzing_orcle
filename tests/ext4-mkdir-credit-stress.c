// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static void die(const char *what)
{
	perror(what);
	exit(1);
}

int main(int argc, char **argv)
{
	const char *root = argc > 1 ? argv[1] : "/mnt/ext4-mkdir-credit/tree";
	unsigned int seed = argc > 2 ? strtoul(argv[2], NULL, 10) : 20000;
	unsigned int loops = argc > 3 ? strtoul(argv[3], NULL, 10) : 30000;
	char path[256];
	int dfd;
	unsigned int created = 0, mkdir_errors = 0, published = 0;

	if (mkdir(root, 0700) && errno != EEXIST)
		die("mkdir root");
	dfd = open(root, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dfd < 0)
		die("open root");
	for (unsigned int i = 0; i < seed; i++) {
		int fd;
		snprintf(path, sizeof(path), "seed-%05u-abcdefghijklmnopqrstuvwxyz", i);
		fd = openat(dfd, path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
		if (fd < 0) {
			if (errno == ENOSPC || errno == EDQUOT)
				break;
			die("seed openat");
		}
		close(fd);
		created++;
	}
	if (fsync(dfd))
		die("fsync seed directory");
	printf("mkdir_credit_start seed=%u created=%u loops=%u\n",
	       seed, created, loops);
	for (unsigned int i = 0; i < loops; i++) {
		int rc;
		snprintf(path, sizeof(path), "probe-%05u", i);
		rc = mkdirat(dfd, path, 0700);
		if (rc) {
			int saved = errno;
			struct stat st;
			int visible = fstatat(dfd, path, &st, AT_SYMLINK_NOFOLLOW) == 0;
			mkdir_errors++;
			if (visible)
				published++;
			printf("mkdir_error i=%u errno=%d(%s) published=%d\n",
			       i, saved, strerror(saved), visible);
			if (saved != ENOSPC && saved != EDQUOT && saved != EIO &&
			    saved != ENOMEM)
				break;
			continue;
		}
		if (unlinkat(dfd, path, AT_REMOVEDIR))
			die("rmdir probe");
	}
	printf("mkdir_credit_done created=%u mkdir_errors=%u published=%u\n",
	       created, mkdir_errors, published);
	fsync(dfd);
	close(dfd);
	return 0;
}
