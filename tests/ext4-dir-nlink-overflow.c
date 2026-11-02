// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define DEFAULT_CHILDREN 65520

static int parent_nlink(int dirfd, unsigned int *nlink)
{
	struct stat st;

	if (fstat(dirfd, &st) < 0)
		return -1;
	*nlink = st.st_nlink;
	return 0;
}

static int count_entries(int dirfd, unsigned int *count)
{
	DIR *dir = fdopendir(dup(dirfd));
	struct dirent *entry;

	if (!dir)
		return -1;
	*count = 0;
	while ((entry = readdir(dir)))
		if (strcmp(entry->d_name, ".") && strcmp(entry->d_name, ".."))
			(*count)++;
	return closedir(dir);
}

int main(int argc, char **argv)
{
	const char *path = argc > 1 ? argv[1] : "/mnt/dir-nlink/parent";
	unsigned int children = argc > 2 ? strtoul(argv[2], NULL, 10) :
		DEFAULT_CHILDREN;
	unsigned int hold_seconds = argc > 3 ? strtoul(argv[3], NULL, 10) : 0;
	unsigned int before, overflow, after, entries;
	char name[32];
	int dirfd, errors = 0;

	if (!children || children > 100000 || hold_seconds > 600) {
		fprintf(stderr, "children must be in 1..100000, hold in 0..600\n");
		return 2;
	}
	if (mkdir(path, 0700) < 0) {
		perror("mkdir parent");
		return 1;
	}
	dirfd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0) {
		perror("open parent");
		return 1;
	}
	if (parent_nlink(dirfd, &before) < 0) {
		perror("stat parent before");
		return 1;
	}
	for (unsigned int i = 0; i < children; i++) {
		snprintf(name, sizeof(name), "d%05u", i);
		if (mkdirat(dirfd, name, 0700) < 0) {
			fprintf(stderr, "mkdirat index=%u errno=%d (%s)\n", i,
				errno, strerror(errno));
			return 1;
		}
		if (i == 64999 && parent_nlink(dirfd, &overflow) < 0) {
			perror("stat parent at threshold");
			return 1;
		}
		if (!(i % 8192))
			fprintf(stderr, "mkdir_progress=%u\n", i + 1);
	}
	if (syncfs(dirfd) < 0 || parent_nlink(dirfd, &after) < 0 ||
	    count_entries(dirfd, &entries) < 0) {
		perror("sync/stat populated parent");
		return 1;
	}
	printf("dir_nlink_overflow children=%u before=%u at_child_65000=%u "
	       "after=%u entries=%u\n", children, before, overflow, after,
	       entries);
	fflush(stdout);
	if (hold_seconds) {
		int ready = open("/tmp/ext4-dir-nlink-ready",
				 O_CREAT | O_TRUNC | O_WRONLY | O_CLOEXEC, 0600);
		if (ready < 0 || close(ready) < 0) {
			perror("signal full-directory state");
			return 1;
		}
		for (unsigned int i = 0; i < hold_seconds * 10; i++) {
			if (!access("/tmp/ext4-dir-nlink-release", F_OK))
				break;
			usleep(100000);
		}
		unlink("/tmp/ext4-dir-nlink-ready");
		close(dirfd);
		return errors ? 1 : 0;
	}
	if (before != 2 || overflow != 1 || after != 1 || entries != children)
		errors++;
	for (unsigned int i = 0; i < children; i++) {
		snprintf(name, sizeof(name), "d%05u", i);
		if (unlinkat(dirfd, name, AT_REMOVEDIR) < 0) {
			fprintf(stderr, "rmdir index=%u errno=%d (%s)\n", i,
				errno, strerror(errno));
			return 1;
		}
	}
	if (syncfs(dirfd) < 0 || parent_nlink(dirfd, &after) < 0 ||
	    count_entries(dirfd, &entries) < 0) {
		perror("sync/stat emptied parent");
		return 1;
	}
	printf("dir_nlink_cleanup remaining_entries=%u parent_nlink=%u\n",
	       entries, after);
	if (entries || after != 1)
		errors++;
	close(dirfd);
	if (rmdir(path) < 0) {
		perror("rmdir parent");
		return 1;
	}
	return errors ? 1 : 0;
}
