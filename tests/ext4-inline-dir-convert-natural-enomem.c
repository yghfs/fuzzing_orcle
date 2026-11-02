// SPDX-License-Identifier: GPL-2.0-only
/* Fill inline directories, then make conversion fail through natural ENOMEM. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

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

static int setup_dir(const char *path)
{
	char child[PATH_MAX + 64], name[64];
	int dfd;

	if (mkdir(path, 0700))
		return -1;
	dfd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dfd < 0)
		return -1;
	for (unsigned int i = 0; i < 3; i++) {
		int fd;

		snprintf(name, sizeof(name), "seed-%u", i);
		snprintf(child, sizeof(child), "%s/%s", path, name);
		fd = open(child, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
		if (fd < 0) {
			close(dfd);
			return -1;
		}
		close(fd);
	}
	if (fsync(dfd)) {
		close(dfd);
		return -1;
	}
	return dfd;
}

static int trigger_one(const char *path, const char *parent,
			       unsigned long limit, unsigned int attempt)
{
	char cg[PATH_MAX * 2], control[PATH_MAX * 2 + 32], value[64], pidbuf[32];
	char child[PATH_MAX + 64];
	int dfd, status;
	pid_t pid;

	dfd = setup_dir(path);
	if (dfd < 0) {
		perror("setup inline directory");
		return -1;
	}
	snprintf(cg, sizeof(cg), "%s/try-%u", parent, attempt);
	if (mkdir(cg, 0755) && errno != EEXIST)
		return -1;
	snprintf(control, sizeof(control), "%s/memory.max", cg);
	snprintf(value, sizeof(value), "%lu\n", limit);
	if (write_path(control, value))
		return -1;
	snprintf(control, sizeof(control), "%s/memory.swap.max", cg);
	if (write_path(control, "0\n"))
		return -1;
	snprintf(child, sizeof(child), "%s/trigger", path);
	pid = fork();
	if (pid < 0)
		return -1;
	if (!pid) {
		int rc, saved;

		/* Leave the syscall result observable if the cgroup rejects pages. */
		if (write_path("/proc/self/oom_score_adj", "-1000\n"))
			_exit(70);
		snprintf(control, sizeof(control), "%s/cgroup.procs", cg);
		snprintf(pidbuf, sizeof(pidbuf), "%d\n", getpid());
		if (write_path(control, pidbuf))
			_exit(71);
		errno = 0;
		rc = mkdir(child, 0700);
		saved = rc ? errno : 0;
		printf("attempt=%u limit_bytes=%lu mkdir_rc=%d mkdir_errno=%d\n",
		       attempt, limit, rc, saved);
		fflush(stdout);
		_exit(0);
	}
	if (waitpid(pid, &status, 0) != pid)
		return -1;
	if (!WIFEXITED(status) || WEXITSTATUS(status))
		printf("attempt=%u limit_bytes=%lu child_status=%d child_signal=%d\n",
		       attempt, limit, status,
		       WIFSIGNALED(status) ? WTERMSIG(status) : 0);
	fflush(stdout);
	/* Keep the parent directory inode referenced for the checker snapshot. */
	if (rmdir(cg) && errno != ENOENT)
		perror("remove cgroup");
	(void)fsync(dfd);
	return dfd;
}

int main(int argc, char **argv)
{
	const char *root = argc > 1 ? argv[1] : "/mnt/ext4-inline-dir";
	const char *limits = argc > 2 ? argv[2] : "32768,49152,65536,98304,131072";
	unsigned long values[MAX_LIMITS];
	char *copy, *save = NULL, *token;
	char parent[PATH_MAX], path[PATH_MAX];
	int *fds;
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
	if (mkdir(root, 0700) && errno != EEXIST)
		return 1;
	snprintf(parent, sizeof(parent), "/sys/fs/cgroup/ext4-inline-dir");
	if (mkdir(parent, 0755) && errno != EEXIST)
		return 1;
	if (write_path("/sys/fs/cgroup/ext4-inline-dir/cgroup.subtree_control",
		       "+memory\n") && errno != EBUSY && errno != EINVAL)
		perror("enable memory controller");
	fds = calloc(count, sizeof(*fds));
	if (!fds)
		return 1;
	for (unsigned int i = 0; i < count; i++) {
		snprintf(path, sizeof(path), "%s/d-%u", root, i);
		fds[i] = trigger_one(path, parent, values[i], i);
		if (fds[i] < 0)
			return 1;
	}
	for (unsigned int i = 0; i < count; i++)
		close(fds[i]);
	free(fds);
	rmdir(parent);
	return 0;
}
