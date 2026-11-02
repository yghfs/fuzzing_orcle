// SPDX-License-Identifier: GPL-2.0
/* Exercise open-unlink orphan lifetime across truncate, sync, and checkpoint. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/ext4.h>
#include <linux/fs.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/ioctl.h>
#include <sys/xattr.h>
#include <unistd.h>

#define FILE_SIZE (128 * 1024)
#define TEST_BLOCK_SIZE 4096
#define DEFAULT_CYCLES 32

struct counters {
	unsigned int checkpoints;
	unsigned int discard_unsupported;
};

static int fail(const char *operation)
{
	fprintf(stderr, "%s: %s\n", operation, strerror(errno));
	return -1;
}

static int checkpoint(int fd, unsigned int flags, struct counters *counts)
{
	if (ioctl(fd, EXT4_IOC_CHECKPOINT, &flags) == 0) {
		counts->checkpoints++;
		return 0;
	}
	if (flags == EXT4_IOC_CHECKPOINT_FLAG_DISCARD &&
	    (errno == EOPNOTSUPP || errno == ENOTTY)) {
		counts->discard_unsupported++;
		return 0;
	}
	return fail("EXT4_IOC_CHECKPOINT");
}

static int write_marker(int fd, const char *data, off_t offset)
{
	size_t length = strlen(data);

	if (pwrite(fd, data, length, offset) != (ssize_t)length)
		return fail("pwrite");
	return 0;
}

static int run_cycle(int rootfd, unsigned int cycle, struct counters *counts)
{
	const char *checkpoint_a = "checkpoint-value-a";
	const char *checkpoint_b = "checkpoint-value-b";
	char dirname[32];
	int dirfd = -1, active = -1, replacement = -1, rc = -1;
	unsigned int discard = EXT4_IOC_CHECKPOINT_FLAG_DISCARD;
	unsigned int flags[] = {
		0,
		EXT4_IOC_CHECKPOINT_FLAG_DRY_RUN,
		EXT4_IOC_CHECKPOINT_FLAG_ZEROOUT,
	};

	snprintf(dirname, sizeof(dirname), "cycle-%04u", cycle);
	if (mkdirat(rootfd, dirname, 0700) < 0)
		return fail("mkdirat cycle");
	dirfd = openat(rootfd, dirname, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0)
		return fail("openat cycle");
	active = openat(dirfd, "active",
			O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (active < 0) {
		fail("openat active");
		goto out;
	}
	replacement = openat(dirfd, "replacement",
			     O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (replacement < 0) {
		fail("openat replacement");
		goto out;
	}
	if (fallocate(active, 0, 0, FILE_SIZE) < 0 ||
	    fallocate(replacement, 0, 0, FILE_SIZE) < 0) {
		fail("fallocate seed");
		goto out;
	}
	if (write_marker(active, "ACTIVE-A", 0) ||
	    write_marker(active, "ACTIVE-B", TEST_BLOCK_SIZE * 2) ||
	    write_marker(replacement, "REPLACEMENT", 0))
		goto out;
	if (fsetxattr(active, "user.checkpoint.a", checkpoint_a,
		      strlen(checkpoint_a), 0) < 0 ||
	    fsetxattr(active, "user.checkpoint.b", checkpoint_b,
		      strlen(checkpoint_b), 0) < 0) {
		fail("fsetxattr");
		goto out;
	}
	if (fsync(active) < 0 || fdatasync(replacement) < 0) {
		fail("sync seeded files");
		goto out;
	}
	if (checkpoint(active, flags[cycle % 3], counts))
		goto out;
	if (linkat(dirfd, "active", dirfd, "alias", 0) < 0) {
		fail("linkat alias");
		goto out;
	}
	if (renameat2(dirfd, "alias", dirfd, "replacement", RENAME_EXCHANGE) < 0) {
		fail("renameat2 exchange");
		goto out;
	}
	if (checkpoint(replacement, discard, counts))
		goto out;
	if (unlinkat(dirfd, "active", 0) < 0 ||
	    unlinkat(dirfd, "replacement", 0) < 0 ||
	    unlinkat(dirfd, "alias", 0) < 0) {
		fail("unlinkat open file");
		goto out;
	}
	if (ftruncate(active, FILE_SIZE / 2) < 0 ||
	    fallocate(active, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE,
		      TEST_BLOCK_SIZE, TEST_BLOCK_SIZE) < 0) {
		fail("truncate or punch unlinked active file");
		goto out;
	}
	if (fsync(active) < 0 || fdatasync(active) < 0) {
		fail("sync unlinked active file");
		goto out;
	}
	if (checkpoint(active, 0, counts))
		goto out;
	if (fremovexattr(active, "user.checkpoint.a") < 0) {
		fail("fremovexattr");
		goto out;
	}
	if (ftruncate(replacement, 0) < 0 || fdatasync(replacement) < 0) {
		fail("truncate/sync unlinked replacement");
		goto out;
	}
	if (checkpoint(replacement, EXT4_IOC_CHECKPOINT_FLAG_DRY_RUN, counts))
		goto out;
	if (close(replacement) < 0) {
		replacement = -1;
		fail("close replacement");
		goto out;
	}
	replacement = -1;
	if (close(active) < 0) {
		active = -1;
		fail("close active");
		goto out;
	}
	active = -1;
	if (mkdirat(dirfd, "subdir", 0700) < 0 ||
	    unlinkat(dirfd, "subdir", AT_REMOVEDIR) < 0) {
		fail("mkdir/rmdir subdir");
		goto out;
	}
	if (syncfs(dirfd) < 0) {
		fail("syncfs cycle");
		goto out;
	}
	rc = 0;
out:
	if (replacement >= 0)
		close(replacement);
	if (active >= 0)
		close(active);
	if (dirfd >= 0)
		close(dirfd);
	if (rc == 0 && unlinkat(rootfd, dirname, AT_REMOVEDIR) < 0)
		return fail("remove cycle directory");
	return rc;
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/ext4-runtime/orphan-checkpoint";
	unsigned long cycles = argc > 2 ? strtoul(argv[2], NULL, 10) : DEFAULT_CYCLES;
	struct counters counts = { 0 };
	unsigned long i;
	int rootfd;

	if (cycles < 1 || cycles > 256) {
		fprintf(stderr, "cycles must be in 1..256\n");
		return 2;
	}
	if (mkdir(dir, 0700) < 0 && errno != EEXIST)
		return fail("mkdir workload directory");
	rootfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (rootfd < 0)
		return fail("open workload directory");
	for (i = 0; i < cycles; i++) {
		if (run_cycle(rootfd, i, &counts)) {
			close(rootfd);
			return 1;
		}
	}
	if (syncfs(rootfd) < 0) {
		close(rootfd);
		return fail("final syncfs");
	}
	close(rootfd);
	if (rmdir(dir) < 0)
		return fail("rmdir workload directory");
	printf("orphan_checkpoint_complete cycles=%lu checkpoints=%u "
	       "discard_unsupported=%u\n",
	       cycles, counts.checkpoints, counts.discard_unsupported);
	return 0;
}
