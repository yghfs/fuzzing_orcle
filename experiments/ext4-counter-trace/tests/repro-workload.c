/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Counter-trace reproduction fixtures, one target syscall per defect.
 *
 * Each mode performs exactly the metadata-mutating window the bug needs and
 * nothing else, so a blkdebug rule pointing at the inode-table sector used by
 * that syscall hits the error exit rather than an earlier unrelated branch.
 * The exit status is reported so the harness can assert on the syscall errno.
 *
 *   mkdir   : create a subdirectory
 *   rmdir   : remove an empty subdirectory
 *   unlink  : remove a plain file
 *   rename  : rename a plain file onto a fresh name
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static void fail(const char *op)
{
	printf("%s=error errno=%d (%s)\n", op, errno, strerror(errno));
	fflush(stdout);
	exit(10);
}

static void ok(const char *op)
{
	printf("%s=ok\n", op);
	fflush(stdout);
}

int main(int argc, char **argv)
{
	const char *mode;
	char path[512];
	int fd;

	if (argc < 3) {
		fprintf(stderr, "usage: %s MODE DIR\n", argv[0]);
		return 2;
	}
	mode = argv[1];

	if (!strcmp(mode, "mkdir")) {
		snprintf(path, sizeof(path), "%s/child", argv[2]);
		if (mkdir(path, 0700) != 0)
			fail("mkdir");
		ok("mkdir");
		return 0;
	}

	if (!strcmp(mode, "rmdir")) {
		snprintf(path, sizeof(path), "%s/child", argv[2]);
		if (rmdir(path) != 0)
			fail("rmdir");
		ok("rmdir");
		return 0;
	}

	if (!strcmp(mode, "unlink")) {
		snprintf(path, sizeof(path), "%s/victim", argv[2]);
		if (unlink(path) != 0)
			fail("unlink");
		ok("unlink");
		return 0;
	}

	if (!strcmp(mode, "rename")) {
		char dst[512];

		snprintf(path, sizeof(path), "%s/src", argv[2]);
		snprintf(dst, sizeof(dst), "%s/dst", argv[2]);
		if (rename(path, dst) != 0)
			fail("rename");
		ok("rename");
		return 0;
	}

	/* prepare MODE: build the state the target syscall needs. */
	if (!strcmp(mode, "prepare-rmdir")) {
		snprintf(path, sizeof(path), "%s/child", argv[2]);
		if (mkdir(path, 0700) != 0)
			fail("prepare-rmdir");
		ok("prepare-rmdir");
		return 0;
	}

	if (!strcmp(mode, "prepare-unlink")) {
		snprintf(path, sizeof(path), "%s/victim", argv[2]);
		fd = open(path, O_CREAT | O_WRONLY | O_CLOEXEC, 0600);
		if (fd < 0)
			fail("prepare-unlink create");
		if (write(fd, "x", 1) != 1)
			fail("prepare-unlink write");
		if (fsync(fd) != 0)
			fail("prepare-unlink fsync");
		if (close(fd) != 0)
			fail("prepare-unlink close");
		ok("prepare-unlink");
		return 0;
	}

	if (!strcmp(mode, "prepare-rename")) {
		snprintf(path, sizeof(path), "%s/src", argv[2]);
		fd = open(path, O_CREAT | O_WRONLY | O_CLOEXEC, 0600);
		if (fd < 0)
			fail("prepare-rename create");
		if (write(fd, "x", 1) != 1)
			fail("prepare-rename write");
		if (fsync(fd) != 0)
			fail("prepare-rename fsync");
		if (close(fd) != 0)
			fail("prepare-rename close");
		snprintf(path, sizeof(path), "%s/dst", argv[2]);
		if (unlink(path) != 0 && errno != ENOENT)
			fail("prepare-rename unlink dst");
		ok("prepare-rename");
		return 0;
	}

	/* prepare-mkdir: nothing to build, the parent is the mount root. */
	if (!strcmp(mode, "prepare-mkdir")) {
		ok("prepare-mkdir");
		return 0;
	}

	fprintf(stderr, "unknown mode: %s\n", mode);
	return 2;
}