// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/falloc.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define BLOCK 4096
#define EXTENTS 192

static int put_file(const char *path, const char *value)
{
	int fd = open(path, O_WRONLY | O_CLOEXEC);
	ssize_t n;
	if (fd < 0)
		return -1;
	n = write(fd, value, strlen(value));
	close(fd);
	return n == (ssize_t)strlen(value) ? 0 : -1;
}

static int make_file(const char *path)
{
	char data[BLOCK];
	int fd;
	memset(data, 0x5a, sizeof(data));
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		return -1;
	if (ftruncate(fd, (off_t)EXTENTS * 3 * BLOCK) < 0)
		goto fail;
	for (unsigned int i = 0; i < EXTENTS; i++) {
		off_t off = (off_t)i * 3 * BLOCK;
		if (pwrite(fd, data, sizeof(data), off) != sizeof(data))
			goto fail;
	}
	if (fsync(fd) < 0)
		goto fail;
	return fd;
fail:
	close(fd);
	return -1;
}

static int run_one(const char *path, const char *cg, unsigned long limit,
			   unsigned int attempt)
{
	char dir[256], control[320], value[64], pidbuf[32];
	pid_t pid;
	int fd, status;

	fd = make_file(path);
	if (fd < 0)
		return -1;
	snprintf(dir, sizeof(dir), "%s/free-%u", cg, attempt);
	if (mkdir(dir, 0755) && errno != EEXIST)
		goto fail_fd;
	snprintf(control, sizeof(control), "%s/memory.max", dir);
	snprintf(value, sizeof(value), "%lu\n", limit);
	if (put_file(control, value))
		goto fail_cg;
	snprintf(control, sizeof(control), "%s/memory.swap.max", dir);
	if (put_file(control, "0\n"))
		goto fail_cg;
	pid = fork();
	if (pid < 0)
		goto fail_cg;
	if (!pid) {
		snprintf(control, sizeof(control), "%s/cgroup.procs", dir);
		snprintf(pidbuf, sizeof(pidbuf), "%d\n", getpid());
		if (put_file(control, pidbuf))
			_exit(70);
		errno = 0;
		printf("attempt=%u limit=%lu truncate_rc=%d errno=%d (%s)\n",
		       attempt, limit, ftruncate(fd, 0), errno, strerror(errno));
		fflush(stdout);
		fsync(fd);
		close(fd);
		_exit(0);
	}
	close(fd);
	if (waitpid(pid, &status, 0) < 0)
		return -1;
	if (!WIFEXITED(status))
		printf("attempt=%u limit=%lu child_status=%d\n", attempt, limit, status);
	rmdir(dir);
	return 0;
fail_cg:
	perror("configure memory cgroup");
	rmdir(dir);
fail_fd:
	if (errno)
		perror("truncate workload setup");
	close(fd);
	return -1;
}

int main(int argc, char **argv)
{
	const char *root = argc > 1 ? argv[1] : "/mnt/ext4-truncate-free";
	const char *limits = argc > 2 ? argv[2] : "4194304,8388608,16777216,33554432";
	char *copy, *save = NULL, *tok;
	unsigned int attempt = 0;
	if (mkdir("/sys/fs/cgroup/ext4-truncate-free", 0755) && errno != EEXIST)
		return 1;
	if (put_file("/sys/fs/cgroup/ext4-truncate-free/cgroup.subtree_control",
		     "+memory") && errno != EBUSY && errno != EINVAL)
		return 1;
	copy = strdup(limits);
	if (!copy)
		return 1;
	for (tok = strtok_r(copy, ",", &save); tok;
	     tok = strtok_r(NULL, ",", &save)) {
		char path[PATH_MAX];
		unsigned long limit = strtoul(tok, NULL, 0);
		snprintf(path, sizeof(path), "%s/truncate-%u", root, attempt);
		if (run_one(path, "/sys/fs/cgroup/ext4-truncate-free", limit,
			    attempt++)) {
			free(copy);
			return 1;
		}
	}
	free(copy);
	return 0;
}
