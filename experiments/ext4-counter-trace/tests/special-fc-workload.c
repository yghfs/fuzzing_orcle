// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define BLOCK_SIZE 4096
#define BLOCKS 64

static int seed_file(const char *dir)
{
	char path[512];
	char child_path[512];
	char block[BLOCK_SIZE];
	int dirfd, fd, i;

	if (mkdir(dir, 0700) < 0)
		return -1;
	snprintf(path, sizeof(path), "%s/tracked", dir);
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		return -1;
	memset(block, 'A', sizeof(block));
	for (i = 0; i < BLOCKS; i++) {
		if (pwrite(fd, block, sizeof(block), (off_t)i * BLOCK_SIZE) !=
		    (ssize_t)sizeof(block))
			goto fail;
	}
	if (fsync(fd) < 0)
		goto fail;
	dirfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0)
		goto fail;
	if (fsync(dirfd) < 0 || syncfs(dirfd) < 0) {
		close(dirfd);
		goto fail;
	}
	close(dirfd);
	/* Create the directory in the transaction after the full-commit baseline. */
	snprintf(child_path, sizeof(child_path), "%s/child", dir);
	if (mkdir(child_path, 0700) < 0)
		goto fail;
	{
		char child_file[1024];
		int child_file_fd;

		snprintf(child_file, sizeof(child_file), "%s/committed", child_path);
		child_file_fd = open(child_file,
				     O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
		if (child_file_fd < 0)
			goto fail;
		if (write(child_file_fd, "C", 1) != 1 || fsync(child_file_fd) < 0) {
			close(child_file_fd);
			goto fail;
		}
		close(child_file_fd);
	}
	memset(block, 'B', sizeof(block));
	if (pwrite(fd, block, sizeof(block), 0) != (ssize_t)sizeof(block) ||
	    fsync(fd) < 0)
		goto fail;
	{
		char fresh_path[512];
		char fresh_block[BLOCK_SIZE];
		int fresh;
		snprintf(fresh_path, sizeof(fresh_path), "%s/fresh", dir);
		fresh = open(fresh_path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
		/*
		 * Keep this inode out of ext4 inline-data.  Inline data is deliberately
		 * fast-commit-ineligible, while a real extent exercises the inode-create
		 * record and its replay bitmap claim.
		 */
		memset(fresh_block, 'F', sizeof(fresh_block));
		if (fresh < 0 || write(fresh, fresh_block, sizeof(fresh_block)) !=
		    (ssize_t)sizeof(fresh_block) || fsync(fresh) < 0) {
			if (fresh >= 0)
				close(fresh);
			goto fail;
		}
		close(fresh);
	}
	{
		struct stat st, fresh_st;
		char fresh_path[512];
		int fresh_fd;

		snprintf(fresh_path, sizeof(fresh_path), "%s/fresh", dir);
		fresh_fd = open(fresh_path, O_RDONLY | O_CLOEXEC);
		if (fstat(fd, &st) < 0 || fresh_fd < 0 ||
		    fstat(fresh_fd, &fresh_st) < 0) {
			if (fresh_fd >= 0)
				close(fresh_fd);
			goto fail;
		}
		close(fresh_fd);
		printf("fast_commit_ready blocks=%d ino=%lu fresh_ino=%lu\n",
		       BLOCKS, (unsigned long)st.st_ino,
		       (unsigned long)fresh_st.st_ino);
	}
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

static int verify_file(const char *dir)
{
	char path[512];
	char fresh_path[512];
	char child_path[512];
	char child_file[1024];
	char byte, fresh_byte;
	int fd;

	snprintf(path, sizeof(path), "%s/tracked", dir);
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return -1;
	if (pread(fd, &byte, 1, 0) != 1 || byte != 'B') {
		close(fd);
		errno = EIO;
		return -1;
	}
	close(fd);
	snprintf(child_path, sizeof(child_path), "%s/child", dir);
	if (access(child_path, F_OK) < 0)
		return -1;
	snprintf(child_file, sizeof(child_file), "%s/committed", child_path);
	if (access(child_file, F_OK) < 0)
		return -1;
	snprintf(fresh_path, sizeof(fresh_path), "%s/fresh", dir);
	fd = open(fresh_path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return -1;
	if (read(fd, &fresh_byte, 1) != 1 || fresh_byte != 'F') {
		close(fd);
		errno = EIO;
		return -1;
	}
	close(fd);
	puts("fast_commit_verify=ok");
	return 0;
}

static int seed_inode_claim(const char *dir)
{
	char path[512];
	char data[BLOCK_SIZE];
	struct stat st;
	int parent_fd, dir_fd, file_fd;

	if (mkdir(dir, 0700) < 0)
		return -1;
	parent_fd = open("/mnt/fc", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (parent_fd < 0)
		return -1;
	if (fsync(parent_fd) < 0 || syncfs(parent_fd) < 0) {
		close(parent_fd);
		return -1;
	}
	close(parent_fd);
	dir_fd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dir_fd < 0)
		return -1;
	if (fsync(dir_fd) < 0 || syncfs(dir_fd) < 0) {
		close(dir_fd);
		return -1;
	}
	snprintf(path, sizeof(path), "%s/fresh", dir);
	file_fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (file_fd < 0) {
		close(dir_fd);
		return -1;
	}
	memset(data, 'F', sizeof(data));
	/*
	 * Keep the post-baseline inode and directory entry in the fast-commit
	 * transaction for the crash/replay fixture.  fsyncing the parent directory
	 * here can force a full journal commit and remove the replay record we need
	 * to exercise ext4_mark_inode_used().
	 */
	if (write(file_fd, data, sizeof(data)) != (ssize_t)sizeof(data) ||
	    fsync(file_fd) < 0 || fstat(file_fd, &st) < 0) {
		int saved = errno ? errno : EIO;

		close(file_fd);
		close(dir_fd);
		errno = saved;
		return -1;
	}
	close(file_fd);
	close(dir_fd);
	printf("fast_commit_inode_claim_ready fresh_ino=%lu size=%ld\n",
	       (unsigned long)st.st_ino, (long)st.st_size);
	fflush(stdout);
	for (;;)
		pause();
}

static int verify_inode_claim(const char *dir)
{
	char path[512], byte;
	struct stat st;
	int fd;

	snprintf(path, sizeof(path), "%s/fresh", dir);
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return -1;
	if (read(fd, &byte, 1) != 1 || byte != 'F' || fstat(fd, &st) < 0) {
		close(fd);
		errno = EIO;
		return -1;
	}
	close(fd);
	if (st.st_nlink != 1 || st.st_size != BLOCK_SIZE) {
		errno = EIO;
		return -1;
	}
	printf("fast_commit_inode_claim_verify=ok fresh_ino=%lu nlink=%lu size=%ld\n",
	       (unsigned long)st.st_ino, (unsigned long)st.st_nlink,
	       (long)st.st_size);
	return 0;
}

static int seed_rmdir(const char *dir)
{
	char child_path[512];
	char child_file[1024];
	int childfd, child_file_fd;
	int dirfd;

	if (mkdir(dir, 0700) < 0)
		return -1;
	dirfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0)
		return -1;
	if (fsync(dirfd) < 0 || syncfs(dirfd) < 0) {
		close(dirfd);
		return -1;
	}
	close(dirfd);

	snprintf(child_path, sizeof(child_path), "%s/child", dir);
	if (mkdir(child_path, 0700) < 0)
		return -1;
	snprintf(child_file, sizeof(child_file), "%s/committed", child_path);
	child_file_fd = open(child_file,
				     O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
	if (child_file_fd < 0)
		return -1;
	if (write(child_file_fd, "C", 1) != 1 || fsync(child_file_fd) < 0) {
		close(child_file_fd);
		return -1;
	}
	close(child_file_fd);
	/*
	 * Commit the directory and its contents first.  The remove must be in a
	 * later transaction; otherwise fast-commit coalesces the create and remove
	 * and there is no EXT4_FC_TAG_UNLINK to replay.
	 */
	childfd = open(child_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (childfd < 0)
		return -1;
	if (fsync(childfd) < 0) {
		close(childfd);
		return -1;
	}
	close(childfd);
	dirfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0)
		return -1;
	if (fsync(dirfd) < 0 || syncfs(dirfd) < 0) {
		close(dirfd);
		return -1;
	}
	close(dirfd);

	if (unlink(child_file) < 0)
		return -1;
	childfd = open(child_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (childfd < 0)
		return -1;
	if (fsync(childfd) < 0) {
		close(childfd);
		return -1;
	}
	close(childfd);
	if (rmdir(child_path) < 0)
		return -1;
	dirfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0)
		return -1;
	if (fsync(dirfd) < 0) {
		close(dirfd);
		return -1;
	}
	close(dirfd);
	puts("fast_commit_ready rmdir=1");
	fflush(stdout);
	for (;;)
		pause();
}

static int verify_rmdir(const char *dir)
{
	char child_path[512];

	snprintf(child_path, sizeof(child_path), "%s/child", dir);
	if (access(child_path, F_OK) == 0 || errno != ENOENT)
		return -1;
	puts("fast_commit_rmdir_verify=ok");
	return 0;
}

static int seed_link(const char *dir)
{
	char path[512], alias[512];
	int fd, dirfd;

	if (mkdir(dir, 0700) < 0)
		return -1;
	snprintf(path, sizeof(path), "%s/tracked", dir);
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		return -1;
	if (write(fd, "L", 1) != 1 || fsync(fd) < 0) {
		close(fd);
		return -1;
	}
	close(fd);
	dirfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0)
		return -1;
	if (fsync(dirfd) < 0 || syncfs(dirfd) < 0) {
		close(dirfd);
		return -1;
	}
	close(dirfd);
	snprintf(alias, sizeof(alias), "%s/alias", dir);
	if (link(path, alias) < 0)
		return -1;
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return -1;
	if (fsync(fd) < 0) {
		close(fd);
		return -1;
	}
	close(fd);
	dirfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0)
		return -1;
	if (fsync(dirfd) < 0) {
		close(dirfd);
		return -1;
	}
	close(dirfd);
	puts("fast_commit_ready link=1");
	fflush(stdout);
	for (;;)
		pause();
}

static int verify_link(const char *dir)
{
	char path[512], alias[512];
	struct stat st;

	snprintf(path, sizeof(path), "%s/tracked", dir);
	snprintf(alias, sizeof(alias), "%s/alias", dir);
	if (stat(path, &st) < 0 || st.st_nlink != 2)
		return -1;
	if (stat(alias, &st) < 0 || st.st_nlink != 2)
		return -1;
	puts("fast_commit_link_verify=ok");
	return 0;
}

int main(int argc, char **argv)
{
	if (argc != 3 || (strcmp(argv[1], "seed") &&
			  strcmp(argv[1], "seed-inode-claim") &&
			  strcmp(argv[1], "verify") &&
			  strcmp(argv[1], "verify-inode-claim") &&
			  strcmp(argv[1], "seed-rmdir") &&
			  strcmp(argv[1], "verify-rmdir") &&
			  strcmp(argv[1], "seed-link") &&
			  strcmp(argv[1], "verify-link"))) {
		fprintf(stderr, "usage: %s seed|verify|seed-inode-claim|verify-inode-claim|seed-rmdir|verify-rmdir|seed-link|verify-link DIR\n",
				argv[0]);
		return 2;
	}
	if (!strcmp(argv[1], "seed")) {
		if (seed_file(argv[2]) < 0) {
			perror("fast-commit seed");
			return 1;
		}
	} else if (!strcmp(argv[1], "seed-inode-claim")) {
		if (seed_inode_claim(argv[2]) < 0) {
			perror("fast-commit inode-claim seed");
			return 1;
		}
	} else if (!strcmp(argv[1], "verify-inode-claim")) {
		if (verify_inode_claim(argv[2]) < 0) {
			perror("fast-commit inode-claim verify");
			return 1;
		}
	} else if (!strcmp(argv[1], "seed-rmdir")) {
		if (seed_rmdir(argv[2]) < 0) {
			perror("fast-commit rmdir seed");
			return 1;
		}
	} else if (!strcmp(argv[1], "seed-link")) {
		if (seed_link(argv[2]) < 0) {
			perror("fast-commit link seed");
			return 1;
		}
	} else if (!strcmp(argv[1], "verify-rmdir")) {
		if (verify_rmdir(argv[2]) < 0) {
			perror("fast-commit rmdir verify");
			return 1;
		}
	} else if (!strcmp(argv[1], "verify-link")) {
		if (verify_link(argv[2]) < 0) {
			perror("fast-commit link verify");
			return 1;
		}
	} else if (verify_file(argv[2]) < 0) {
		perror("fast-commit verify");
		return 1;
	}
	return 0;
}
