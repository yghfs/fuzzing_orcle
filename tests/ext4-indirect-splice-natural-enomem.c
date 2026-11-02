// SPDX-License-Identifier: GPL-2.0-only
/* Exercise the legacy indirect direct-pointer splice with natural ENOMEM. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/falloc.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define BLOCK 4096
#define MAX_LIMITS 32

static int write_path(const char *path, const char *value)
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

static int seed_file(const char *path)
{
	unsigned char data[BLOCK];
	int fd;

	memset(data, 0x5a, sizeof(data));
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		return -1;
	if (pwrite(fd, data, sizeof(data), 0) != (ssize_t)sizeof(data) ||
	    fsync(fd)) {
		int saved = errno ?: EIO;

		close(fd);
		errno = saved;
		return -1;
	}
	return fd;
}

static int trigger_one(const char *path, const char *parent,
			       unsigned long limit, unsigned int attempt)
{
	char cg[PATH_MAX * 2], control[PATH_MAX * 2 + 32], value[64], pidbuf[32];
	int fd, status;
	pid_t child;

	fd = seed_file(path);
	if (fd < 0) {
		perror("seed indirect file");
		return -1;
	}
	snprintf(cg, sizeof(cg), "%s/try-%u", parent, attempt);
	if (mkdir(cg, 0755) && errno != EEXIST) {
		perror("mkdir cgroup");
		close(fd);
		return -1;
	}
	snprintf(control, sizeof(control), "%s/memory.max", cg);
	snprintf(value, sizeof(value), "%lu\n", limit);
	if (write_path(control, value)) {
		perror("set memory.max");
		close(fd);
		return -1;
	}
	snprintf(control, sizeof(control), "%s/memory.swap.max", cg);
	if (write_path(control, "0\n")) {
		perror("set memory.swap.max");
		close(fd);
		return -1;
	}
	child = fork();
	if (child < 0) {
		perror("fork indirect trigger");
		close(fd);
		return -1;
	}
	if (!child) {
		int rc, saved, sync_errno;

		if (write_path("/proc/self/oom_score_adj", "-1000\n"))
			_exit(70);
		snprintf(control, sizeof(control), "%s/cgroup.procs", cg);
		snprintf(pidbuf, sizeof(pidbuf), "%d\n", getpid());
		if (write_path(control, pidbuf))
			_exit(71);
		errno = 0;
		{
			unsigned char data[BLOCK];

			memset(data, 0x6b, sizeof(data));
			rc = pwrite(fd, data, sizeof(data), BLOCK);
			if (rc == (int)sizeof(data))
				rc = 0;
		}
		saved = rc < 0 ? errno : 0;
		errno = 0;
		sync_errno = fsync(fd) ? errno : 0;
		printf("attempt=%u limit_bytes=%lu pwrite_rc=%d pwrite_errno=%d "
		       "fsync_errno=%d\n", attempt, limit, rc, saved, sync_errno);
		fflush(stdout);
		_exit(0);
	}
	if (waitpid(child, &status, 0) != child) {
		perror("wait indirect trigger");
		close(fd);
		return -1;
	}
	if (!WIFEXITED(status) || WEXITSTATUS(status))
		printf("attempt=%u limit_bytes=%lu child_status=%d child_signal=%d\n",
		       attempt, limit, status,
		       WIFSIGNALED(status) ? WTERMSIG(status) : 0);
	fflush(stdout);
	close(fd);
	if (rmdir(cg) && errno != ENOENT)
		perror("remove cgroup");
	return 0;
}

int main(int argc, char **argv)
{
	const char *root = argc > 1 ? argv[1] : "/mnt/ext4-indirect-splice";
	const char *limits = argc > 2 ? argv[2] : "4096,8192,16384,32768,65536";
	unsigned long values[MAX_LIMITS];
	char *copy, *save = NULL, *token;
	char parent[PATH_MAX], path[PATH_MAX];
	unsigned int count = 0;

	copy = strdup(limits);
	if (!copy)
		return 1;
	for (token = strtok_r(copy, ",", &save); token;
	     token = strtok_r(NULL, ",", &save)) {
		if (count == MAX_LIMITS)
			return 2;
		values[count++] = strtoul(token, NULL, 0);
	}
	if (!count)
		return 2;
	snprintf(parent, sizeof(parent), "/sys/fs/cgroup/ext4-indirect-splice");
	if (mkdir(parent, 0755) && errno != EEXIST) {
		perror("mkdir cgroup parent");
		return 1;
	}
	if (write_path("/sys/fs/cgroup/ext4-indirect-splice/cgroup.subtree_control",
		       "+memory\n") && errno != EBUSY && errno != EINVAL)
		perror("enable memory controller");
	for (unsigned int i = 0; i < count; i++) {
		snprintf(path, sizeof(path), "%s/indirect-%u", root, i);
		if (trigger_one(path, parent, values[i], i)) {
			free(copy);
			return 1;
		}
	}
	rmdir(parent);
	free(copy);
	return 0;
}
