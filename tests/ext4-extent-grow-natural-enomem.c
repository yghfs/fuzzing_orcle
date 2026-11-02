// SPDX-License-Identifier: GPL-2.0-only
/* Trigger ext4 extent-tree growth with ordinary fallocate/fsync syscalls. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/limits.h>
#include <linux/falloc.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define BLOCK_SIZE 4096
#define MAX_LIMITS 32

static int write_file(const char *path, const char *value)
{
	int fd = open(path, O_WRONLY | O_CLOEXEC);
	size_t len = strlen(value);
	ssize_t n;

	if (fd < 0)
		return -1;
	n = write(fd, value, len);
	close(fd);
	return n == (ssize_t)len ? 0 : -1;
}

static int make_seed_file(const char *path)
{
	char data[BLOCK_SIZE];
	int fd;

	memset(data, 'E', sizeof(data));
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		return -1;
	/* Four separated extents fill the inode extent root (eh_max == 4). */
	for (int i = 0; i < 4; i++) {
		if (pwrite(fd, data, sizeof(data), (off_t)i * 2 * BLOCK_SIZE) !=
		    sizeof(data) || fsync(fd)) {
			int saved = errno ?: EIO;
			close(fd);
			errno = saved;
			return -1;
		}
	}
	return fd;
}

static int run_trigger(const char *file, const char *cg, unsigned long limit,
		       unsigned int attempt)
{
	char cgpath[128], control[160], value[64];
	pid_t pid;
	int fd, status;

	fd = make_seed_file(file);
	if (fd < 0) {
		perror("seed extent file");
		return -1;
	}
	if (limit) {
		snprintf(cgpath, sizeof(cgpath), "%s/ext4-grow-%u", cg, attempt);
		if (mkdir(cgpath, 0755) && errno != EEXIST) {
			perror("mkdir cgroup");
			close(fd);
			return -1;
		}
		snprintf(control, sizeof(control), "%s/memory.max", cgpath);
		snprintf(value, sizeof(value), "%lu\n", limit);
		if (write_file(control, value)) {
			perror("set memory.max");
			close(fd);
			return -1;
		}
		snprintf(control, sizeof(control), "%s/memory.swap.max", cgpath);
		if (write_file(control, "0\n")) {
			perror("set memory.swap.max");
			close(fd);
			return -1;
		}
	}

	pid = fork();
	if (pid < 0) {
		perror("fork trigger");
		close(fd);
		return -1;
	}
	if (!pid) {
		char pidbuf[32];
		int rc, err, sync_err;

		if (limit) {
			if (write_file("/proc/self/oom_score_adj", "-1000\n"))
				_exit(70);
			snprintf(control, sizeof(control), "%s/cgroup.procs", cgpath);
			snprintf(pidbuf, sizeof(pidbuf), "%d\n", getpid());
			if (write_file(control, pidbuf))
				_exit(71);
		}
		errno = 0;
		rc = fallocate(fd, 0, 8 * BLOCK_SIZE, BLOCK_SIZE);
		err = rc < 0 ? errno : 0;
		errno = 0;
		sync_err = fsync(fd) ? errno : 0;
		printf("attempt=%u limit_bytes=%lu fallocate_rc=%d fallocate_errno=%d "
		       "fsync_errno=%d\n", attempt, limit, rc, err, sync_err);
		fflush(stdout);
		close(fd);
		_exit(0);
	}
	close(fd);
	if (waitpid(pid, &status, 0) < 0) {
		perror("wait trigger");
		return -1;
	}
	if (!WIFEXITED(status) || WEXITSTATUS(status)) {
		printf("attempt=%u limit_bytes=%lu child_status=%d child_signal=%d\n",
		       attempt, limit, status,
	       WIFSIGNALED(status) ? WTERMSIG(status) : 0);
		fflush(stdout);
	}
	if (limit) {
		snprintf(cgpath, sizeof(cgpath), "%s/ext4-grow-%u", cg, attempt);
		/* The child has exited, so the cgroup can now be removed. */
		if (rmdir(cgpath) && errno != ENOENT)
			perror("remove cgroup");
	}
	return 0;
}

int main(int argc, char **argv)
{
	const char *root = argc > 1 ? argv[1] : "/mnt/ext4-extent-grow";
	const char *limits = argc > 2 ? argv[2] : "0";
	const char *cg = "/sys/fs/cgroup";
	unsigned long values[MAX_LIMITS];
	char *copy, *saveptr = NULL, *token;
	unsigned int count = 0;

	copy = strdup(limits);
	if (!copy) {
		perror("strdup limits");
		return 1;
	}
	for (token = strtok_r(copy, ",", &saveptr); token;
	     token = strtok_r(NULL, ",", &saveptr)) {
		if (count == MAX_LIMITS) {
			fprintf(stderr, "too many limits (max %u)\n", MAX_LIMITS);
			free(copy);
			return 2;
		}
		values[count++] = strtoul(token, NULL, 0);
	}
	if (!count) {
		fprintf(stderr, "no memory limits supplied\n");
		free(copy);
		return 2;
	}
	if (mkdir("/sys/fs/cgroup/ext4-extent-grow-natural", 0755) &&
	    errno != EEXIST) {
		perror("create cgroup parent");
		free(copy);
		return 1;
	}
	cg = "/sys/fs/cgroup/ext4-extent-grow-natural";
	if (write_file("/sys/fs/cgroup/ext4-extent-grow-natural/cgroup.subtree_control",
		       "+memory")) {
		/* The root may already have enabled the controller. */
		if (errno != EBUSY && errno != EINVAL)
			perror("enable memory controller");
	}
	for (unsigned int i = 0; i < count; i++) {
		char path[PATH_MAX];

		snprintf(path, sizeof(path), "%s/extent-%u", root, i);
		if (run_trigger(path, cg, values[i], i)) {
			free(copy);
			return 1;
		}
	}
	rmdir(cg);
	free(copy);
	return 0;
}
